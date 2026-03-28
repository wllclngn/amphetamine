// Shared memory management for per-thread message queues
//
// Follows the fsync pattern: single named shared memory file, all Wine
// clients mmap it. Each thread gets a fixed-size slot containing its
// ThreadQueue. The server creates and manages the file; clients open it
// by name during thread init.
//
// Layout:
//   Offset 0:           ShmHeader (64 bytes, cache-line aligned)
//   Offset HEADER_SIZE: ThreadQueue slot 0 (THREAD_QUEUE_SIZE bytes)
//   Offset HEADER_SIZE + THREAD_QUEUE_SIZE: ThreadQueue slot 1
//   ...
//   Offset HEADER_SIZE + N * THREAD_QUEUE_SIZE: ThreadQueue slot N
//
// The shm file path is: /dev/shm/triskelion-<prefix-hash>
// (analogous to Wine's /dev/shm/winefsync)

use std::sync::atomic::AtomicU32;
use crate::protocol::thread_id_t;
use crate::queue::{ThreadQueue, THREAD_QUEUE_SIZE};

pub const SHM_MAGIC: u32 = 0x54524953; // "TRIS"
pub const SHM_VERSION: u32 = 1;
pub const MAX_THREADS: u32 = 8192;

// Header at offset 0 of the shared memory file.
// Read by clients to discover layout parameters.
#[repr(C, align(64))]
pub struct ShmHeader {
    pub magic: u32,
    pub version: u32,
    pub max_threads: u32,
    pub queue_size: u32,
    pub next_slot: AtomicU32,
    pub desktop_ready: std::sync::atomic::AtomicU8,
    _reserved: [u8; 43],
}

const _: () = assert!(std::mem::size_of::<ShmHeader>() == 64);
const HEADER_SIZE: usize = std::mem::size_of::<ShmHeader>();

pub struct ShmManager {
    base: *mut u8,
    fd: i32,
    total_size: usize,
    shm_name: String,
    slot_map: std::collections::HashMap<thread_id_t, u32>,
    slab: crate::slab::MmapSlab,
}

// SAFETY: ShmManager holds a raw *mut u8 to memfd-backed shared memory.
// The pointer is valid for the lifetime of the process (memfd + mmap never unmapped).
// All writes go through ThreadQueue atomics or single-writer init paths.
// Only the daemon thread touches ShmManager — no concurrent access.
unsafe impl Send for ShmManager {}

impl ShmManager {
    // Create the shared memory region. Called once at server startup.
    // Returns Err on failure — caller should print the error and exit(1)
    // so Wine falls back to its own wineserver.
    pub fn create(prefix_hash: &str) -> Result<Self, String> {
        let shm_name = format!("/triskelion-{prefix_hash}");
        let total_size = HEADER_SIZE + (MAX_THREADS as usize) * THREAD_QUEUE_SIZE;

        // shm_open: create or truncate
        let c_name = std::ffi::CString::new(shm_name.as_str()).unwrap();
        let fd = unsafe {
            libc::shm_open(
                c_name.as_ptr(),
                libc::O_CREAT | libc::O_RDWR | libc::O_TRUNC,
                0o644,
            )
        };
        if fd < 0 {
            return Err(format!("shm_open({shm_name}) failed: {}", std::io::Error::last_os_error()));
        }

        let ret = unsafe { libc::ftruncate(fd, total_size as libc::off_t) };
        if ret != 0 {
            unsafe { libc::close(fd); }
            return Err(format!("ftruncate({shm_name}) failed: {}", std::io::Error::last_os_error()));
        }

        let base = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                total_size,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED,
                fd,
                0,
            )
        };
        if base == libc::MAP_FAILED {
            unsafe { libc::close(fd); }
            return Err(format!("mmap({shm_name}) failed: {}", std::io::Error::last_os_error()));
        }

        let base = base as *mut u8;

        // Initialize header
        let header = base as *mut ShmHeader;
        unsafe {
            std::ptr::write_bytes(header, 0, 1);
            (*header).magic = SHM_MAGIC;
            (*header).version = SHM_VERSION;
            (*header).max_threads = MAX_THREADS;
            (*header).queue_size = THREAD_QUEUE_SIZE as u32;
        }

        log_info!("shm: created {shm_name} ({total_size} bytes, {MAX_THREADS} slots)");

        Ok(Self {
            base,
            fd,
            total_size,
            shm_name,
            slot_map: std::collections::HashMap::new(),
            slab: crate::slab::MmapSlab::new(MAX_THREADS),
        })
    }

    // Allocate a slot for a thread and initialize its ThreadQueue.
    // O(1) via MmapSlab free list pop or bump.
    pub fn alloc_slot(&mut self, tid: thread_id_t) -> Option<u32> {
        // Reuse existing slot for same tid (reconnection)
        if let Some(&existing) = self.slot_map.get(&tid) {
            let ptr = self.slot_ptr(existing);
            unsafe { ThreadQueue::init_at(ptr, tid); }
            return Some(existing);
        }

        let slot = self.slab.insert()?;

        // Update header high-water mark for client bounds checking
        let header = self.header();
        let hw = self.slab.high_water();
        header.next_slot.store(hw, std::sync::atomic::Ordering::Relaxed);

        let ptr = self.slot_ptr(slot);
        unsafe { ThreadQueue::init_at(ptr, tid); }

        self.slot_map.insert(tid, slot);
        Some(slot)
    }

    /// Free a thread's SHM slot. O(1) via MmapSlab free list push.
    pub fn free_slot(&mut self, tid: thread_id_t) {
        if let Some(slot) = self.slot_map.remove(&tid) {
            let ptr = self.slot_ptr(slot) as *mut u8;
            unsafe { std::ptr::write_bytes(ptr, 0, THREAD_QUEUE_SIZE); }
            self.slab.remove(slot);
        }
    }

    // Get the ThreadQueue for a thread.
    pub fn get_queue(&self, tid: thread_id_t) -> Option<&ThreadQueue> {
        self.slot_map.get(&tid).map(|&slot| {
            unsafe { &*self.slot_ptr(slot) }
        })
    }

    fn header(&self) -> &ShmHeader {
        unsafe { &*(self.base as *const ShmHeader) }
    }

    fn slot_ptr(&self, slot: u32) -> *mut ThreadQueue {
        let offset = HEADER_SIZE + (slot as usize) * THREAD_QUEUE_SIZE;
        debug_assert!(offset + THREAD_QUEUE_SIZE <= self.total_size);
        unsafe { self.base.add(offset) as *mut ThreadQueue }
    }

    pub fn set_desktop_ready(&self) {
        self.header().desktop_ready.store(1, std::sync::atomic::Ordering::Release);
    }

}

impl Drop for ShmManager {
    fn drop(&mut self) {
        unsafe {
            libc::munmap(self.base as *mut _, self.total_size);
            libc::close(self.fd);
        }
        // Unlink the shm file so it's cleaned up
        let c_name = std::ffi::CString::new(self.shm_name.as_str()).unwrap();
        unsafe { libc::shm_unlink(c_name.as_ptr()); }
        log_info!("shm: unlinked {}", self.shm_name);
    }
}

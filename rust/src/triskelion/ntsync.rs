// ntsync -- kernel-native NT sync primitive driver wrapper
//
// Wraps /dev/ntsync ioctls (Linux 6.14+) to provide atomic
// semaphore, mutex, and event operations backed by the kernel.
// Falls back gracefully: NtsyncDevice::open() returns None
// on older kernels without the driver.
//
// Each NtsyncObj is a file descriptor returned by the kernel.
// Drop closes the FD automatically.

use std::os::unix::io::RawFd;

// ---- ioctl codes (computed from _IOW/_IOR/_IOWR macros) ----
// type = 'N' = 0x4E, sizes from /usr/include/linux/ntsync.h

const NTSYNC_IOC_CREATE_SEM:   u64 = 0x40084E80; // _IOW ('N', 0x80, 8)
const NTSYNC_IOC_SEM_RELEASE:  u64 = 0xC0044E81; // _IOWR('N', 0x81, 4)
const NTSYNC_IOC_WAIT_ANY:     u64 = 0xC0284E82; // _IOWR('N', 0x82, 40)
const NTSYNC_IOC_WAIT_ALL:     u64 = 0xC0284E83; // _IOWR('N', 0x83, 40)
const NTSYNC_IOC_CREATE_MUTEX: u64 = 0x40084E84; // _IOW ('N', 0x84, 8)
const NTSYNC_IOC_MUTEX_UNLOCK: u64 = 0xC0084E85; // _IOWR('N', 0x85, 8)
const NTSYNC_IOC_MUTEX_KILL:   u64 = 0x40044E86; // _IOW ('N', 0x86, 4) — abandon mutex on owner death
const NTSYNC_IOC_CREATE_EVENT: u64 = 0x40084E87; // _IOW ('N', 0x87, 8)
const NTSYNC_IOC_EVENT_SET:    u64 = 0x80044E88; // _IOR ('N', 0x88, 4)
const NTSYNC_IOC_EVENT_RESET:  u64 = 0x80044E89; // _IOR ('N', 0x89, 4)
const NTSYNC_IOC_EVENT_PULSE:  u64 = 0x80044E8A; // _IOR ('N', 0x8a, 4)
const NTSYNC_IOC_SEM_READ:    u64 = 0x80084E8B; // _IOR ('N', 0x8b, 8)
const NTSYNC_IOC_MUTEX_READ:  u64 = 0x80084E8C; // _IOR ('N', 0x8c, 8)
const NTSYNC_IOC_EVENT_READ:  u64 = 0x80084E8D; // _IOR ('N', 0x8d, 8)

// ---- Kernel structs (match /usr/include/linux/ntsync.h) ----

#[repr(C)]
struct NtsyncSemArgs {
    count: u32,
    max: u32,
}

#[repr(C)]
struct NtsyncMutexArgs {
    owner: u32,
    count: u32,
}

#[repr(C)]
struct NtsyncEventArgs {
    manual: u32,
    signaled: u32,
}

#[repr(C)]
struct NtsyncWaitArgs {
    timeout: u64,
    objs: u64,
    count: u32,
    index: u32,
    flags: u32,
    owner: u32,
    alert: u32,
    pad: u32,
}

// ---- Public types ----

#[derive(Debug)]
pub enum WaitResult {
    Signaled(u32),  // index of signaled object
    Timeout,
    Alerted,        // alert event was signaled (wait cancelled)
    Error,
}

/// A single ntsync kernel object (semaphore, mutex, or event).
/// Owns the file descriptor; Drop closes it.
pub struct NtsyncObj {
    fd: RawFd,
}

impl NtsyncObj {
    /// Create an NtsyncObj wrapping an existing fd (e.g. from dup()).
    /// The NtsyncObj takes ownership and will close the fd on drop.
    pub fn from_raw_fd(fd: RawFd) -> Self {
        Self { fd }
    }

    /// Duplicate this ntsync object. Returns a new NtsyncObj with an
    /// independent fd pointing to the same kernel sync object.
    pub fn dup(&self) -> Option<Self> {
        let new_fd = unsafe { libc::dup(self.fd) };
        if new_fd < 0 { None } else { Some(Self { fd: new_fd }) }
    }

    /// Release (post) a semaphore. Returns previous count.
    pub fn sem_release(&self, count: u32) -> Result<u32, i32> {
        let mut val = count;
        let ret = unsafe {
            libc::ioctl(self.fd, NTSYNC_IOC_SEM_RELEASE, &mut val as *mut u32)
        };
        if ret < 0 { Err(errno()) } else { Ok(val) }
    }

    /// Unlock a mutex. Returns previous recursion count.
    pub fn mutex_unlock(&self, owner: u32) -> Result<u32, i32> {
        let mut args = NtsyncMutexArgs { owner, count: 0 };
        let ret = unsafe {
            libc::ioctl(self.fd, NTSYNC_IOC_MUTEX_UNLOCK, &mut args as *mut NtsyncMutexArgs)
        };
        if ret < 0 { Err(errno()) } else { Ok(args.count) }
    }

    /// Mark mutex as abandoned (owner died without releasing).
    /// Any thread waiting on this mutex will be woken with EOWNERDEAD.
    /// Required when a thread exits while holding a mutex.
    pub fn mutex_kill(&self, owner: u32) -> Result<(), i32> {
        let owner_val = owner;
        let ret = unsafe {
            libc::ioctl(self.fd, NTSYNC_IOC_MUTEX_KILL, &owner_val as *const u32)
        };
        if ret < 0 { Err(errno()) } else { Ok(()) }
    }

    /// Signal an event. Returns previous state.
    pub fn event_set(&self) -> Result<u32, i32> {
        let mut prev: u32 = 0;
        let ret = unsafe {
            libc::ioctl(self.fd, NTSYNC_IOC_EVENT_SET, &mut prev as *mut u32)
        };
        if ret < 0 { Err(errno()) } else { Ok(prev) }
    }

    /// Reset (unsignal) an event. Returns previous state.
    pub fn event_reset(&self) -> Result<u32, i32> {
        let mut prev: u32 = 0;
        let ret = unsafe {
            libc::ioctl(self.fd, NTSYNC_IOC_EVENT_RESET, &mut prev as *mut u32)
        };
        if ret < 0 { Err(errno()) } else { Ok(prev) }
    }

    /// Pulse an event (set then reset atomically). Returns previous state.
    pub fn event_pulse(&self) -> Result<u32, i32> {
        let mut prev: u32 = 0;
        let ret = unsafe {
            libc::ioctl(self.fd, NTSYNC_IOC_EVENT_PULSE, &mut prev as *mut u32)
        };
        if ret < 0 { Err(errno()) } else { Ok(prev) }
    }

    pub fn fd(&self) -> RawFd { self.fd }

    /// Read event state. Returns (manual, signaled).
    pub fn event_read(&self) -> Option<(u32, u32)> {
        let mut args = NtsyncEventArgs { manual: 0, signaled: 0 };
        let ret = unsafe { libc::ioctl(self.fd, NTSYNC_IOC_EVENT_READ, &mut args as *mut NtsyncEventArgs) };
        if ret < 0 { None } else { Some((args.manual, args.signaled)) }
    }

    /// Read mutex state. Returns (owner, count).
    pub fn mutex_read(&self) -> Option<(u32, u32)> {
        let mut args = NtsyncMutexArgs { owner: 0, count: 0 };
        let ret = unsafe { libc::ioctl(self.fd, NTSYNC_IOC_MUTEX_READ, &mut args as *mut NtsyncMutexArgs) };
        if ret < 0 { None } else { Some((args.owner, args.count)) }
    }

    /// Read semaphore state. Returns (count, max).
    pub fn sem_read(&self) -> Option<(u32, u32)> {
        let mut args = NtsyncSemArgs { count: 0, max: 0 };
        let ret = unsafe { libc::ioctl(self.fd, NTSYNC_IOC_SEM_READ, &mut args as *mut NtsyncSemArgs) };
        if ret < 0 { None } else { Some((args.count, args.max)) }
    }
}

impl Drop for NtsyncObj {
    fn drop(&mut self) {
        unsafe { libc::close(self.fd); }
    }
}

/// Handle to /dev/ntsync device. One per triskelion instance.
pub struct NtsyncDevice {
    fd: RawFd,
}

impl NtsyncDevice {
    /// Try to open /dev/ntsync. Returns None if device doesn't exist.
    pub fn open() -> Option<Self> {
        let path = b"/dev/ntsync\0";
        let fd = unsafe {
            libc::open(path.as_ptr() as *const libc::c_char, libc::O_RDWR | libc::O_CLOEXEC)
        };
        if fd < 0 { None } else { Some(Self { fd }) }
    }

    pub fn create_sem(&self, count: u32, max: u32) -> Option<NtsyncObj> {
        let mut args = NtsyncSemArgs { count, max };
        let fd = unsafe {
            libc::ioctl(self.fd, NTSYNC_IOC_CREATE_SEM, &mut args as *mut NtsyncSemArgs)
        };
        if fd < 0 { None } else { Some(NtsyncObj { fd }) }
    }

    pub fn create_mutex(&self, owner: u32, count: u32) -> Option<NtsyncObj> {
        let mut args = NtsyncMutexArgs { owner, count };
        let fd = unsafe {
            libc::ioctl(self.fd, NTSYNC_IOC_CREATE_MUTEX, &mut args as *mut NtsyncMutexArgs)
        };
        if fd < 0 { None } else { Some(NtsyncObj { fd }) }
    }

    pub fn create_event(&self, manual: bool, signaled: bool) -> Option<NtsyncObj> {
        let mut args = NtsyncEventArgs {
            manual: manual as u32,
            signaled: signaled as u32,
        };
        let fd = unsafe {
            libc::ioctl(self.fd, NTSYNC_IOC_CREATE_EVENT, &mut args as *mut NtsyncEventArgs)
        };
        if fd < 0 { None } else { Some(NtsyncObj { fd }) }
    }

    /// Wait for any of the given objects to become signaled (poll with timeout=0).
    /// `obj_fds` is a slice of ntsync object file descriptors.
    /// `owner` is the thread ID for mutex ownership.
    pub fn wait_any(&self, obj_fds: &[RawFd], timeout_ns: u64, owner: u32) -> WaitResult {
        self.do_wait(NTSYNC_IOC_WAIT_ANY, obj_fds, timeout_ns, owner)
    }

    /// Wait for all objects to become signaled simultaneously.
    pub fn wait_all(&self, obj_fds: &[RawFd], timeout_ns: u64, owner: u32) -> WaitResult {
        self.do_wait(NTSYNC_IOC_WAIT_ALL, obj_fds, timeout_ns, owner)
    }

    /// Raw device fd for use by worker threads.
    pub fn fd(&self) -> RawFd { self.fd }

    fn do_wait(&self, ioctl_code: u64, obj_fds: &[RawFd], timeout_ns: u64, owner: u32) -> WaitResult {
        do_wait_inner(self.fd, ioctl_code, obj_fds, timeout_ns, owner, 0)
    }
}

// ---- Free functions for thread-safe blocking waits ----
// These don't need &NtsyncDevice — just the device fd (valid for EventLoop lifetime).

/// Blocking wait-any with alert support. Callable from any thread.
pub fn wait_any_blocking(device_fd: RawFd, obj_fds: &[RawFd], timeout_ns: u64, owner: u32, alert_fd: RawFd) -> WaitResult {
    do_wait_inner(device_fd, NTSYNC_IOC_WAIT_ANY, obj_fds, timeout_ns, owner, alert_fd as u32)
}

/// Blocking wait-all with alert support. Callable from any thread.
pub fn wait_all_blocking(device_fd: RawFd, obj_fds: &[RawFd], timeout_ns: u64, owner: u32, alert_fd: RawFd) -> WaitResult {
    do_wait_inner(device_fd, NTSYNC_IOC_WAIT_ALL, obj_fds, timeout_ns, owner, alert_fd as u32)
}

fn do_wait_inner(device_fd: RawFd, ioctl_code: u64, obj_fds: &[RawFd], timeout_ns: u64, owner: u32, alert: u32) -> WaitResult {
    if obj_fds.is_empty() {
        return WaitResult::Timeout;
    }

    // Convert RawFd (i32) to u32 for the kernel
    let fds: Vec<u32> = obj_fds.iter().map(|&fd| fd as u32).collect();

    let mut args = NtsyncWaitArgs {
        timeout: timeout_ns,
        objs: fds.as_ptr() as u64,
        count: fds.len() as u32,
        index: 0,
        flags: 0, // CLOCK_MONOTONIC
        owner,
        alert,
        pad: 0,
    };

    let ret = unsafe {
        libc::ioctl(device_fd, ioctl_code, &mut args as *mut NtsyncWaitArgs)
    };

    if ret == 0 {
        // ntsync: index < count means a regular object was signaled.
        // index == count means the alert event was signaled (not a regular object).
        if alert != 0 && args.index == fds.len() as u32 {
            WaitResult::Alerted
        } else {
            WaitResult::Signaled(args.index)
        }
    } else {
        let e = errno();
        if e == libc::ETIMEDOUT {
            WaitResult::Timeout
        } else if e == libc::ECANCELED {
            WaitResult::Alerted
        } else {
            WaitResult::Error
        }
    }
}

impl Drop for NtsyncDevice {
    fn drop(&mut self) {
        unsafe { libc::close(self.fd); }
    }
}

fn errno() -> i32 {
    unsafe { *libc::__errno_location() }
}



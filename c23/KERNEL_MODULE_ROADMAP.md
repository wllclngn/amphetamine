# Triskelion Kernel Module (`/dev/quark`) — Roadmap

## Current State

The C23 kernel module is **90% structurally complete** at ~4,000 LOC. Sync primitives, message queues, handle tables, and the relay bridge all work. The Rust daemon is the proving ground — everything learned there transfers here.

**What works today:**
- `/dev/triskelion` char device with 12 ioctl commands
- Semaphore, mutex, event — full NT semantics, slab-allocated, lock-free fast paths
- WaitForSingleObject / WaitForMultipleObjects with timeout (WaitAny + WaitAll)
- Per-thread message queues (hash table + 256-slot ring buffer, RCU reads)
- FUSE-style relay bridge to userspace daemon (fd marshalling, blocking semantics)
- Handle table (dense array + free list, spinlock-protected)
- Comprehensive test suite with Prometheus metrics

---

## Critical Bugs

### 1. Kbuild Object Name Mismatch
**File:** `c23/Kbuild`
**Problem:** Kbuild references `quark_*.o` but source files are named `triskelion_*.c`. Module will NOT link.
**Fix:** Rename sources to `quark_*.c` OR update Kbuild to reference `triskelion_*.o`.

---

## Architecture Changes Needed

### 2. Port `file_access` Fix from Rust Daemon
**File:** `triskelion_relay.c` or new `triskelion_mapping.c`
**Problem:** `create_mapping` must store `file_access` (FILE_READ_DATA) not section access (GENERIC_READ) in handle entries. Without this, `wine_server_handle_to_fd` returns STATUS_ACCESS_DENIED and Wayland SHM buffers fail — no windows appear.
**Rust reference:** `rust/src/event_loop/file_io.rs` line 635 — `let fd_access = if req.file_access != 0 { req.file_access } else { req.access };`
**Impact:** Blocking — no rendering without this.

### 3. Port `cacheable=0` for Handle 0
**File:** Handle fd dispatch (currently relayed, but if moved in-kernel)
**Problem:** `get_handle_fd` for handle=0 must return `cacheable=0`, not `cacheable=1`. Wine's fd cache calls `add_fd_to_cache(NULL, ...)` which corrupts the cache.
**Rust reference:** `rust/src/event_loop/handles.rs` line 103-108
**Impact:** Blocking — causes 14 fd cache errors at startup, cascading failures.

### 4. User Entry Shared Memory Layout
**File:** New — needs shared session memfd management
**Problem:** Wine reads `user_entry` structs from shared session memory to validate window handles via `get_win_ptr()`. Without correct user_entry type/generation fields, `WAYLAND_WindowPosChanged` never fires and no Wayland surfaces are created.
**Rust reference:** `rust/src/event_loop/mod.rs` lines 623-644 — `alloc_user_handle` writes user_entry at `index * 32`
**Impact:** Blocking — no windows without valid user_entries.

---

## Optimizations to Implement

### 5. Message Queue: O(1) Paint Tracking
**Current:** `get_message` scans all `WindowState` entries for `needs_paint == true` — O(n) per call.
**Target:** Maintain a `paint_queue` (VecDeque / linked list). Push handle when `needs_paint` set, pop in `get_message`. O(1).
**File:** `triskelion_queue.c`
**Priority:** Medium — only matters at scale (hundreds of windows).

### 6. Lock-Free Message Ring with Cache Line Alignment
**Current:** Ring buffer uses spinlock for enqueue/dequeue.
**Target:** SPSC (single-producer single-consumer) lock-free ring with cache-line-aligned head/tail. Producer uses `smp_store_release()`, consumer uses `smp_load_acquire()`. No locks on the fast path.
**File:** `triskelion_queue.c`
**Priority:** High — message pump is the hottest path in games.

### 7. Per-CPU Slab Caches for Relay Entries
**Current:** Single `kmem_cache` for relay entries with global spinlock.
**Target:** Use `SLAB_HWCACHE_ALIGN` flag (already set) but consider `kmem_cache_alloc_node()` for NUMA affinity. For high-throughput relay, a per-CPU free list avoids cross-CPU cache line bouncing.
**File:** `triskelion_relay.c`
**Priority:** Low — relay is cold path (sync is hot).

### 8. Eventfd Integration for Wait Completion
**Current:** `wait_event_interruptible()` blocks the calling thread in kernel.
**Target:** Return an eventfd to userspace immediately. Wine polls/epolls the eventfd. When the sync object is satisfied, kernel signals the eventfd. Zero blocking in kernel — the game thread stays in userspace doing useful work while waiting.
**File:** `triskelion_sync.c`, `triskelion_dispatch.c`
**Priority:** High — eliminates context switches for every Wait call.

### 9. Shared Memory Window State (Zero-Copy)
**Current:** Window state lives in Rust daemon. Every `set_window_pos`, `get_window_info` requires a relay round-trip.
**Target:** Map window state into a shared memory region accessible by both kernel and Wine processes. Wine reads window rects, styles, paint flags directly from shared memory. Only mutations (set_window_pos) go through the kernel.
**File:** New `triskelion_window.c`
**Priority:** High — window operations are second-hottest path after sync.

### 10. Batch Relay (Scatter-Gather)
**Current:** Each Wine server call = one relay entry = one daemon read() + write() cycle.
**Target:** Batch multiple requests into a single read(). Daemon processes batch, returns batch reply in single write(). Reduces syscall overhead for request-heavy sequences (registry enumeration, DLL loading).
**File:** `triskelion_relay.c`
**Priority:** Medium — helps startup time (2000+ registry ops).

### 11. Futex-Based Sync Object Signaling
**Current:** Sync objects use kernel wait queues (`wait_event_interruptible`).
**Target:** Use futex words in shared memory. Wine threads wait on futex directly (no ioctl needed). Kernel writes to futex word and calls `futex_wake()` on signal. This is what ntsync does — match its approach.
**File:** `triskelion_sync.c`
**Priority:** High — ntsync compatibility, zero-syscall wait for already-signaled objects.

### 12. Registry Key Cache in Kernel
**Current:** All registry ops relay to Rust daemon.
**Target:** Cache hot registry keys (Wine\Drivers, Wine\DllOverrides, CurrentVersion) in kernel memory. Serve reads from cache, write-through to daemon. Eliminates ~2200 relay round-trips at startup.
**File:** New `triskelion_registry.c`
**Priority:** Medium — improves startup time from 2s to <0.5s.

---

## New Subsystems Needed

### 13. Process/Thread Lifecycle Management
**What:** Track process creation, thread creation, exit events, exit codes.
**Why:** Wine's `NtCreateProcess`, `NtCreateThread`, `terminate_process` need server-side state.
**Rust reference:** `rust/src/event_loop/thread.rs`, `process.rs`
**Complexity:** Medium — mostly state tracking, some exit event signaling.

### 14. File Handle Management (In-Kernel)
**What:** `create_file`, `create_mapping`, `get_handle_fd` with proper access checks.
**Why:** Every DLL load goes through create_file → create_mapping → map_image_view. Currently relayed.
**Rust reference:** `rust/src/event_loop/file_io.rs`
**Complexity:** High — needs memfd creation, PE image parsing, fd lifecycle management in kernel.

### 15. Window Management (In-Kernel)
**What:** Window handle allocation, style tracking, rect management, paint flags.
**Why:** `set_window_pos` is called 100+ times per frame. Relay latency is unacceptable for games.
**Rust reference:** `rust/src/event_loop/window.rs` — WindowState struct, 20+ handlers
**Complexity:** Very High — largest subsystem, but most impactful for gaming performance.

### 16. Completion Port Support
**What:** I/O completion port creation, message queueing, `remove_completion` blocking.
**Why:** Games use overlapped I/O for async file/network operations.
**Rust reference:** `rust/src/event_loop/completion.rs`
**Complexity:** Medium — ring buffer + wait queue, similar to message queue.

---

## Testing Improvements

### 17. Multi-Thread Stress Test
**What:** Spawn N threads, each doing sync ops + message sends concurrently. Verify no deadlocks, no data corruption, no handle leaks.
**File:** `test_triskelion.py` or new `stress_test.c`
**Priority:** High — kernel bugs = kernel panics.

### 18. Daemon Crash Recovery Test
**What:** Kill the Rust daemon mid-relay. Verify Wine threads unblock with -EIO, no kernel panic, clean state.
**File:** `test_triskelion.py`
**Priority:** High — production resilience.

### 19. Signal Interrupt Recovery Test
**What:** Send SIGINT to Wine process during wait/relay. Verify clean EINTR handling, no stuck threads.
**File:** `test_triskelion.py`
**Priority:** Medium — correctness under signal pressure.

---

## Implementation Priority (Recommended Order)

| # | Item | Impact | Effort | Notes |
|---|------|--------|--------|-------|
| 1 | Fix Kbuild names | Blocking | 5 min | Module won't link without this |
| 2 | Port file_access fix | Blocking | 30 min | No windows without it |
| 3 | Port cacheable=0 fix | Blocking | 10 min | fd cache corruption |
| 4 | User entry shared memory | Blocking | 2 hr | get_win_ptr fails without it |
| 5 | Futex-based sync signaling | High perf | 4 hr | Match ntsync, zero-syscall waits |
| 6 | Lock-free message ring | High perf | 2 hr | Hottest path optimization |
| 7 | Eventfd wait completion | High perf | 3 hr | Eliminate wait context switches |
| 8 | Shared memory window state | High perf | 8 hr | Second-hottest path |
| 9 | Process/thread lifecycle | Required | 4 hr | Basic Wine functionality |
| 10 | File handle management | Required | 6 hr | DLL loading |
| 11 | Multi-thread stress test | Safety | 2 hr | Prevent kernel panics |
| 12 | Batch relay | Medium perf | 3 hr | Startup optimization |
| 13 | Registry cache | Medium perf | 4 hr | Startup optimization |
| 14 | Window management | Required | 12 hr | Largest subsystem |
| 15 | Completion ports | Required | 3 hr | Async I/O |
| 16 | Daemon crash recovery | Safety | 2 hr | Production resilience |

---

## Design Principles

1. **Kernel does sync, daemon does everything else** — until proven otherwise by profiling
2. **Shared memory over syscalls** — window state, sync state, message queues all mapped
3. **Lock-free over spinlocks** — atomic ops on hot paths, spinlocks only for mutations
4. **Slab allocators for everything** — no kmalloc in hot paths
5. **Cache line alignment** — ring buffer heads/tails on separate cache lines
6. **Fail gracefully** — daemon crash = EIO to Wine, not kernel panic
7. **Measure everything** — Prometheus metrics from test suite, latency histograms in production

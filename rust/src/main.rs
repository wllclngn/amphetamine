// triskelion -- lock-free wineserver replacement + proton launcher
//
// Multi-mode binary:
//   ./proton <verb> <exe>   Proton launcher (Steam compatibility tool)
//   triskelion package      Package a built Wine tree for Steam
//   triskelion server       Wineserver replacement daemon
//
// Three legs of the server:
//   queue   -- per-thread message queues (shared-memory ring buffers)
//   ntsync  -- sync primitives via /dev/ntsync kernel driver
//   objects -- handle tables, process/thread state

#[macro_use]
mod log;
mod slab;
mod cli;
mod gaming;
mod clone;
mod status;
mod analyze;
mod configure;
mod profile;
mod launcher;
mod packager;
mod pe_patch;
mod protocol;
mod protocol_remap;
mod queue;
mod ntsync;
mod objects;
mod registry;
mod event_loop;
mod intel;
mod ipc;
mod shm;
mod oneshot;
mod broker;
mod worker;
pub mod pe_scanner;

use std::sync::atomic::{AtomicBool, Ordering};

static SHUTDOWN: AtomicBool = AtomicBool::new(false);

fn main() {
    match cli::parse_args() {
        cli::Mode::Server => run_server(),
        cli::Mode::Launch { verb, args } => {
            std::process::exit(launcher::run(&verb, &args));
        }
        cli::Mode::Package { wine_dir } => {
            std::process::exit(packager::run(&wine_dir));
        }
        cli::Mode::Status => {
            std::process::exit(status::run());
        }
        cli::Mode::Analyze => {
            std::process::exit(analyze::run());
        }
        cli::Mode::Configure { wine_dir, execute } => {
            std::process::exit(configure::run(&wine_dir, execute));
        }
        cli::Mode::Profile { app_id, game_name } => {
            std::process::exit(profile::run_profile(&app_id, game_name.as_deref()));
        }
        cli::Mode::ProfileAttach { label } => {
            std::process::exit(profile::run_profile_attach(label.as_deref()));
        }
        cli::Mode::ProfileCompare { dir_a, dir_b } => {
            std::process::exit(profile::run_profile_compare(&dir_a, &dir_b));
        }
        cli::Mode::ProfileOpcodes { trace_file } => {
            std::process::exit(profile::run_profile_opcodes(&trace_file));
        }
        cli::Mode::Clone => {
            clone::ensure_wine_clone();
            clone::ensure_proton_clone();
            log_info!("Both clones ready");
        }
    }
}

fn run_server() {
    // Panic hook: write backtrace to log file before dying
    std::panic::set_hook(Box::new(|info| {
        let log_dir = "/tmp/amphetamine";
        let _ = std::fs::create_dir_all(log_dir);
        let log_path = format!("{log_dir}/daemon_panic.log");
        let bt = std::backtrace::Backtrace::force_capture();
        let msg = format!("[triskelion] PANIC: {info}\n\nBacktrace:\n{bt}\n");
        eprintln!("{msg}");
        let _ = std::fs::write(&log_path, &msg);
    }));

    // Ignore SIGPIPE — prevents daemon crash on closed pipes
    unsafe { libc::signal(libc::SIGPIPE, libc::SIG_IGN); }

    // Bind the listener socket BEFORE forking. Wine's start_server() does
    // waitpid on us — the parent exits immediately after fork. If the child
    // hasn't bound the socket yet, Wine's connect() gets ECONNREFUSED and
    // it spawns a SECOND daemon (race condition). Binding first guarantees
    // the socket is live before Wine tries to connect.
    let socket_path = resolve_socket_path();
    let listener = ipc::create_listener(&socket_path);

    // Now daemonize: parent exits (Wine's waitpid returns), child continues.
    // The child inherits the bound listen fd — connections queue in the
    // kernel's backlog until we set up epoll and start accepting.
    daemonize();

    // Redirect daemon stderr to a log file so we can debug after daemonize
    let log_dir = std::path::Path::new("/tmp/amphetamine");
    let _ = std::fs::create_dir_all(log_dir);
    // Remove stale desktop_ready sentinel from previous daemon lifetime.
    let _ = std::fs::remove_file(log_dir.join("desktop_ready"));
    // Write daemon PID so the launcher can validate sentinel freshness.
    let _ = std::fs::write(log_dir.join("daemon.pid"), std::process::id().to_string());
    let log_path = log_dir.join("daemon.log");
    if let Ok(f) = std::fs::File::create(&log_path) {
        use std::os::unix::io::IntoRawFd;
        let fd = f.into_raw_fd();
        unsafe { libc::dup2(fd, 2); libc::close(fd); }
    }

    let sigfd = install_signal_handler();

    log_info!("starting (pid {})", std::process::id());

    // Dynamic protocol detection: scan client's ntdll.so, build opcode remap.
    // This MUST happen before accepting connections (sets the handshake version).
    let protocol_remap = protocol_remap::detect_and_remap();
    ipc::set_runtime_protocol_version(protocol_remap.version);

    let prefix_hash = compute_prefix_hash();
    let shm = match shm::ShmManager::create(&prefix_hash) {
        Ok(shm) => shm,
        Err(e) => {
            log_error!("FATAL: {e}");
            std::process::exit(1);
        }
    };

    // fsync SHM: only needed for Proton. System Wine doesn't use it.
    // create_fsync_shm(&prefix_hash);

    let (user_sid_str, user_sid) = parse_prefix_sid();

    // Create EventLoop (still initializes epoll/timerfd for internal use by handlers)
    let mut ev = event_loop::EventLoop::new(listener, sigfd, shm, protocol_remap, user_sid, &user_sid_str);

    // Take listener out of EventLoop — acceptor thread will own it
    let listener = ev.take_listener();

    log_info!("listening on {}", socket_path.display());

    // Create broker channel
    let (broker_tx, broker_rx) = std::sync::mpsc::channel::<broker::BrokerMsg>();

    // Build broker state wrapping EventLoop
    let broker_state = broker::BrokerState {
        ev,
        broker_tx: broker_tx.clone(),
        worker_count: 0,
        active_workers: std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0)),
    };

    // Spawn broker thread — owns all global mutable state
    let broker_handle = std::thread::Builder::new()
        .name("broker".into())
        .spawn(move || {
            broker::broker_main(broker_rx, broker_state);
        })
        .expect("spawn broker thread");

    // Spawn acceptor thread — blocking accept, sends new clients to broker
    let acceptor_tx = broker_tx.clone();
    let _acceptor_handle = std::thread::Builder::new()
        .name("acceptor".into())
        .spawn(move || {
            // Switch listener to blocking mode — we block on accept()
            listener.set_blocking();
            log_info!("acceptor thread started (blocking accept)");

            loop {
                if SHUTDOWN.load(Ordering::Relaxed) {
                    break;
                }
                match listener.accept() {
                    Some((client, msg_fd)) => {
                        if acceptor_tx.send(broker::BrokerMsg::NewClient {
                            client,
                            msg_fd,
                        }).is_err() {
                            break; // broker gone
                        }
                    }
                    None => {
                        // accept() returned None — shouldn't happen in blocking mode
                        // unless interrupted by signal
                        if SHUTDOWN.load(Ordering::Relaxed) {
                            break;
                        }
                    }
                }
            }
            log_info!("acceptor thread exiting");
        })
        .expect("spawn acceptor thread");

    // Spawn timer thread — sends Tick to broker for housekeeping
    let timer_tx = broker_tx.clone();
    let _timer_handle = std::thread::Builder::new()
        .name("timer".into())
        .spawn(move || {
            loop {
                std::thread::sleep(std::time::Duration::from_millis(50));
                if SHUTDOWN.load(Ordering::Relaxed) { break; }
                if timer_tx.send(broker::BrokerMsg::Tick).is_err() { break; }
            }
        })
        .expect("spawn timer thread");

    // Main thread: block on signalfd, set SHUTDOWN on signal
    loop {
        let mut info: libc::signalfd_siginfo = unsafe { std::mem::zeroed() };
        let n = unsafe {
            libc::read(
                sigfd,
                &mut info as *mut _ as *mut _,
                std::mem::size_of::<libc::signalfd_siginfo>(),
            )
        };
        if n > 0 {
            let sig = info.ssi_signo as i32;
            if sig == libc::SIGINT || sig == libc::SIGTERM {
                // Ignore SIGINT and SIGTERM — Steam sends both to the process group
                // when it thinks the game hasn't started (~8-10s timeout).
                // The daemon shuts down via the linger timer when all clients disconnect.
                log_info!("ignoring signal {} from process group", info.ssi_signo);
                continue;
            }
            log_info!("received signal {}", info.ssi_signo);
            SHUTDOWN.store(true, Ordering::Relaxed);
            break;
        }
        if SHUTDOWN.load(Ordering::Relaxed) {
            break;
        }
    }

    // Drop the sender to unblock the broker's recv()
    drop(broker_tx);

    // Wait for broker to finish
    let _ = broker_handle.join();

    log_info!("shutting down");
}

/// Daemonize: fork, parent exits(0), child continues as server daemon.
/// This satisfies Wine's waitpid in start_server() (loader.c:550).
fn daemonize() {
    let pid = unsafe { libc::fork() };
    match pid {
        -1 => {
            log_error!("fork failed: {}", std::io::Error::last_os_error());
            std::process::exit(1);
        }
        0 => {
            // Child: become session leader, continue as daemon
            unsafe { libc::setsid(); }
        }
        _ => {
            // Parent: exit immediately so Wine's waitpid returns
            unsafe { libc::_exit(0); }
        }
    }
}

fn resolve_socket_path() -> std::path::PathBuf {
    let prefix = std::env::var("WINEPREFIX")
        .unwrap_or_else(|_| {
            let home = std::env::var("HOME").expect("HOME not set");
            format!("{home}/.wine")
        });

    let prefix = std::path::Path::new(&prefix);

    let stat = std::fs::metadata(prefix).expect("WINEPREFIX does not exist");
    use std::os::unix::fs::MetadataExt;
    let dev = stat.dev();
    let ino = stat.ino();

    // Proton uses /tmp/.wine-<uid>/server-<dev>-<ino>/ (not $WINEPREFIX/server-...)
    let uid = unsafe { libc::getuid() };
    let base_dir = std::path::PathBuf::from(format!("/tmp/.wine-{uid}"));
    let server_dir = base_dir.join(format!("server-{dev:x}-{ino:x}"));
    // Wine requires 0700 on the socket directory tree — refuses to connect otherwise.
    use std::os::unix::fs::DirBuilderExt;
    let mut builder = std::fs::DirBuilder::new();
    builder.recursive(true).mode(0o700);
    if let Err(e) = builder.create(&server_dir) {
        log_error!("Cannot create server dir {}: {e}", server_dir.display());
        std::process::exit(1);
    }

    server_dir.join("socket")
}

/// Create the fsync shared memory file Proton's ntdll expects.
/// Name: /dev/shm/wine-<prefix_inode>-fsync
/// Size: 4MB initial (Proton grows it as needed via ftruncate).
fn _create_fsync_shm(_prefix_hash: &str) {
    // prefix_hash is "<dev_hex><ino_hex>" — extract inode part
    // Actually, Proton uses the raw inode from stat(), not our hash format.
    // Recompute from WINEPREFIX.
    let prefix = std::env::var("WINEPREFIX")
        .unwrap_or_else(|_| {
            let home = std::env::var("HOME").expect("HOME not set");
            format!("{home}/.wine")
        });
    let stat = std::fs::metadata(&prefix).expect("WINEPREFIX does not exist");
    use std::os::unix::fs::MetadataExt;
    let ino = stat.ino();

    let shm_name = if ino != (ino as u32) as u64 {
        format!("/wine-{:x}{:08x}-fsync", (ino >> 32) as u32, ino as u32)
    } else {
        format!("/wine-{:x}-fsync", ino as u32)
    };

    let c_name = std::ffi::CString::new(shm_name.as_str()).unwrap();
    let fd = unsafe {
        libc::shm_open(c_name.as_ptr(), libc::O_CREAT | libc::O_RDWR, 0o644)
    };
    if fd < 0 {
        log_warn!("fsync shm: failed to create {shm_name}: {}", std::io::Error::last_os_error());
        return;
    }
    // Set initial size — Proton will grow it via ftruncate as needed
    let initial_size: usize = 4 * 1024 * 1024; // 4MB
    unsafe { libc::ftruncate(fd, initial_size as libc::off_t); }

    // Mmap the shm so our server can write initial values for fsync slots
    let base = unsafe {
        libc::mmap(std::ptr::null_mut(), initial_size, libc::PROT_READ | libc::PROT_WRITE,
                   libc::MAP_SHARED, fd, 0)
    };
    if base != libc::MAP_FAILED {
        // Store the mmap'd pointer globally so create_esync/create_fsync can write to it
        unsafe { FSYNC_SHM_BASE = base as *mut u8; }
        unsafe { FSYNC_SHM_SIZE = initial_size; }
        log_info!("fsync shm: created and mmap'd {shm_name} ({initial_size} bytes)");
    } else {
        log_warn!("fsync shm: mmap failed: {}", std::io::Error::last_os_error());
    }
    // Keep fd open (shm needs it for ftruncate growth)
    unsafe { FSYNC_SHM_FD = fd; }
}

// Global fsync shm state (written by create_esync/create_fsync handlers)
static mut FSYNC_SHM_BASE: *mut u8 = std::ptr::null_mut();
static mut FSYNC_SHM_SIZE: usize = 0;
#[allow(dead_code)]
static mut FSYNC_SHM_FD: i32 = -1;

/// Write initial fsync values to a shm slot. Called by esync/fsync handlers.
pub fn fsync_shm_write(idx: u32, low: i32, high: i32) {
    let offset = (idx as usize) * 16;
    let size = unsafe { FSYNC_SHM_SIZE };
    let base = unsafe { FSYNC_SHM_BASE };
    if base.is_null() || offset + 16 > size { return; }
    unsafe {
        let slot = base.add(offset) as *mut i32;
        *slot = low;           // [0] = initial count/state
        *slot.add(1) = high;   // [1] = max count
        *slot.add(2) = 1;     // [2] = refcount
        *slot.add(3) = 0;     // [3] = last ref pid
    }
}

/// Signal an fsync event: atomically set signaled=1 and futex_wake all waiters.
/// This bridges server-side ntsync events with client-side fsync futex waits.
pub fn fsync_signal(idx: u32) {
    let offset = (idx as usize) * 16;
    let size = unsafe { FSYNC_SHM_SIZE };
    let base = unsafe { FSYNC_SHM_BASE };
    if base.is_null() || offset + 16 > size || idx == 0 { return; }
    unsafe {
        let signaled_ptr = base.add(offset) as *mut i32;
        // Atomic exchange: set signaled=1, return old value
        let old = std::sync::atomic::AtomicI32::from_ptr(signaled_ptr)
            .swap(1, std::sync::atomic::Ordering::SeqCst);
        // Only wake if it wasn't already signaled
        if old == 0 {
            libc::syscall(libc::SYS_futex, signaled_ptr, 1 /*FUTEX_WAKE*/, i32::MAX, 0, 0, 0);
        }
    }
}

/// Clear an fsync event: atomically set signaled=0.
pub fn fsync_clear(idx: u32) {
    let offset = (idx as usize) * 16;
    let size = unsafe { FSYNC_SHM_SIZE };
    let base = unsafe { FSYNC_SHM_BASE };
    if base.is_null() || offset + 16 > size || idx == 0 { return; }
    unsafe {
        let signaled_ptr = base.add(offset) as *mut i32;
        std::sync::atomic::AtomicI32::from_ptr(signaled_ptr)
            .store(0, std::sync::atomic::Ordering::SeqCst);
    }
}

fn compute_prefix_hash() -> String {
    let prefix = std::env::var("WINEPREFIX")
        .unwrap_or_else(|_| {
            let home = std::env::var("HOME").expect("HOME not set");
            format!("{home}/.wine")
        });

    let stat = std::fs::metadata(&prefix).expect("WINEPREFIX does not exist");
    use std::os::unix::fs::MetadataExt;
    format!("{:x}{:x}", stat.dev(), stat.ino())
}

/// Parse the user SID from the prefix's user.reg file.
/// Line 2 of user.reg: ";; All keys relative to REGISTRY\\User\\S-1-5-21-A-B-C-RID"
/// Returns (SID string, SID binary bytes).
/// Falls back to S-1-5-21-0-0-0-1000 if parsing fails.
fn parse_prefix_sid() -> (String, Vec<u8>) {
    let prefix = std::env::var("WINEPREFIX")
        .unwrap_or_else(|_| {
            let home = std::env::var("HOME").expect("HOME not set");
            format!("{home}/.wine")
        });

    let reg_path = std::path::Path::new(&prefix).join("user.reg");

    let sid_str = std::fs::read_to_string(&reg_path)
        .ok()
        .and_then(|content| {
            // Find the line: ";; All keys relative to REGISTRY\\User\\S-1-..."
            // File contains literal double backslashes: \\User\\S-
            for line in content.lines().take(5) {
                if let Some(pos) = line.find("\\\\User\\\\") {
                    // Skip past "\\User\\" (8 chars) to get the SID string
                    return Some(line[pos + 8..].trim().to_string());
                }
            }
            None
        });

    if let Some(sid) = sid_str {
        if let Some(bytes) = sid_string_to_bytes(&sid) {
            log_info!("prefix SID: {sid} ({} bytes)", bytes.len());
            return (sid, bytes);
        }
        log_warn!("failed to parse SID \"{sid}\" from {}", reg_path.display());
    } else {
        log_warn!("no SID found in {} — using fallback", reg_path.display());
    }

    // Fallback: S-1-5-21-0-0-0-1000
    let fallback = "S-1-5-21-0-0-0-1000".to_string();
    let bytes = sid_string_to_bytes(&fallback).unwrap();
    (fallback, bytes)
}

/// Convert a SID string like "S-1-5-21-A-B-C-RID" to binary SID bytes.
fn sid_string_to_bytes(sid: &str) -> Option<Vec<u8>> {
    let parts: Vec<&str> = sid.split('-').collect();
    if parts.len() < 4 || parts[0] != "S" { return None; }
    let revision: u8 = parts[1].parse().ok()?;
    let authority: u64 = parts[2].parse().ok()?;
    let sub_authorities: Vec<u32> = parts[3..].iter()
        .map(|s| s.parse::<u32>())
        .collect::<Result<Vec<_>, _>>()
        .ok()?;

    let mut bytes = Vec::with_capacity(8 + sub_authorities.len() * 4);
    bytes.push(revision);
    bytes.push(sub_authorities.len() as u8);
    // IdentifierAuthority: 6 bytes big-endian
    bytes.extend_from_slice(&(authority as u64).to_be_bytes()[2..8]);
    for &sub in &sub_authorities {
        bytes.extend_from_slice(&sub.to_le_bytes());
    }
    Some(bytes)
}

fn install_signal_handler() -> i32 {
    unsafe {
        let mut mask: libc::sigset_t = std::mem::zeroed();
        libc::sigemptyset(&mut mask);
        libc::sigaddset(&mut mask, libc::SIGTERM);
        libc::sigaddset(&mut mask, libc::SIGINT);
        libc::sigaddset(&mut mask, libc::SIGHUP);
        libc::sigprocmask(libc::SIG_BLOCK, &mask, std::ptr::null_mut());
        libc::signalfd(-1, &mask, libc::SFD_NONBLOCK | libc::SFD_CLOEXEC)
    }
}

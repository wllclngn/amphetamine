# amphetamine/triskelion

A Rust wineserver replacement and Steam compatibility layer. triskelion replaces Wine's 26,000-line C wineserver with a ~20,000-line Rust daemon built on `/dev/ntsync` kernel primitives, slab-allocated handle tables, and shared-memory message queues.

Single binary. Single dependency (`libc`). Drops in as a Steam compatibility tool.

```
Steam
  └─ triskelion (launcher + daemon, 1.6 MB Rust binary)
       ├─ Wine client (system Wine 11.5, protocol v930)
       │    └─ /dev/ntsync ioctls (inproc sync, bypasses daemon)
       ├─ steam.exe (Wine builtin, built from Proton steam_helper source)
       │    └─ lsteamclient.dll/.so (built from Proton source, patched for Wine 11.5)
       ├─ EAC bridge (from Proton EasyAntiCheat Runtime, Steam tool 1826330)
       └─ /dev/ntsync (kernel-native NT semaphore/mutex/event)
```

## Game Status

| Game | Status | Notes |
|------|--------|-------|
| Balatro | WORKING | Renders, exits cleanly, Steam API active |
| Dark Souls Remastered | WORKING | Renders, playable |
| Halls of Torment | WORKING | Renders, playable |
| Resident Evil 4 | WORKING | Full launch, D3D12 via VKD3D-Proton |
| Silent Hill 2 | WORKING | UE5, full launch via steam.exe |
| Elden Ring | EAC BLOCKED | Steam auth works, D3D12 works, EAC module mapping fails |

## Replacing Proton

| | Proton | amphetamine |
|---|---|---|
| Launcher | Python (~2,000 lines) | Rust (2,235 lines, compiled) |
| Wineserver | Wine's C wineserver (26,000+ lines) | triskelion (~20,000 lines Rust) |
| Binary count | 3+ (script, wineserver, toolchain) | 1 (1.6 MB) |
| Dependencies | Python 3, runtime libraries | libc |
| Deployment cache | None (re-evaluates every launch) | v4 per-component (wine, dxvk, vkd3d, steam, pe_scan) |
| Prefix setup | Python shutil (readdir + copy per file) | getdents64 (32 KB bulk reads) + hardlinks |
| Wineserver sync | pthread mutexes, kernel locks | `/dev/ntsync` (kernel-native NT sync) |
| Timer precision | Fixed polling interval | timerfd (kernel-precise deadlines) |
| Message passing | All through wineserver socket | Shared-memory SPSC bypass |
| Save data | No protection | Pre-launch snapshot + post-game restore |
| Steam bridge | In-process game loading | Wine builtin steam.exe + lsteamclient (built from Proton source) |
| EAC integration | wine-valve address space model | Bridge DLLs from Proton EAC Runtime + Wine patches |

## Replacing wineserver

| | Wine wineserver | triskelion |
|---|---|---|
| Protocol | 306 opcodes (C, hand-written dispatch) | 306 generated, 237 with handlers |
| Handle tables | Linked list + linear search | HeapSlab (O(1) bump alloc, generation counters) |
| SHM thread slots | N/A | MmapSlab (O(1) alloc/free, LIFO free list) |
| Sync primitives | In-process futex/poll | `/dev/ntsync` kernel ioctls |
| APC delivery | wait_fd + SIGUSR1 | Same: worker interrupt + SIGUSR1 for inproc waits |
| Alert lifecycle | Signal for APC_USER only | Same: daemon never signals alert for system APCs |
| Async I/O | async_set_result in APC destructor | Two-phase: STATUS_KERNEL_APC → prev_apc → deferred event |
| Named pipes | Full async state machine | Create, listen (sync+overlapped), connect, transceive, blocking PIPE_WAIT |
| Registry | On-disk hive files | In-memory tree, persisted on shutdown |
| Process lifecycle | fork + exec tracking | new_process, init_thread, exit events, job objects, completion ports |

## Performance

### Launcher

- **Deployment cache** — Per-component hashes (wine, dxvk, vkd3d, steam, pe_scan). Cache hit skips all file I/O, straight to `wine64`.
- **Prefix setup** — `getdents64` bulk directory reads + hardlinks. Falls back to copy for cross-device.
- **Registry injection** — Template-based from stock `~/.wine` prefix. Amphetamine-specific overrides (display driver, Steam paths) injected on top.

### triskelion, Rust userspace daemon

- **Zero-alloc hot path** — Fixed-size replies use a `[u8; 64]` stack buffer. Accumulation buffers reused via `std::mem::take()`. After warmup, the request path never allocates.
- **Slab allocators** — HeapSlab for handle tables (O(1) alloc, bump-only for Wine fd cache compatibility, LIFO free list with generation counters). MmapSlab for SHM thread slots (O(1) alloc/free, metadata on heap, data in caller-owned mmap).
- **epoll + timerfd** — O(1) fd readiness. Wait deadlines use `timerfd_create(CLOCK_MONOTONIC)` for kernel-precise wakeups. No polling loops.
- **ntsync inproc** — With `WINE_NTSYNC=1`, Wine clients perform synchronization directly via `/dev/ntsync` ioctls, bypassing the daemon entirely for wait/signal operations. The daemon only handles creation, destruction, and APC delivery.
- **Split alert/interrupt** — Thread alerts (returned to Wine for inproc waits) are never signaled by the daemon. A separate auto-reset worker interrupt event wakes daemon-side ntsync worker threads. System APCs delivered via `tgkill(SIGUSR1)` matching stock wineserver's `send_thread_signal`. Wine's SIGUSR1 handler calls `wait_suspend` → `server_select(SELECT_INTERRUPTIBLE)` → daemon delivers the APC inside the signal handler.
- **pending_fd ordering** — All ntsync fd sends go through the worker thread (same thread as reply), guaranteeing fd arrives before the reply that references it.
- **Two-phase APC** — Pipe listen completion: queue APC_ASYNC_IO → STATUS_KERNEL_APC → `invoke_system_apc` (irp_completion writes IOSB) → prev_apc ACK → deferred event signal. Matches stock wineserver's `async_set_result` flow. Prevents rpcrt4 pipe floods.

### triskelion, C23 kernel module



## Anti-Cheat

triskelion itself does not interfere with VAC, EAC, or BattlEye. It runs as a separate native Linux process — communicates with Wine via Unix domain sockets and never appears in the game's memory maps. No game memory modification, no DLL hooking, no import table patching.

EAC integration uses Valve's official Proton EasyAntiCheat Runtime (Steam tool 1826330). Wine patches (011-013) add DLL path injection, load order overrides, and launcher process detection so the bridge DLLs load correctly. The bridge .so files handle all Unix-side EAC communication.

## Install

### Dependencies

- **Linux 6.14+** with `/dev/ntsync` enabled
- **Wine 11.5+** (system Wine)
- **Rust 1.85+** (2024 edition)
- **x86_64-w64-mingw32-gcc** (for lsteamclient + steam.exe stubs)
- **Proton EasyAntiCheat Runtime** (Steam tool, for EAC games)

```bash
# Arch Linux / CachyOS
pacman -S wine rust mingw-w64-gcc
```

```bash
./install.py
```

The installer:
1. Builds triskelion (`cargo build --release`)
2. Deploys system Wine tree (hardlinks)
3. Syncs PE DLLs to game prefixes
4. Downloads + deploys DXVK and VKD3D-Proton
5. Builds lsteamclient.dll/.so + steam.exe from Proton source (patched for Wine 11.5)
6. Applies wine patches (001-013) and builds patched ntdll + kernelbase + win32u
7. Deploys EAC bridge DLLs from Proton EAC Runtime (warns if not installed)

Then select **amphetamine** as the compatibility tool for any game in Steam.

### Verbose mode

```bash
./install.py --verbose    # Enable runtime diagnostics
./install.py --no-verbose # Disable runtime diagnostics
```

Or set `AMPHETAMINE_VERBOSE=1` in Steam launch options: `AMPHETAMINE_VERBOSE=1 %command%`

### Manual build

```bash
cd rust && cargo build --release
cp target/release/triskelion ../triskelion
```

Single dependency: `libc`.

## Architecture

### Launcher

`launcher.rs` (2,235 lines) replaces Proton's Python script.

**Discovery**: Wine from `AMPHETAMINE_WINE_DIR` → Proton Experimental → any Proton → system Wine. Steam from `STEAM_COMPAT_CLIENT_INSTALL_PATH` → `~/.steam/root`.

**Prefix**: `getdents64` bulk reads + hardlinks from Wine's `default_pfx/`. Repair mode fixes broken symlinks from previous deploys.

**DLLs**: DXVK (`d3d11`, `d3d10core`, `d3d9`, `dxgi`), VKD3D-Proton (`d3d12`, `d3d12core`) -- 64-bit and 32-bit. Always deployed defensively (launcher stubs often have zero D3D imports).

**Steam integration**: Game launched through `wine64 C:\windows\system32\steam.exe <game.exe>`. steam.exe (Proton's steam_helper, built from source) creates Win32 events, connects to the running Steam daemon via native steamclient.so, writes `ActiveProcess\PID=0xfffe`, then spawns the game as a child via CreateProcess.

**Save protection**: Pre-launch snapshot of save directories. Post-game restore of files deleted by Steam Cloud sync.

**Logging**: Silent by default. `./install.py --verbose` or `AMPHETAMINE_VERBOSE=1` enables diagnostics. Three tiers: default (`-all`), verbose (`+module,+loaddll,+process,err`), trace (`+server,+timestamp`).

### Daemon

*Quocunque Jeceris Stabit*

**Broker/worker architecture**: Broker thread owns all mutable state. Per-client worker threads do blocking reads and forward to broker via oneshot channels. SCM_RIGHTS fd passing via pending_fd mechanism (worker sends fd before reply, same thread, guaranteed ordering).

**Protocol**: 306 opcodes auto-generated from Wine's `protocol.def` by `build.rs`. Handles Proton's enum divergence (esync/fsync entries shift opcode values vs upstream Wine). 237 opcodes have handlers. Adding a handler = one function in the appropriate event_loop module.

**IPC**: Unix domain socket per thread. SCM_RIGHTS for fd passing. Variable-length replies (VARARG) for startup info, registry, and APC data.

**Event loop**: `epoll_wait` hub. `timerfd` for wait deadlines. Deferred replies for `Select` with timeout. Linger timer (5s) bridges the gap between wineboot exit and game connect.

**Slab allocators**:
- `HeapSlab<T>` -- Handle tables. Bump-only allocation (never reuses slots) for Wine fd cache compatibility. LIFO free list available for non-handle use. Generation counters detect stale references.
- `MmapSlab` -- SHM thread queue slots. O(1) alloc/free. Metadata on heap, data in caller-owned mmap region. Eliminated SHM exhaustion (previously hit 8,192 slots in 5 seconds from Wine's RPC thread pool).

**ntsync lifecycle**:
- Device fd and per-thread alert fd sent to Wine via pending_fd (guaranteed ordering).
- Wine performs inproc waits directly via `/dev/ntsync` ioctls, bypassing the daemon.
- Daemon-side waits use a separate auto-reset worker interrupt event as `alert_fd` -- never touches the thread's inproc alert.
- System APCs (APC_ASYNC_IO) interrupt inproc waits via `tgkill(SIGUSR1)`. Wine's signal handler calls `wait_suspend` → `server_select(SELECT_INTERRUPTIBLE)` → daemon delivers the APC inside the signal handler. This matches stock wineserver's `queue_apc` → `send_thread_signal`.
- Thread alerts are never signaled by the daemon for system APCs. Stock wineserver only signals alerts for `APC_USER` (Wine `thread.c:1339`).

**Named pipes**: Create, listen (synchronous + overlapped), connect, disconnect, transceive, and blocking PIPE_WAIT for pipes that don't exist yet. Two-phase APC handshake for async completion: STATUS_KERNEL_APC → invoke_system_apc → prev_apc → deferred event signal. Completion port integration for rpcrt4 worker threads.

**Registry**: In-memory tree (HashMap of keys, Vec of values). Loaded from prefix `*.reg` files at startup. Symlink resolution for `CurrentControlSet` → `ControlSet001`. Saved on shutdown and on last-user-process exit.

**Process lifecycle**: new_process with handle inheritance, init_first_thread/init_thread with startup info synthesis (curdir + imagepath + cmdline), thread suspend/resume, exit events, job objects with completion port notifications, process idle events (WaitForInputIdle), system PID tracking for shutdown.

**Wine patches**: `triskelion.c` in ntdll intercepts PostMessage/GetMessage via shared-memory SPSC rings, bypasses wineserver for ntsync sync operations. `triskelion_has_posted()` in win32u forces server call path when the ring has messages. Queue pointer bridged via `TEB->glReserved2`.

## Wine Patches

| Patch | Target | Purpose |
|-------|--------|---------|
| 001-ntdll-guard-NtFilterToken-null-deref | ntdll | Null deref guard |
| 002-ntdll-create-process-heap-before-loader-lock | ntdll | Heap before loader lock |
| 003-win32u-soften-user-lock-assert | win32u | Soften USER lock assert |
| 009-ntdll-steamclient-authentication-trampoline | ntdll | Steam auth trampoline (PE + Unix) |
| 010-kernelbase-steam-openprocess-pid-hack | kernelbase | OpenProcess(0xfffe) PID substitution |
| 011-ntdll-eac-runtime-dll-path | ntdll/unix/loader | PROTON_EAC_RUNTIME DLL path injection |
| 012-ntdll-eac-loadorder | ntdll/unix/loadorder | EAC builtin/native load order |
| 013-kernelbase-eac-launcher-detection | kernelbase | PROTON_EAC_LAUNCHER_PROCESS env |

lsteamclient patches (applied to Proton source during build):

| Patch | Purpose |
|-------|---------|
| 004-configure-add-lsteamclient-dll | Register lsteamclient in Wine configure |
| 005-configure-add-steam-helper | Register steam_helper in Wine configure |
| 006-lsteamclient-wine11-api-compat | Path API compat for Wine 11.5 |
| 007-lsteamclient-link-stdcxx | Link libstdc++ |

All wine patches applied automatically by install.py (`sorted(patch_dir.glob("*.patch"))`).

## Project Structure

```
amphetamine/
  install.py                 Build + deploy pipeline (2,387 lines)
  c23/
    steam_bridge.c            Legacy steam.exe (replaced by Proton steam_helper build)
    noassert.c                LD_PRELOAD assertion suppressor
  rust/
    build.rs                  protocol.def codegen (306 opcodes)
    Cargo.toml                Single dep: libc
    include/
      triskelion_shm.h        C header matching Rust shm layout
    src/
      main.rs                 Entry, signal handling, socket path, daemon.pid
      launcher.rs             Proton replacement launcher (2,235 lines)
      broker.rs               Broker thread — mpsc dispatch, epoll hub
      worker.rs               Per-client worker threads — blocking read, oneshot reply
      ipc.rs                  Unix socket IPC, SCM_RIGHTS, pending_fd
      slab.rs                 HeapSlab<T> + MmapSlab — O(1) slab allocators
      objects.rs              HandleTable (HeapSlab), HandleEntry, Process, Thread
      shm.rs                  ShmManager (MmapSlab), desktop_ready atomic
      ntsync.rs               /dev/ntsync ioctl wrapper (semaphore, mutex, event, wait)
      queue.rs                SPSC ring buffer message queues, futex wake
      registry.rs             In-memory registry tree, .reg file parser
      oneshot.rs              Lock-free oneshot channel for worker→broker
      log.rs                  Timestamped logging macros (verbose gating)
      intel.rs                Game intelligence cache (engine detection, opcode coverage)
      pe_scanner.rs           PE header parsing (import table, render API detection)
      event_loop/
        mod.rs                EventLoop struct, field init, shared helpers (1,275 lines)
        sync.rs               Select, APC delivery, events, mutexes, semaphores (1,334 lines)
        window.rs             Window messages, desktop, atoms, clipboard (2,540 lines)
        file_io.rs            Files, mappings, GENERIC_* access mapping (1,258 lines)
        thread.rs             Thread lifecycle, startup info synthesis (856 lines)
        pipes.rs              Named pipes, two-phase APC, PIPE_WAIT (674 lines)
        process.rs            Process lifecycle, handle inheritance (622 lines)
        handles.rs            get_handle_fd, dup_handle, create_file_handle (592 lines)
        completion.rs         Completion ports, jobs, assign_job, timers (544 lines)
        client.rs             Disconnect, cleanup, SHM slot free (348 lines)
        registry_handlers.rs  Registry opcodes (381 lines)
        dispatch.rs           Opcode → handler routing (174 lines)
        token.rs              Security tokens (129 lines)
      profile.rs              strace/perf profiling harness
      packager.rs             Steam compatibility tool packaging
      configure.rs            Wine ./configure generation
      clone.rs                Upstream Wine source cloner
      cli.rs                  CLI argument parsing
      gaming.rs               Gaming DLL/program definitions
      analyze.rs              Wine DLL surface area analysis
      status.rs               Project status reporting
  patches/
    wine/                     Wine patches (001-013), applied by install.py
    wine/dlls/ntdll/unix/triskelion.c       SHM bypass + ntsync shadow table
    wine/dlls/win32u/triskelion_message.c   win32u peek_message integration
  tests/
    iterate.py                Build-deploy-launch iteration loop
    montauk_compare.py        eBPF trace comparison (with montauk)
    triskelion-tests.py       Protocol-level tests
    discover_opcodes.py       Opcode discovery from Wine protocol
    test_package.py           Package integrity tests (47 tests)
```

## Testing

```bash
# Integration: build, deploy, launch, check logs
python3 tests/iterate.py --appid 2379780 --timeout 30

# Package integrity (47 tests)
python3 tests/test_package.py

# With montauk eBPF tracing
python3 tests/montauk_compare.py --appid 2379780
```

### Logs

All logs go to `/tmp/amphetamine/`:

| File | Contents |
|------|----------|
| `daemon.log` | Timestamped daemon events: opcodes, APC delivery, pipe connects, errors |
| `wine_stderr.log` | Wine's stderr (empty unless `--verbose` enabled) |
| `launcher_env.txt` | Full environment snapshot at launch |
| `daemon.pid` | Daemon PID for stale sentinel detection |

### Logging tiers

| Mode | WINEDEBUG | Launcher output |
|------|-----------|-----------------|
| Default | `-all` | `launching: <game.exe>` + errors/warnings only |
| `--verbose` | `+module,+loaddll,+process,err` | Full diagnostics |
| `AMPHETAMINE_TRACE_OPCODES` | `+server,+timestamp` | Full wineserver protocol dump |

### Debugging

```bash
# Enable verbose diagnostics
./install.py --verbose
# Or per-launch: AMPHETAMINE_VERBOSE=1 %command%

# Enable opcode tracing
touch /tmp/amphetamine/TRACE_OPCODES

# Force full redeploy
find ~/.steam/root/steamapps/compatdata/ -name ".triskelion_deployed" -delete

# Nuke a game prefix
rm -rf ~/.steam/root/steamapps/compatdata/<app_id>/pfx
```

## Tooling

amphetamine is a multi-mode binary:

```bash
triskelion server                           # wineserver daemon (started by Wine)
triskelion <verb> <exe>                     # Proton-compatible launcher (started by Steam)
triskelion package <wine_dir>               # package as Steam compatibility tool
triskelion configure <wine_dir> [--execute] # Wine ./configure with --disable-* flags
triskelion clone                            # clone upstream Wine source
triskelion status                           # project status
triskelion analyze                          # Wine DLL surface area analysis
triskelion profile <app_id>                 # strace profiling
triskelion profile-attach                   # attach to running game
triskelion profile-compare                  # compare profile outputs
triskelion profile-opcodes                  # analyze opcode traces
```

## License

GPL-2.0

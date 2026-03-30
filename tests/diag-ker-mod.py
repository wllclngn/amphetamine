#!/usr/bin/env python3
"""Triskelion kernel module + relay diagnostic launcher.

Tests the /dev/triskelion ioctl interface, relay queue, and game launching.

Usage:
  python3 diag-ker-mod.py                     # preflight + smoke tests
  python3 diag-ker-mod.py --smoke             # ioctl smoke tests only
  python3 diag-ker-mod.py --relay             # relay roundtrip test (fork)
  python3 diag-ker-mod.py [app_id]            # launch game with diagnostics
  python3 diag-ker-mod.py [app_id] --timeout N
  python3 diag-ker-mod.py [app_id] --winedebug "+process,+module"
  python3 diag-ker-mod.py --decode-startup <hex_bytes>

Default app_id: 2379780 (Balatro)
"""

import os, sys, subprocess, time, signal, glob, struct, ctypes, ctypes.util
import fcntl, array, errno

# ── Constants ──────────────────────────────────────────────────────────

DEVICE = "/dev/triskelion"
COMPAT_DIR = os.path.expanduser("~/.local/share/Steam/compatibilitytools.d/quark")
PROTON = os.path.join(COMPAT_DIR, "proton")
STEAM_DIR = os.path.expanduser("~/.local/share/Steam")
DIAG_TIMEOUT = 8
MAX_TIMEOUT = 30

# Ioctl magic and numbers (must match triskelion.h)
TRISKELION_MAGIC = ord('T')

# _IOWR(magic, nr, size) = direction(3) | size(14) | magic(8) | nr(8)
def _IOC(dir, magic, nr, size):
    return (dir << 30) | (size << 16) | (magic << 8) | nr
_IOC_WRITE = 1
_IOC_READ = 2
def _IOW(magic, nr, size):  return _IOC(_IOC_WRITE, magic, nr, size)
def _IOR(magic, nr, size):  return _IOC(_IOC_READ, magic, nr, size)
def _IOWR(magic, nr, size): return _IOC(_IOC_READ | _IOC_WRITE, magic, nr, size)

# Struct sizes
SEM_ARGS_SIZE = 16   # handle(4) + count(4) + max_count(4) + prev_count(4)
EVENT_ARGS_SIZE = 16 # handle(4) + manual_reset(4) + initial_state(4) + prev_state(4)
HANDLE_SIZE = 4
DAEMON_ARGS_SIZE = 4 # version(4)

IOC_CREATE_SEM    = _IOWR(TRISKELION_MAGIC, 0x10, SEM_ARGS_SIZE)
IOC_CREATE_EVENT  = _IOWR(TRISKELION_MAGIC, 0x12, EVENT_ARGS_SIZE)
IOC_CLOSE         = _IOW(TRISKELION_MAGIC, 0x40, HANDLE_SIZE)
IOC_REGISTER_DAEMON = _IOW(TRISKELION_MAGIC, 0x60, DAEMON_ARGS_SIZE)

# Relay args struct size
# request(8) + reply(8) + request_size(4) + reply_max(4) + reply_size(4) +
# flags(4) + fds[4](16) + fd_count_in(4) + fd_count_out(4) = 56
RELAY_ARGS_SIZE = 56
IOC_RELAY = _IOWR(TRISKELION_MAGIC, 0x50, RELAY_ARGS_SIZE)

# Relay header size (for read/write)
# request_id(8) + client_id(4) + opcode(4) + payload_size(4) + flags(4) +
# fd_count(4) + fds[4](16) = 44
RELAY_HEADER_SIZE = 44

# Game database
GAMES = {
    "2379780": ("Balatro", "Balatro.exe"),
    "2218750": ("Halls of Torment", "Halls of Torment.exe"),
}

# ── Color output ───────────────────────────────────────────────────────

def ok(msg):   print(f"  \033[32m✓\033[0m {msg}")
def fail(msg): print(f"  \033[31m✗\033[0m {msg}")
def info(msg): print(f"  \033[36m•\033[0m {msg}")
def warn(msg): print(f"  \033[33m!\033[0m {msg}")
def header(msg):
    print(f"\n\033[1m{'─' * 60}\033[0m")
    print(f"\033[1m  {msg}\033[0m")
    print(f"\033[1m{'─' * 60}\033[0m")

# ── Startup info decoder ─────────────────────────────────────────────
#
# Wine's startup_info_data struct (protocol.def, 24 u32 fields = 96 bytes):
#   debug_flags, console_flags, console, hstdin, hstdout, hstderr,
#   x, y, xsize, ysize, xchars, ychars, attribute, flags, show,
#   process_group_id, curdir_len, dllpath_len, imagepath_len, cmdline_len,
#   title_len, desktop_len, shellinfo_len, runtime_len
# Followed by contiguous wide-string data:
#   curdir, dllpath, imagepath, cmdline, title, desktop, shellinfo, runtime

STARTUP_INFO_FIELDS = [
    "debug_flags", "console_flags", "console", "hstdin", "hstdout", "hstderr",
    "x", "y", "xsize", "ysize", "xchars", "ychars", "attribute", "flags", "show",
    "process_group_id", "curdir_len", "dllpath_len", "imagepath_len", "cmdline_len",
    "title_len", "desktop_len", "shellinfo_len", "runtime_len",
]
STARTUP_INFO_STRUCT_SIZE = len(STARTUP_INFO_FIELDS) * 4  # 96 bytes

STRING_FIELDS = [
    "curdir", "dllpath", "imagepath", "cmdline",
    "title", "desktop", "shellinfo", "runtime",
]

def decode_startup_info(data, label=""):
    """Decode and print Wine startup_info_data from raw bytes."""
    prefix = f"  [{label}] " if label else "  "

    if len(data) < STARTUP_INFO_STRUCT_SIZE:
        print(f"{prefix}Too short ({len(data)} bytes, need {STARTUP_INFO_STRUCT_SIZE})")
        return

    # Unpack the 24 u32 fields
    values = struct.unpack_from(f"<{len(STARTUP_INFO_FIELDS)}I", data)
    fields = dict(zip(STARTUP_INFO_FIELDS, values))

    # Print non-zero fixed fields
    print(f"{prefix}startup_info_data ({len(data)} bytes total):")
    for name in STARTUP_INFO_FIELDS[:16]:
        v = fields[name]
        if v != 0:
            print(f"{prefix}  {name} = {v} (0x{v:x})")

    # Print string length fields
    for name in STARTUP_INFO_FIELDS[16:]:
        v = fields[name]
        print(f"{prefix}  {name} = {v}")

    # Decode wide strings from after the struct header
    offset = STARTUP_INFO_STRUCT_SIZE
    for sname in STRING_FIELDS:
        length = fields.get(f"{sname}_len", 0)
        if length == 0:
            continue
        if offset + length > len(data):
            print(f"{prefix}  {sname}: TRUNCATED (need {length} at offset {offset}, have {len(data)})")
            break
        try:
            text = data[offset:offset + length].decode("utf-16-le").rstrip("\x00")
        except:
            text = f"<decode error: {data[offset:offset+min(length,32)].hex()}>"
        print(f"{prefix}  {sname} = \"{text}\"")
        offset += length

    remaining = len(data) - offset
    if remaining > 0:
        print(f"{prefix}  environment: {remaining} bytes after strings")

def decode_startup_hex(hex_str):
    """Decode startup info from a hex string (from daemon logs)."""
    hex_str = hex_str.replace(" ", "").replace("\n", "")
    data = bytes.fromhex(hex_str)
    header("STARTUP INFO DECODE")
    decode_startup_info(data)

# ── Preflight checks ──────────────────────────────────────────────────

def check_preflight():
    header("PREFLIGHT CHECKS")
    passed = 0
    total = 0

    # 1. /dev/triskelion exists
    total += 1
    if os.path.exists(DEVICE):
        ok(f"{DEVICE} exists")
        passed += 1
    else:
        fail(f"{DEVICE} not found — kernel module not loaded")
        info("Fix: sudo insmod /path/to/triskelion_kmod.ko")
        info("  or: python3 install.py (builds + installs)")

    # 2. Module loaded
    total += 1
    result = subprocess.run(["lsmod"], capture_output=True, text=True)
    if "triskelion_kmod" in result.stdout:
        # Extract module info
        for line in result.stdout.splitlines():
            if "triskelion_kmod" in line:
                ok(f"Module loaded: {line.strip()}")
                break
        passed += 1
    else:
        fail("triskelion_kmod not in lsmod")

    # 3. Module params
    total += 1
    param_dir = "/sys/module/triskelion_kmod/parameters"
    if os.path.isdir(param_dir):
        params = {}
        for p in os.listdir(param_dir):
            try:
                with open(os.path.join(param_dir, p)) as f:
                    params[p] = f.read().strip()
            except:
                pass
        ok(f"Module params: {params}")
        passed += 1
    else:
        warn("Cannot read module parameters (module not loaded?)")

    # 4. Daemon binary
    total += 1
    if os.path.exists(PROTON):
        size = os.path.getsize(PROTON)
        ok(f"Daemon binary: {PROTON} ({size // 1024} KB)")
        passed += 1
    else:
        fail(f"Daemon binary not found: {PROTON}")
        info("Fix: cargo build --release -p triskelion && cp target/release/triskelion <compat_dir>/proton")

    # 5. Device permissions
    total += 1
    if os.path.exists(DEVICE):
        try:
            fd = os.open(DEVICE, os.O_RDWR)
            os.close(fd)
            ok(f"{DEVICE} is readable+writable")
            passed += 1
        except PermissionError:
            fail(f"{DEVICE} permission denied (mode 0666 expected)")
        except Exception as e:
            fail(f"{DEVICE} open failed: {e}")
    else:
        fail(f"Cannot test {DEVICE} (not found)")

    print(f"\n  Preflight: {passed}/{total} passed")
    return passed == total

# ── Ioctl smoke tests ─────────────────────────────────────────────────

def test_smoke():
    header("IOCTL SMOKE TESTS")
    passed = 0
    total = 0

    if not os.path.exists(DEVICE):
        fail(f"{DEVICE} not found — skipping smoke tests")
        return False

    try:
        fd = os.open(DEVICE, os.O_RDWR)
    except Exception as e:
        fail(f"Cannot open {DEVICE}: {e}")
        return False

    # Test 1: CREATE_SEM
    total += 1
    try:
        # struct triskelion_sem_args: handle(u32) + count(u32) + max_count(u32) + prev_count(u32)
        args = struct.pack("IIII", 0, 1, 10, 0)  # initial=1, max=10
        buf = bytearray(args)
        fcntl.ioctl(fd, IOC_CREATE_SEM, buf)
        handle, count, max_count, prev = struct.unpack("IIII", buf)
        if handle > 0:
            ok(f"CREATE_SEM: handle={handle} (count={count}, max={max_count})")
            passed += 1

            # Test 2: CLOSE
            total += 1
            try:
                close_buf = struct.pack("I", handle)
                close_arr = bytearray(close_buf)
                fcntl.ioctl(fd, IOC_CLOSE, close_arr)
                ok(f"CLOSE: handle={handle} closed")
                passed += 1
            except Exception as e:
                fail(f"CLOSE failed: {e}")
        else:
            fail(f"CREATE_SEM returned invalid handle: {handle}")
    except Exception as e:
        fail(f"CREATE_SEM failed: {e}")

    # Test 3: CREATE_EVENT
    total += 1
    try:
        args = struct.pack("IIII", 0, 1, 0, 0)  # manual_reset=1, initial_state=0
        buf = bytearray(args)
        fcntl.ioctl(fd, IOC_CREATE_EVENT, buf)
        handle, manual, initial, prev = struct.unpack("IIII", buf)
        if handle > 0:
            ok(f"CREATE_EVENT: handle={handle} (manual={manual})")
            # Close it
            close_buf = bytearray(struct.pack("I", handle))
            fcntl.ioctl(fd, IOC_CLOSE, close_buf)
            passed += 1
        else:
            fail(f"CREATE_EVENT returned invalid handle")
    except Exception as e:
        fail(f"CREATE_EVENT failed: {e}")

    # Test 4: REGISTER_DAEMON
    total += 1
    try:
        args = struct.pack("I", 930)  # version
        buf = bytearray(args)
        fcntl.ioctl(fd, IOC_REGISTER_DAEMON, buf)
        ok("REGISTER_DAEMON: registered as daemon (version=930)")
        passed += 1
    except Exception as e:
        fail(f"REGISTER_DAEMON failed: {e}")

    os.close(fd)

    print(f"\n  Smoke tests: {passed}/{total} passed")
    return passed == total

# ── Relay roundtrip test ──────────────────────────────────────────────

def test_relay():
    header("RELAY ROUNDTRIP TEST")

    if not os.path.exists(DEVICE):
        fail(f"{DEVICE} not found — skipping relay test")
        return False

    # Fork: child = daemon, parent = Wine client
    child_pid = os.fork()

    if child_pid == 0:
        # === CHILD: daemon ===
        try:
            fd = os.open(DEVICE, os.O_RDWR)

            # Register as daemon
            args = struct.pack("I", 930)
            buf = bytearray(args)
            fcntl.ioctl(fd, IOC_REGISTER_DAEMON, buf)

            # Read one relay request
            read_buf = os.read(fd, 4096)
            if len(read_buf) < RELAY_HEADER_SIZE:
                os._exit(1)

            # Parse relay header
            hdr = struct.unpack_from("<QIIIII4i", read_buf[:RELAY_HEADER_SIZE])
            request_id = hdr[0]
            payload_size = hdr[3]

            # Echo back: write reply header + 64-byte stub reply
            reply_data = bytearray(64)  # zeroed reply
            # Set reply header error = 0 (STATUS_SUCCESS)
            struct.pack_into("<I", reply_data, 0, 0)  # error = 0
            struct.pack_into("<I", reply_data, 4, 0)  # reply_size = 0

            reply_hdr = struct.pack("<QIIIII4i",
                request_id,     # request_id (Q)
                0,              # client_id (I, unused in reply)
                0,              # opcode (I, unused in reply)
                64,             # payload_size (I)
                0,              # flags (I)
                0,              # fd_count (I)
                -1, -1, -1, -1, # fds (4i, unused)
            )
            os.write(fd, reply_hdr + bytes(reply_data))

            os.close(fd)
            os._exit(0)
        except Exception as e:
            print(f"  Daemon child error: {e}", file=sys.stderr)
            os._exit(1)
    else:
        # === PARENT: Wine client ===
        time.sleep(0.2)  # Let child register

        try:
            fd = os.open(DEVICE, os.O_RDWR)

            # Build a fake Wine protocol request (64 bytes, opcode=999)
            request = bytearray(64)
            struct.pack_into("<i", request, 0, 999)  # opcode
            struct.pack_into("<I", request, 4, 0)    # request_size (no vararg)

            # Pack relay args using ctypes for pointer passing
            # We need to pass pointers, which ioctl doesn't support directly
            # via struct.pack. Instead, use ctypes.

            # Actually, the ioctl RELAY takes pointers in the struct.
            # Let's use ctypes for proper pointer handling.
            request_buf = (ctypes.c_ubyte * 64)(*request)
            reply_buf = (ctypes.c_ubyte * 64)()

            class RelayArgs(ctypes.Structure):
                _fields_ = [
                    ("request", ctypes.c_void_p),
                    ("reply", ctypes.c_void_p),
                    ("request_size", ctypes.c_uint32),
                    ("reply_max", ctypes.c_uint32),
                    ("reply_size", ctypes.c_uint32),
                    ("flags", ctypes.c_uint32),
                    ("fds", ctypes.c_int32 * 4),
                    ("fd_count_in", ctypes.c_uint32),
                    ("fd_count_out", ctypes.c_uint32),
                ]

            args = RelayArgs()
            args.request = ctypes.addressof(request_buf)
            args.reply = ctypes.addressof(reply_buf)
            args.request_size = 64
            args.reply_max = 64
            args.reply_size = 0
            args.flags = 0
            args.fd_count_in = 0
            args.fd_count_out = 0

            ret = fcntl.ioctl(fd, IOC_RELAY, args)

            # Check reply
            error = struct.unpack_from("<I", bytes(reply_buf))[0]
            if error == 0:
                ok(f"RELAY roundtrip: opcode=999 → STATUS_SUCCESS (reply_size={args.reply_size})")
            else:
                ok(f"RELAY roundtrip: opcode=999 → error=0x{error:08x} (reply_size={args.reply_size})")

            os.close(fd)

            # Reap child
            _, status = os.waitpid(child_pid, 0)
            if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
                ok("Daemon child exited cleanly")
            else:
                warn(f"Daemon child exit status: {status}")

            return True

        except Exception as e:
            fail(f"RELAY roundtrip failed: {e}")
            os.waitpid(child_pid, 0)
            return False

# ── Game launch diagnostics (carried forward from original) ────────────

def kill_stale():
    for pat in ["compatibilitytools.d/quark/proton",
                "quark/lib/wine"]:
        subprocess.run(["pkill", "-9", "-f", pat], capture_output=True)
    time.sleep(0.5)

def find_game(app_id):
    if app_id in GAMES:
        name, exe = GAMES[app_id]
        base = os.path.join(STEAM_DIR, "steamapps/common", name)
        return os.path.join(base, exe)
    common = os.path.join(STEAM_DIR, "steamapps/common")
    for d in os.listdir(common):
        for f in os.listdir(os.path.join(common, d)):
            if f.endswith(".exe"):
                return os.path.join(common, d, f)
    return None

def read_proc(pid, field):
    try:
        with open(f"/proc/{pid}/{field}", "r") as f:
            return f.read().strip()
    except:
        return None

def dump_fds(pid):
    fd_dir = f"/proc/{pid}/fd"
    try:
        fds = []
        for name in sorted(os.listdir(fd_dir), key=lambda x: int(x)):
            try:
                target = os.readlink(f"{fd_dir}/{name}")
                fds.append(f"  fd {name:>3s} -> {target}")
            except:
                pass
        return "\n".join(fds)
    except:
        return "  (cannot read)"

def find_procs():
    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    procs = {}
    for line in result.stdout.splitlines():
        if "quark/proton" in line and "grep" not in line:
            parts = line.split()
            pid = int(parts[1])
            if "waitforexitandrun" in line:
                procs[pid] = ("launcher", line)
            else:
                procs[pid] = ("server", line)
        elif "quark/lib/wine" in line and "grep" not in line:
            parts = line.split()
            pid = int(parts[1])
            procs[pid] = ("wine", line)
    return procs

def dump_diagnostics(procs):
    header("PROCESS DIAGNOSTICS")
    for pid, (role, cmdline) in sorted(procs.items()):
        status = read_proc(pid, "status")
        if not status:
            print(f"\n[{role}] pid={pid} -- DEAD")
            continue
        wchan = read_proc(pid, "wchan")
        state_line = [l for l in status.splitlines() if l.startswith("State:")]
        state = state_line[0] if state_line else "unknown"
        print(f"\n[{role}] pid={pid}")
        print(f"  {state}")
        print(f"  wchan: {wchan}")

        # Check for /dev/triskelion fds
        fd_text = dump_fds(pid)
        triskelion_fds = [l for l in fd_text.splitlines() if "triskelion" in l]
        if triskelion_fds:
            print(f"  /dev/triskelion fds:")
            for l in triskelion_fds:
                print(f"  {l}")

        print(f"  fds:")
        print(fd_text)

        syscall = read_proc(pid, "syscall")
        if syscall:
            print(f"  syscall: {syscall[:80]}")

def kill_all(procs, proc):
    for pid in procs:
        try: os.kill(pid, signal.SIGTERM)
        except ProcessLookupError: pass
    try: proc.terminate()
    except: pass
    try: proc.wait(timeout=2)
    except subprocess.TimeoutExpired: pass
    for pid in procs:
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass
    try:
        proc.kill()
        proc.wait(timeout=2)
    except: pass

def launch_game(app_id, max_timeout, winedebug=None):
    game_exe = find_game(app_id)
    if not game_exe:
        fail(f"Cannot find game for app_id={app_id}")
        return

    header("GAME LAUNCH")
    info(f"Game: {game_exe}")
    info(f"Proton: {PROTON}")

    kill_stale()

    # Clear old relay debug log
    try: os.unlink("/tmp/triskelion_wine_relay.log")
    except FileNotFoundError: pass

    pfx = os.path.expanduser(f"~/.steam/root/steamapps/compatdata/{app_id}/pfx")
    wine_bin = os.path.join(COMPAT_DIR, "lib/wine/x86_64-unix")

    # Create a wineserver wrapper that logs invocation args for debugging,
    # then execs the real triskelion binary.
    wrapper_path = "/tmp/triskelion_wineserver_wrapper.sh"
    with open(wrapper_path, "w") as f:
        f.write(f"""#!/bin/sh
echo "[wrapper] WINESERVER invoked: $0 $@" >&2
echo "[wrapper] argv: $@" >&2
exec "{PROTON}" "$@"
""")
    os.chmod(wrapper_path, 0o755)

    env = os.environ.copy()
    env.update({
        "WINEPREFIX": pfx,
        "WINESERVER": wrapper_path,
        "WINEDEBUG": winedebug or "+server,+timestamp",
        "SteamAppId": app_id,
        "SteamGameId": app_id,
        "WINEFSYNC": "1",
        "WINEESYNC": "0",
        "PATH": f"{wine_bin}:{env.get('PATH', '')}",
        "LD_LIBRARY_PATH": os.path.join(COMPAT_DIR, "lib/wine/x86_64-unix"),
    })

    wine64 = os.path.join(wine_bin, "wine")
    if not os.path.exists(wine64):
        wine64 = os.path.join(wine_bin, "wine64")

    wine_entry = os.path.join(COMPAT_DIR, "bin", "wine")
    if not os.path.exists(wine_entry):
        wine_entry = os.path.join(COMPAT_DIR, "bin", "wine64")
    if os.path.exists(wine_entry):
        env["WINELOADER"] = wine_entry

    info(f"Wine: {wine64}")
    info(f"WINELOADER: {env.get('WINELOADER', '(not set)')}")
    info(f"Prefix: {pfx}")
    info(f"Diagnostics at {DIAG_TIMEOUT}s, hard kill at {max_timeout}s")
    print("-" * 60)

    log_file = f"/tmp/triskelion_diag_{int(time.time())}.log"
    proc = subprocess.Popen(
        [wine64, "c:\\windows\\system32\\steam.exe", game_exe],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    start = time.time()
    time.sleep(DIAG_TIMEOUT)

    procs = find_procs()
    info(f"Found {len(procs)} processes")

    # Check stderr
    try:
        stderr_data = proc.stderr.read1(65536) if hasattr(proc.stderr, 'read1') else b""
    except:
        stderr_data = b""

    # Cached stderr log
    stderr_logs = sorted(glob.glob(os.path.expanduser("~/.cache/quark/stderr-*.log")))
    cached_stderr = ""
    if stderr_logs:
        try:
            with open(stderr_logs[-1], "r") as f:
                cached_stderr = f.read()
        except:
            pass

    dump_diagnostics(procs)

    # /dev/triskelion usage check
    header("TRISKELION DEVICE STATUS")
    triskelion_users = 0
    for pid, (role, _) in sorted(procs.items()):
        fd_text = dump_fds(pid)
        if "triskelion" in fd_text:
            triskelion_users += 1
    if triskelion_users > 0:
        ok(f"{triskelion_users} processes have /dev/triskelion open")
    else:
        warn("No processes have /dev/triskelion open (relay not active yet?)")

    # Check dmesg for triskelion messages
    dmesg = subprocess.run(["dmesg"], capture_output=True, text=True)
    triskelion_msgs = [l for l in dmesg.stdout.splitlines() if "triskelion" in l.lower()]
    if triskelion_msgs:
        header("DMESG (triskelion)")
        for line in triskelion_msgs[-10:]:
            print(f"  {line}")

    # Stderr output
    header("STDERR")
    if cached_stderr:
        print("  From cached log:")
        for line in cached_stderr.strip().splitlines()[-40:]:
            print(f"  {line}")
    if stderr_data:
        print("\n  Direct stderr:")
        text = stderr_data.decode("utf-8", errors="replace")
        for line in text.splitlines()[-60:]:
            print(f"  {line}")
    if not cached_stderr and not stderr_data:
        info("(no stderr captured)")

    # Auto-decode startup info from daemon logs
    all_stderr = (cached_stderr or "") + (stderr_data.decode("utf-8", errors="replace") if stderr_data else "")
    _try_decode_startup_from_logs(all_stderr)

    # Child process exit tracking
    _analyze_child_exits(all_stderr)

    # Wine-side relay debug log (written to file since child stderr is invisible)
    RELAY_LOG = "/tmp/triskelion_wine_relay.log"
    if os.path.exists(RELAY_LOG):
        header("WINE RELAY DEBUG LOG")
        try:
            with open(RELAY_LOG, "r") as f:
                relay_text = f.read()
            for line in relay_text.strip().splitlines():
                print(f"  {line}")
            if not relay_text.strip():
                info("(empty)")
        except Exception as e:
            warn(f"Could not read {RELAY_LOG}: {e}")
    else:
        info(f"No Wine relay log at {RELAY_LOG}")

    # Wait or kill
    elapsed = time.time() - start
    remaining = max_timeout - elapsed
    if remaining > 0 and proc.poll() is None:
        info(f"Waiting {remaining:.0f}s more (Ctrl+C or wait for auto-kill)...")
        try:
            proc.wait(timeout=remaining)
            ok("Process exited on its own.")
        except subprocess.TimeoutExpired:
            warn(f"{max_timeout}s reached — killing all processes.")
        except KeyboardInterrupt:
            warn("Ctrl+C — killing all processes.")

    # Save log
    with open(log_file, "w") as f:
        f.write(f"=== CACHED STDERR ===\n{cached_stderr}\n")
        if stderr_data:
            f.write(f"=== DIRECT STDERR ===\n{stderr_data.decode('utf-8', errors='replace')}\n")

    info(f"Full log saved to: {log_file}")

    # Clean kill
    info("Killing processes...")
    kill_all(procs, proc)
    ok("Done.")

# ── Log analysis helpers ──────────────────────────────────────────────

import re

def _try_decode_startup_from_logs(text):
    """Extract and decode startup_info hex dumps from daemon stderr."""
    # Look for "[triskelion] get_startup_info data[..N]: XX XX XX ..."
    pattern = r"\[triskelion\] get_startup_info data\[\.\.\d+\]: ([0-9a-f ]+)"
    matches = re.findall(pattern, text)
    if not matches:
        return

    # Also grab the header line for context
    hdr_pattern = r"\[triskelion\] get_startup_info: (.+)"
    hdr_matches = re.findall(hdr_pattern, text)

    header("STARTUP INFO DECODE")
    for i, hex_str in enumerate(matches):
        if i < len(hdr_matches):
            info(hdr_matches[i])
        try:
            data = bytes.fromhex(hex_str.replace(" ", ""))
            decode_startup_info(data, f"reply #{i+1}")
        except Exception as e:
            warn(f"Decode failed: {e}")

    # Also check if new_process logged raw info data
    info_pattern = r"\[triskelion\] new_process: info\[0\.\.80\]: ([0-9a-f ]+)"
    info_matches = re.findall(info_pattern, text)
    if info_matches:
        for i, hex_str in enumerate(info_matches):
            try:
                data = bytes.fromhex(hex_str.replace(" ", ""))
                print(f"\n  [new_process #{i+1}] First 80 bytes of stored startup info:")
                decode_startup_info(data, f"new_process #{i+1}")
            except Exception as e:
                warn(f"Decode failed: {e}")

def _analyze_child_exits(text):
    """Analyze daemon logs for child process lifecycle issues."""
    lines = text.splitlines()

    # Count key operations
    init_count = sum(1 for l in lines if "op=InitFirstThread" in l and "req fd=" in l)
    new_proc_count = sum(1 for l in lines if "op=NewProcess" in l and "req fd=" in l)
    startup_count = sum(1 for l in lines if "op=GetStartupInfo" in l)
    disconnect_count = sum(1 for l in lines if "n=0 fds_before=" in l)
    relay_count = sum(1 for l in lines if "relay req id=" in l)
    send_fd_ok = sum(1 for l in lines if "send_fd returned" in l and "returned -1" not in l)
    send_fd_fail = sum(1 for l in lines if "send_fd returned -1" in l)

    if init_count == 0 and relay_count == 0:
        return  # No daemon activity

    header("PROCESS LIFECYCLE ANALYSIS")
    info(f"InitFirstThread: {init_count}")
    info(f"NewProcess: {new_proc_count}  (send_fd ok={send_fd_ok} fail={send_fd_fail})")
    info(f"GetStartupInfo: {startup_count}")
    info(f"Relay requests: {relay_count}")
    info(f"Client disconnects: {disconnect_count}")

    # Relay opcode breakdown
    relay_ops = re.findall(r"relay req id=\d+ client=\d+ op=(\w+)", text)
    if relay_ops:
        from collections import Counter
        counts = Counter(relay_ops)
        top = counts.most_common(10)
        info(f"Relay opcodes: {', '.join(f'{op}={n}' for op, n in top)}")

    # Diagnose common issues
    if new_proc_count > 5 and startup_count == 0:
        warn("INFINITE PROCESS CHAIN: many NewProcess but no GetStartupInfo")
        warn("  → init_first_thread_reply.info_size is likely 0 for child processes")
    elif new_proc_count > 0 and startup_count > 0 and disconnect_count > startup_count:
        warn("Child processes dying after GetStartupInfo")
        warn("  → check startup_info data (imagepath, cmdline) or env_size underflow")
    elif send_fd_fail > 0:
        warn(f"{send_fd_fail} send_fd failures — child processes can't receive request pipes")

    # Check for wine error messages
    wine_errors = [l for l in lines if "wine:" in l.lower() and ("could not" in l.lower() or "failed" in l.lower())]
    if wine_errors:
        info("Wine errors:")
        for e in wine_errors[:5]:
            print(f"    {e.strip()}")

# ── Post-mortem suggestions ────────────────────────────────────────────

def post_mortem():
    header("POST-MORTEM CHECKLIST")
    issues = []

    if not os.path.exists(DEVICE):
        issues.append(("kmod not loaded",
                       "sudo insmod triskelion_kmod.ko  OR  python3 install.py"))

    if os.path.exists(DEVICE):
        # Check if daemon is registered by trying to read (should block or return -EINVAL)
        try:
            fd = os.open(DEVICE, os.O_RDWR | os.O_NONBLOCK)
            try:
                os.read(fd, 1)
            except OSError as e:
                if e.errno == 22:  # EINVAL = not a daemon
                    pass
                elif e.errno == 11:  # EAGAIN = daemon but nothing pending
                    pass
            os.close(fd)
        except:
            pass

    if not os.path.exists(PROTON):
        issues.append(("daemon binary missing",
                       f"Build: cargo build --release -p triskelion"))

    if issues:
        for problem, fix in issues:
            fail(f"{problem}")
            info(f"  → {fix}")
    else:
        ok("No obvious issues detected")

# ── Main ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    max_timeout = MAX_TIMEOUT
    app_id = None
    do_smoke = False
    do_relay = False
    do_launch = False

    winedebug = None

    i = 0
    while i < len(args):
        if args[i] == "--smoke":
            do_smoke = True
        elif args[i] == "--relay":
            do_relay = True
        elif args[i] == "--timeout" and i + 1 < len(args):
            max_timeout = int(args[i + 1])
            i += 1
        elif args[i] == "--winedebug" and i + 1 < len(args):
            winedebug = args[i + 1]
            i += 1
        elif args[i] == "--decode-startup" and i + 1 < len(args):
            decode_startup_hex(args[i + 1])
            sys.exit(0)
        elif args[i].isdigit():
            app_id = args[i]
            do_launch = True
        i += 1

    # Default: preflight + smoke
    if not do_smoke and not do_relay and not do_launch:
        do_smoke = True

    print(f"\033[1mtriskelion diagnostic tool v0.2.0\033[0m")

    check_preflight()

    if do_smoke:
        test_smoke()

    if do_relay:
        test_relay()

    if do_launch:
        if not app_id:
            app_id = "2379780"
        launch_game(app_id, max_timeout, winedebug=winedebug)

    post_mortem()

if __name__ == "__main__":
    main()

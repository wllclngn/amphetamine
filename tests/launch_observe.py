#!/usr/bin/env python3
"""Launch amphetamine and observe: prefix setup, services, pipes, window creation.

Usage:
    python3 tests/launch_observe.py [--timeout 30]
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from util import kill_amphetamine_processes, STEAM_ROOT

TIMEOUT = int(sys.argv[sys.argv.index("--timeout") + 1]) if "--timeout" in sys.argv else 30
PROTON_BIN = STEAM_ROOT / "compatibilitytools.d/amphetamine/proton"
GAME_EXE = STEAM_ROOT / "steamapps/common/Balatro/Balatro.exe"
COMPAT_DATA = STEAM_ROOT / "steamapps/compatdata/2379780"

WINE_STDERR = Path("/tmp/amphetamine/wine_stderr.log")
DAEMON_LOG = Path("/tmp/amphetamine/daemon.log")

def main():
    # Kill stale
    kill_amphetamine_processes()

    env = os.environ.copy()
    env["STEAM_COMPAT_DATA_PATH"] = str(COMPAT_DATA)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(STEAM_ROOT)
    env["SteamAppId"] = "2379780"
    env["SteamGameId"] = "2379780"

    print(f"Launching amphetamine (timeout={TIMEOUT}s)...")
    print(f"  Game: {GAME_EXE}")
    print(f"  Prefix: {COMPAT_DATA / 'pfx'}")
    pfx_fresh = not (COMPAT_DATA / "pfx" / "system.reg").exists()
    print(f"  Fresh prefix: {pfx_fresh}")
    print()

    proc = subprocess.Popen(
        [str(PROTON_BIN), "run", str(GAME_EXE)],
        env=env, cwd="/tmp",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    t0 = time.monotonic()
    launcher_exited = False
    for tick in range(TIMEOUT):
        time.sleep(1)
        if not launcher_exited and proc.poll() is not None:
            launcher_exited = True
            # Launcher exits quickly — Wine/daemon keep running. Don't break.
        # Progress
        wine_lines = 0
        daemon_lines = 0
        if WINE_STDERR.exists():
            wine_lines = sum(1 for _ in open(WINE_STDERR, errors="replace"))
        if DAEMON_LOG.exists():
            daemon_lines = sum(1 for _ in open(DAEMON_LOG, errors="replace"))

        if tick % 5 == 4:
            # Check key milestones
            milestones = []
            if DAEMON_LOG.exists():
                text = DAEMON_LOG.read_text(errors="replace")
                if "create_named_pipe" in text: milestones.append("PIPE_CREATE")
                if "FSCTL_PIPE_LISTEN" in text: milestones.append("PIPE_LISTEN")
                if "pipe connect" in text: milestones.append("PIPE_CONNECT")
                if "KERNEL_APC" in text: milestones.append("APC_DELIVERY")
                if "captured cookie" in text: milestones.append("COOKIE_CAPTURE")
                if "winedevice" in text.lower(): milestones.append("WINEDEVICE")
                if "plugplay" in text.lower(): milestones.append("PLUGPLAY")
                if "wglSwapBuffers" in text or (WINE_STDERR.exists() and "wglSwapBuffers" in WINE_STDERR.read_text(errors="replace")): milestones.append("RENDERING")
            print(f"  [{tick+1}s] daemon={daemon_lines} wine={wine_lines} | {' '.join(milestones) if milestones else 'loading...'}")

    elapsed = time.monotonic() - t0
    exit_code = proc.poll()
    if exit_code is None:
        proc.kill()
        proc.wait()
        print(f"\n  Timeout ({TIMEOUT}s), killed after {elapsed:.1f}s")
    else:
        print(f"\n  Exited code={exit_code} after {elapsed:.1f}s")

    # Kill everything
    kill_amphetamine_processes()

    # Analysis
    print(f"\n{'='*60}")
    print("  ANALYSIS")
    print(f"{'='*60}")

    if DAEMON_LOG.exists():
        text = DAEMON_LOG.read_text(errors="replace")
        lines = text.splitlines()
        print(f"  Daemon requests: ~{len(lines)}")

        # Services
        svc_lines = [l for l in lines if "query_key Services:" in l]
        if svc_lines:
            # Extract children count from first occurrence
            import re
            m = re.search(r'children=\[(.+?)\]', svc_lines[0])
            if m:
                children = m.group(1).count('"') // 2
                print(f"  Services enumerated: {children} children")

        # Pipes
        pipes = [l for l in lines if "create_named_pipe:" in l and "handle=" in l]
        for p in pipes:
            print(f"  Pipe: {p.strip()}")

        listens = [l for l in lines if "FSCTL_PIPE_LISTEN" in l]
        for l in listens:
            print(f"  Listen: {l.strip()}")

        connects = [l for l in lines if "pipe connect:" in l]
        for c in connects[:10]:
            print(f"  Connect: {c.strip()}")

        apcs = [l for l in lines if "KERNEL_APC" in l or "queued APC" in l or "captured cookie" in l]
        for a in apcs[:10]:
            print(f"  APC: {a.strip()}")

        # Processes
        procs = [l for l in lines if "new_process: pid=" in l and "info_size" in l]
        print(f"  Processes spawned: {len(procs)}")
        for p in procs:
            print(f"    {p.strip()}")

        # Exits
        exits = [l for l in lines if "terminate_process:" in l and "exit_code" in l and "handle=0x0" in l]
        for e in exits:
            print(f"  Exit: {e.strip()}")

        # Wineboot
        wineboot_err = [l for l in lines if "wineboot" in l.lower() and ("error" in l.lower() or "fail" in l.lower())]
        for w in wineboot_err:
            print(f"  Wineboot: {w.strip()}")

    if WINE_STDERR.exists():
        text = WINE_STDERR.read_text(errors="replace")
        swaps = text.count("wglSwapBuffers")
        if swaps:
            print(f"  Render: {swaps} wglSwapBuffers calls")
        errs = [l.strip() for l in text.splitlines() if "err:" in l.lower() or "warn:" in l.lower() or "could not load" in l.lower()]
        for e in errs[:10]:
            print(f"  Wine: {e}")

    # Launcher stderr
    launcher_out = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if "could not load" in launcher_out.lower() or "error" in launcher_out.lower():
        for line in launcher_out.splitlines():
            if "could not load" in line.lower() or "error" in line.lower() or "fail" in line.lower():
                print(f"  Launcher: {line.strip()}")

    print(f"\n  Files: daemon={DAEMON_LOG}  wine={WINE_STDERR}")
    print()

if __name__ == "__main__":
    main()

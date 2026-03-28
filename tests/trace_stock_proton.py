#!/usr/bin/env python3
"""Trace a stock Proton 10.0 launch of Balatro to capture wineserver protocol traffic.

Captures WINEDEBUG=+server output which shows every wineserver request/reply,
giving us the exact blueprint for what amphetamine needs to handle.

Usage:
    python3 tests/trace_stock_proton.py                # Default 30s timeout
    python3 tests/trace_stock_proton.py --timeout 60   # Longer
    python3 tests/trace_stock_proton.py --filter display  # Filter output
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from util import _USER_HOME, STEAM_ROOT
STEAMAPPS = STEAM_ROOT / "steamapps"
PROTON_DIR = STEAMAPPS / "common" / "Proton - Experimental"
PROTON_BIN = PROTON_DIR / "proton"
GAME_EXE = STEAMAPPS / "common" / "Balatro" / "Balatro.exe"
COMPAT_DATA = STEAMAPPS / "compatdata" / "2379780"
TRACE_DIR = Path("/tmp/amphetamine/trace")
TRACE_LOG = TRACE_DIR / "wine_server_trace.log"
FILTERED_LOG = TRACE_DIR / "display_init_trace.log"

# Keywords we care about for display initialization
DISPLAY_KEYWORDS = [
    "set_winstation_monitors",
    "get_window_property",
    "set_window_property",
    "remove_window_property",
    "update_display",
    "display_device",
    "monitor",
    "desktop",
    "GraphicsDriver",
    "wine_display_device_guid",
    "create_desktop",
    "get_thread_desktop",
    "set_thread_desktop",
    "open_desktop",
    "close_desktop",
    "get_desktop_window",
    "enum_desktop",
    "winstation",
    "create_winstation",
    "open_winstation",
    "get_process_winstation",
    "set_process_winstation",
]


def main():
    parser = argparse.ArgumentParser(description="Trace stock Proton launch")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--filter", type=str, default=None,
                        help="Filter keyword (e.g., 'display', 'monitor', 'winstation')")
    parser.add_argument("--full-debug", action="store_true",
                        help="Use +server,+display,+monitor,+winstation (very verbose)")
    args = parser.parse_args()

    if not PROTON_BIN.exists():
        print(f"ERROR: Proton not found at {PROTON_BIN}")
        sys.exit(1)
    if not GAME_EXE.exists():
        print(f"ERROR: Balatro not found at {GAME_EXE}")
        sys.exit(1)

    TRACE_DIR.mkdir(parents=True, exist_ok=True)

    # Build environment
    env = os.environ.copy()
    env["STEAM_COMPAT_DATA_PATH"] = str(COMPAT_DATA)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(STEAM_ROOT)
    env["SteamAppId"] = "2379780"
    env["SteamGameId"] = "2379780"

    # The key: trace all wineserver calls
    if args.full_debug:
        env["WINEDEBUG"] = "+server,+display,+monitor,+winstation,+process"
    else:
        # +server alone gives us every request/reply pair
        env["WINEDEBUG"] = "+server"

    # Ensure display vars pass through
    for var in ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DISPLAY"):
        if var in os.environ:
            env[var] = os.environ[var]

    print(f"=== Stock Proton 10.0 Trace ===")
    print(f"Game:      Balatro (2379780)")
    print(f"Proton:    {PROTON_DIR}")
    print(f"WINEDEBUG: {env['WINEDEBUG']}")
    print(f"Timeout:   {args.timeout}s")
    print(f"Trace log: {TRACE_LOG}")
    print(f"Filtered:  {FILTERED_LOG}")
    print()

    # Launch
    print("Launching...")
    t0 = time.monotonic()
    stderr_file = open(TRACE_LOG, "w")

    try:
        proc = subprocess.Popen(
            [str(PROTON_BIN), "run", str(GAME_EXE)],
            env=env,
            cwd="/tmp",
            stdout=subprocess.PIPE,
            stderr=stderr_file,
        )
    except OSError as e:
        print(f"Failed to launch: {e}")
        sys.exit(1)

    # Monitor
    try:
        for tick in range(args.timeout):
            time.sleep(1)
            ret = proc.poll()
            elapsed = time.monotonic() - t0

            # Print progress every 5s
            if tick % 5 == 0:
                size = TRACE_LOG.stat().st_size if TRACE_LOG.exists() else 0
                print(f"  [{elapsed:.0f}s] running... trace log: {size / 1024:.0f} KB")

            if ret is not None:
                elapsed = time.monotonic() - t0
                print(f"\nProcess exited with code {ret} after {elapsed:.1f}s")
                break
        else:
            print(f"\nTimeout ({args.timeout}s) reached, killing...")
            proc.send_signal(signal.SIGTERM)
            time.sleep(2)
            proc.kill()
    except KeyboardInterrupt:
        print("\nInterrupted, killing...")
        proc.kill()
    finally:
        stderr_file.close()

    # Kill triskelion daemon only — never kill stock Proton wineserver
    subprocess.run(["pkill", "-x", "triskelion"], capture_output=True)
    time.sleep(1)

    # Post-process: extract display-related lines
    print(f"\n=== Post-processing trace ===")
    total_lines = 0
    display_lines = []
    request_counts = {}

    try:
        with open(TRACE_LOG, "r", errors="replace") as f:
            for line in f:
                total_lines += 1

                # Count request types (lines like "0024: req_name( ...")
                if ": " in line and "(" in line:
                    # Extract request name from patterns like "0024: set_winstation_monitors("
                    parts = line.split(": ", 1)
                    if len(parts) == 2:
                        req_part = parts[1].strip()
                        paren = req_part.find("(")
                        if paren > 0:
                            req_name = req_part[:paren].strip()
                            # Filter out noise (timestamps, etc)
                            if req_name.replace("_", "").isalpha():
                                request_counts[req_name] = request_counts.get(req_name, 0) + 1

                # Filter for display-related lines
                line_lower = line.lower()
                if any(kw in line_lower for kw in DISPLAY_KEYWORDS):
                    display_lines.append(line)
    except OSError as e:
        print(f"Error reading trace: {e}")
        sys.exit(1)

    # Write filtered log
    with open(FILTERED_LOG, "w") as f:
        f.writelines(display_lines)

    print(f"Total trace lines: {total_lines:,}")
    print(f"Display-related lines: {len(display_lines):,}")
    print(f"Full trace: {TRACE_LOG}")
    print(f"Filtered:   {FILTERED_LOG}")

    # Print request frequency table
    if request_counts:
        print(f"\n=== Request Frequency (top 40) ===")
        sorted_reqs = sorted(request_counts.items(), key=lambda x: -x[1])
        for name, count in sorted_reqs[:40]:
            marker = " <-- DISPLAY" if any(kw in name for kw in ["monitor", "desktop", "winstation", "display", "property"]) else ""
            print(f"  {count:6d}  {name}{marker}")

    # Print display-related excerpt
    if display_lines:
        print(f"\n=== Display Init Trace (first 100 lines) ===")
        for line in display_lines[:100]:
            print(f"  {line.rstrip()}")

    if args.filter:
        print(f"\n=== Custom filter: '{args.filter}' ===")
        for line in display_lines:
            if args.filter.lower() in line.lower():
                print(f"  {line.rstrip()}")

    print(f"\nDone. Read {TRACE_LOG} for full trace, {FILTERED_LOG} for display-related calls.")


if __name__ == "__main__":
    main()

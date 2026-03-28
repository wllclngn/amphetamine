#!/usr/bin/env python3
"""Capture stock Proton reference artifacts for comparison testing.

Runs stock Proton 10.0 against a game (default: Balatro) and captures
protocol traces, display init, registry state, etc. Results are saved
to tests/reference/ and reused by comparison tests on every subsequent run.

Uses a TEMP PREFIX — never touches the real Steam prefix.
Uses PROCESS GROUP isolation — never orphans wine processes.

Usage:
    python3 tests/capture_reference.py                    # Full capture
    python3 tests/capture_reference.py --layer protocol   # Just protocol
    python3 tests/capture_reference.py --layer display    # Just display
    python3 tests/capture_reference.py --timeout 45       # Longer run
    python3 tests/capture_reference.py --appid 2320       # Different game

Layers requiring sudo (montauk eBPF):
    sudo python3 tests/capture_reference.py --layer io
    sudo python3 tests/capture_reference.py --layer process_tree
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from util import (
    STEAM_ROOT, PROTON_BIN, REFERENCE_DIR,
    make_temp_prefix, make_game_env, get_game_exe,
    kill_process_group, sudo_as_user,
)

_USER_HOME = Path(f"/home/{os.environ.get('SUDO_USER', os.environ.get('USER', 'mod'))}")
MONTAUK = _USER_HOME / "personal/PROGRAMMING/SYSTEM PROGRAMS/LINUX/montauk/build/montauk"

TIMEOUT = 30
APPID = "2379780"
ALL_LAYERS = ["protocol", "display", "registry"]
SUDO_LAYERS = ["io", "process_tree"]


def get_proton_version():
    """Read Proton version string."""
    version_file = PROTON_BIN.parent / "version"
    if version_file.exists():
        return version_file.read_text().strip().split()[-1] if version_file.exists() else "unknown"
    return "unknown"


def capture_protocol(game_exe, timeout):
    """Capture WINEDEBUG=+server output from stock Proton."""
    print("  [protocol] Capturing wineserver protocol trace...")
    prefix, cleanup = make_temp_prefix()

    try:
        env = make_game_env(APPID, winedebug="+server,+pid", extra={
            "WINEPREFIX": str(prefix),
        })

        cmd = sudo_as_user(["python3", str(PROTON_BIN), "run", str(game_exe)])
        proc = subprocess.Popen(
            cmd, env=env, cwd="/tmp",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        time.sleep(timeout)
        kill_process_group(proc)

        stderr = proc.stderr.read().decode(errors="replace")

        # Save raw trace
        out_dir = REFERENCE_DIR / "protocol"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "server_trace.log").write_text(stderr)

        # Parse opcode frequency
        opcodes = re.findall(r'(\w+) request', stderr)
        freq = Counter(opcodes)
        freq_text = "\n".join(f"{count:6d}  {op}" for op, count in freq.most_common())
        (out_dir / "opcode_frequency.txt").write_text(freq_text)

        print(f"  [protocol] Captured {len(opcodes)} requests, {len(freq)} unique opcodes")
        return True
    finally:
        cleanup()


def capture_display(game_exe, timeout):
    """Capture display initialization trace."""
    print("  [display] Capturing display init trace...")
    prefix, cleanup = make_temp_prefix()

    display_keywords = [
        "display", "monitor", "desktop", "GraphicsDriver", "XRandR",
        "x11drv", "winex11", "resolution", "screen",
    ]

    try:
        env = make_game_env(APPID, winedebug="+display,+x11drv,+system", extra={
            "WINEPREFIX": str(prefix),
        })

        cmd = sudo_as_user(["python3", str(PROTON_BIN), "run", str(game_exe)])
        proc = subprocess.Popen(
            cmd, env=env, cwd="/tmp",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        time.sleep(timeout)
        kill_process_group(proc)

        stderr = proc.stderr.read().decode(errors="replace")

        # Filter for display-relevant lines
        filtered = []
        for line in stderr.splitlines():
            lower = line.lower()
            if any(kw in lower for kw in display_keywords):
                filtered.append(line)

        out_dir = REFERENCE_DIR / "display"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "display_init_trace.txt").write_text("\n".join(filtered))

        print(f"  [display] Captured {len(filtered)} display-related lines")
        return True
    finally:
        cleanup()


def capture_registry(game_exe, timeout):
    """Capture registry state after Proton boot."""
    print("  [registry] Capturing registry state...")
    prefix, cleanup = make_temp_prefix()

    try:
        env = make_game_env(APPID, winedebug="-all", extra={
            "WINEPREFIX": str(prefix),
        })

        cmd = sudo_as_user(["python3", str(PROTON_BIN), "run", str(game_exe)])
        proc = subprocess.Popen(
            cmd, env=env, cwd="/tmp",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        time.sleep(timeout)
        kill_process_group(proc)

        # Read registry files from the temp prefix
        out_dir = REFERENCE_DIR / "registry"
        out_dir.mkdir(parents=True, exist_ok=True)

        for reg_name in ["system.reg", "user.reg", "userdef.reg"]:
            reg_file = prefix / reg_name
            if reg_file.exists():
                shutil.copy2(reg_file, out_dir / reg_name)

        # Extract display device keys
        system_reg = prefix / "system.reg"
        if system_reg.exists():
            text = system_reg.read_text(errors="replace")
            display_lines = []
            in_display = False
            for line in text.splitlines():
                if "PCI\\VEN_" in line or "DISPLAY" in line.upper() or "GraphicsDriver" in line:
                    in_display = True
                if in_display:
                    display_lines.append(line)
                    if line.strip() == "":
                        in_display = False
            (out_dir / "display_registry.txt").write_text("\n".join(display_lines))

        reg_files = list(out_dir.glob("*.reg"))
        print(f"  [registry] Captured {len(reg_files)} registry files")
        return True
    finally:
        cleanup()


def write_metadata(layers_captured, game_exe):
    """Write metadata.json with capture parameters."""
    meta = {
        "proton_version": get_proton_version(),
        "captured_at": datetime.now().isoformat(),
        "game": game_exe.stem if game_exe else "unknown",
        "appid": APPID,
        "timeout": TIMEOUT,
        "layers": layers_captured,
    }
    (REFERENCE_DIR / "metadata.json").write_text(json.dumps(meta, indent=2))
    print(f"\n  Metadata written: Proton {meta['proton_version']}, {len(layers_captured)} layers")


def main():
    global TIMEOUT, APPID

    if "--timeout" in sys.argv:
        TIMEOUT = int(sys.argv[sys.argv.index("--timeout") + 1])
    if "--appid" in sys.argv:
        APPID = sys.argv[sys.argv.index("--appid") + 1]

    requested_layers = ALL_LAYERS[:]
    if "--layer" in sys.argv:
        requested_layers = [sys.argv[sys.argv.index("--layer") + 1]]

    # Check for sudo-required layers
    for layer in requested_layers:
        if layer in SUDO_LAYERS and os.geteuid() != 0:
            print(f"ERROR: Layer '{layer}' requires sudo (montauk eBPF)")
            print(f"  sudo python3 tests/capture_reference.py --layer {layer}")
            sys.exit(1)

    if not PROTON_BIN.exists():
        print(f"ERROR: Proton not found at {PROTON_BIN}")
        sys.exit(1)

    game_exe = get_game_exe(APPID)
    if not game_exe:
        print(f"ERROR: Game exe not found for appid {APPID}")
        sys.exit(1)

    print("=" * 60)
    print("  Stock Proton Reference Capture")
    print("=" * 60)
    print(f"  Proton: {get_proton_version()}")
    print(f"  Game: {game_exe.name} ({game_exe.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"  Timeout: {TIMEOUT}s per layer")
    print(f"  Layers: {', '.join(requested_layers)}")
    print(f"  Output: {REFERENCE_DIR}/")

    # Check for existing reference
    meta = None
    meta_path = REFERENCE_DIR / "metadata.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        print(f"\n  Existing reference: Proton {meta.get('proton_version', '?')}, "
              f"captured {meta.get('captured_at', '?')[:10]}")

    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)

    capture_fns = {
        "protocol": capture_protocol,
        "display": capture_display,
        "registry": capture_registry,
    }

    captured = []
    for layer in requested_layers:
        fn = capture_fns.get(layer)
        if fn:
            print()
            if fn(game_exe, TIMEOUT):
                captured.append(layer)
        else:
            print(f"\n  [SKIP] Layer '{layer}' not yet implemented")

    write_metadata(captured, game_exe)

    print(f"\n{'=' * 60}")
    print(f"  Done: {len(captured)}/{len(requested_layers)} layers captured")
    print(f"  Reference at: {REFERENCE_DIR}/")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()

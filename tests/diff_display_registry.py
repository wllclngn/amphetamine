#!/usr/bin/env python3
"""Diff display device registry between stock wineserver and quark daemon.

Runs a minimal Wine process under BOTH stock wineserver and our daemon,
queries the display-related registry keys that SDL2/winex11.drv reads,
and shows exactly what's missing or different.

This is the diagnostic for "SDL2 won't create a window" — the game
reads display device info from registry during SDL_Init(SDL_INIT_VIDEO).
If the keys are empty/wrong, SDL2 skips window creation.

Usage:
    python3 tests/diff_display_registry.py
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from util import kill_quark_processes, STEAM_ROOT

# ── Paths ─────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPAT_DIR = STEAM_ROOT / "compatibilitytools.d/quark"
COMPAT_DATA = STEAM_ROOT / "steamapps/compatdata/2379780"
PREFIX = COMPAT_DATA / "pfx"

OUT_DIR = Path("/tmp/quark/display_diff")

# Registry paths SDL2/winex11.drv reads for display devices
# These are the keys that must be populated for SDL_CreateWindow to work
DISPLAY_REG_PATHS = [
    # Display adapters — winex11.drv writes here via commit_display_devices
    r"HKLM\System\CurrentControlSet\Control\Video",
    # Class GUID for display adapters
    r"HKLM\System\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}",
    # DeviceMap — maps \Device\Video0 etc
    r"HKLM\Hardware\DeviceMap\Video",
    # DirectDraw info
    r"HKLM\Software\Microsoft\DirectDraw",
    # Desktop/display settings
    r"HKCU\Control Panel\Desktop",
    r"HKCU\Software\Wine\Drivers",
    # Graphics drivers registry — where Wine stores display config
    r"HKLM\Software\Wine\Drivers",
    # Volatile environment (screen resolution, etc.)
    r"HKCU\Volatile Environment",
]

# Small C program that queries and dumps registry keys
# We compile this ONCE and run it under both wineservers
REG_DUMP_C = r"""
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>

/* Recursively dump a registry key to stdout as indented text */
void dump_key(HKEY hkey, const char *path, int depth) {
    DWORD i, name_len, type, data_len;
    char name[512], data[4096];
    HKEY subkey;
    FILETIME ft;

    /* Enumerate values */
    for (i = 0; ; i++) {
        name_len = sizeof(name);
        data_len = sizeof(data);
        memset(data, 0, sizeof(data));
        if (RegEnumValueA(hkey, i, name, &name_len, NULL, &type, (BYTE*)data, &data_len) != ERROR_SUCCESS)
            break;
        for (int d = 0; d < depth; d++) printf("  ");
        printf("VALUE %s type=%lu len=%lu", name, type, data_len);
        if (type == REG_SZ || type == REG_EXPAND_SZ)
            printf(" data=\"%s\"", data);
        else if (type == REG_DWORD && data_len >= 4)
            printf(" data=0x%08x", *(DWORD*)data);
        else {
            printf(" data=");
            for (DWORD b = 0; b < data_len && b < 64; b++) printf("%02x", (unsigned char)data[b]);
            if (data_len > 64) printf("...");
        }
        printf("\n");
    }

    /* Enumerate subkeys */
    for (i = 0; ; i++) {
        name_len = sizeof(name);
        if (RegEnumKeyExA(hkey, i, name, &name_len, NULL, NULL, NULL, &ft) != ERROR_SUCCESS)
            break;
        for (int d = 0; d < depth; d++) printf("  ");
        printf("KEY %s\\%s\n", path, name);
        if (RegOpenKeyExA(hkey, name, 0, KEY_READ, &subkey) == ERROR_SUCCESS) {
            char subpath[1024];
            snprintf(subpath, sizeof(subpath), "%s\\%s", path, name);
            dump_key(subkey, subpath, depth + 1);
            RegCloseKey(subkey);
        }
    }
}

void dump_root(HKEY root, const char *root_name, const char *subpath) {
    HKEY hkey;
    char full[1024];
    snprintf(full, sizeof(full), "%s\\%s", root_name, subpath);
    if (RegOpenKeyExA(root, subpath, 0, KEY_READ, &hkey) == ERROR_SUCCESS) {
        printf("=== %s ===\n", full);
        dump_key(hkey, full, 1);
        RegCloseKey(hkey);
    } else {
        printf("=== %s === (NOT FOUND)\n", full);
    }
}

int main(void) {
    /* Give wineboot/explorer time to populate display devices */
    Sleep(3000);

    printf("### DISPLAY REGISTRY DUMP ###\n");

    /* HKLM keys */
    dump_root(HKEY_LOCAL_MACHINE, "HKLM", "System\\CurrentControlSet\\Control\\Video");
    dump_root(HKEY_LOCAL_MACHINE, "HKLM", "System\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}");
    dump_root(HKEY_LOCAL_MACHINE, "HKLM", "Hardware\\DeviceMap\\Video");
    dump_root(HKEY_LOCAL_MACHINE, "HKLM", "Software\\Microsoft\\DirectDraw");
    dump_root(HKEY_LOCAL_MACHINE, "HKLM", "Software\\Wine\\Drivers");

    /* HKCU keys */
    dump_root(HKEY_CURRENT_USER, "HKCU", "Control Panel\\Desktop");
    dump_root(HKEY_CURRENT_USER, "HKCU", "Software\\Wine\\Drivers");
    dump_root(HKEY_CURRENT_USER, "HKCU", "Volatile Environment");

    /* Check EnumDisplayDevices directly */
    printf("\n### EnumDisplayDevices ###\n");
    DISPLAY_DEVICEA dd;
    dd.cb = sizeof(dd);
    for (DWORD dev = 0; EnumDisplayDevicesA(NULL, dev, &dd, 0); dev++) {
        printf("Adapter %lu: DeviceName=%s DeviceString=%s StateFlags=0x%lx\n",
               dev, dd.DeviceName, dd.DeviceString, dd.StateFlags);
        DISPLAY_DEVICEA mon;
        mon.cb = sizeof(mon);
        for (DWORD m = 0; EnumDisplayDevicesA(dd.DeviceName, m, &mon, 0); m++) {
            printf("  Monitor %lu: DeviceName=%s DeviceString=%s StateFlags=0x%lx\n",
                   m, mon.DeviceName, mon.DeviceString, mon.StateFlags);
        }
        dd.cb = sizeof(dd);
    }

    /* Check current display mode */
    printf("\n### Current Display Mode ###\n");
    DEVMODEA dm;
    dm.dmSize = sizeof(dm);
    if (EnumDisplaySettingsA(NULL, ENUM_CURRENT_SETTINGS, &dm)) {
        printf("Resolution: %lux%lu @ %luHz, %lu bpp\n",
               dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency, dm.dmBitsPerPel);
    } else {
        printf("EnumDisplaySettings FAILED\n");
    }

    printf("\n### DONE ###\n");
    fflush(stdout);
    return 0;
}
"""


def kill_wine():
    """Kill all wine processes."""
    kill_quark_processes()


def compile_regdump():
    """Compile the registry dump helper."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    src = OUT_DIR / "regdump.c"
    exe = OUT_DIR / "regdump.exe"

    src.write_text(REG_DUMP_C)

    # Cross-compile with Wine's mingw
    cc = None
    for candidate in ["x86_64-w64-mingw32-gcc", "x86_64-w64-mingw32-cc"]:
        if subprocess.run(["which", candidate], capture_output=True).returncode == 0:
            cc = candidate
            break

    if not cc:
        print("ERROR: x86_64-w64-mingw32-gcc not found. Install mingw-w64-gcc.")
        sys.exit(1)

    print(f"Compiling regdump.exe with {cc}...")
    r = subprocess.run([cc, "-o", str(exe), str(src), "-ladvapi32", "-luser32"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Compile failed:\n{r.stderr}")
        sys.exit(1)

    print(f"Built {exe}")
    return exe


def run_with_stock_wine(exe: Path) -> str:
    """Run regdump under stock system Wine (stock wineserver)."""
    print("\n" + "="*70)
    print("RUNNING UNDER STOCK WINESERVER (system Wine 11.5)")
    print("="*70)

    kill_wine()

    env = os.environ.copy()
    env["WINEPREFIX"] = str(PREFIX)
    env["WINEDEBUG"] = "-all"
    env["DISPLAY"] = os.environ.get("DISPLAY", ":0")

    # Let wineboot init + explorer populate display devices
    print("Running wineboot --init (60s timeout)...")
    try:
        subprocess.run(["wine", "wineboot", "--init"], env=env,
                       capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        print("  wineboot timed out — continuing (prefix may already exist)")

    # Give explorer time to register display devices
    time.sleep(3)

    print("Running regdump.exe...")
    try:
        r = subprocess.run(["wine", str(exe)], env=env,
                           capture_output=True, text=True, timeout=30)
        output = r.stdout
    except subprocess.TimeoutExpired:
        output = "(TIMED OUT)"

    # Save
    out_file = OUT_DIR / "stock_wine.txt"
    out_file.write_text(output)
    print(f"Saved to {out_file}")

    kill_wine()
    return output


def run_with_quark(exe: Path) -> str:
    """Run regdump under our daemon (quark/triskelion)."""
    print("\n" + "="*70)
    print("RUNNING UNDER QUARK DAEMON (triskelion)")
    print("="*70)

    kill_wine()

    # Find our daemon binary and wine
    daemon_bin = COMPAT_DIR / "triskelion"
    wine_bin = Path("/usr/bin/wine")

    if not daemon_bin.exists():
        print(f"ERROR: daemon not found at {daemon_bin}")
        sys.exit(1)

    # Determine socket dir
    uid = os.getuid()
    socket_dir = Path(f"/tmp/.wine-{uid}/server-{os.stat('/').st_dev:x}-{os.stat('/').st_ino:x}")

    env = os.environ.copy()
    env["WINEPREFIX"] = str(PREFIX)
    env["WINEDEBUG"] = "-all"
    env["DISPLAY"] = os.environ.get("DISPLAY", ":0")
    env["WINESERVER"] = str(daemon_bin)

    # Start our daemon
    print(f"Starting daemon: {daemon_bin}")
    daemon_proc = subprocess.Popen(
        [str(daemon_bin)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=open(OUT_DIR / "daemon_stderr.log", "w"),
    )
    time.sleep(1)

    # Run wineboot to init
    print("Running wineboot --init...")
    try:
        subprocess.run(["wine", "wineboot", "--init"], env=env,
                       capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        print("  wineboot timed out (continuing anyway)")

    # Give explorer time
    time.sleep(2)

    print("Running regdump.exe...")
    try:
        r = subprocess.run(["wine", str(exe)], env=env,
                           capture_output=True, text=True, timeout=30)
        output = r.stdout
    except subprocess.TimeoutExpired:
        output = "(TIMED OUT)"

    # Save
    out_file = OUT_DIR / "quark.txt"
    out_file.write_text(output)
    print(f"Saved to {out_file}")

    kill_wine()
    daemon_proc.kill()
    daemon_proc.wait()

    return output


def run_with_iterate(exe: Path) -> str:
    """Run regdump through iterate.py (the actual quark launch path)."""
    print("\n" + "="*70)
    print("RUNNING UNDER QUARK VIA ITERATE.PY (actual launch path)")
    print("="*70)

    kill_wine()

    env = os.environ.copy()
    env["WINEPREFIX"] = str(PREFIX)
    env["WINEDEBUG"] = "-all"
    env["DISPLAY"] = os.environ.get("DISPLAY", ":0")

    # Use our launcher's exact setup
    wine_bin = COMPAT_DIR / "bin" / "wine64"
    if not wine_bin.exists():
        wine_bin = Path("/usr/bin/wine")

    daemon_bin = COMPAT_DIR / "triskelion"

    # Start daemon with same flags as launcher
    print(f"Starting daemon: {daemon_bin}")
    daemon_log = OUT_DIR / "iterate_daemon.log"
    daemon_proc = subprocess.Popen(
        [str(daemon_bin)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=open(daemon_log, "w"),
    )
    time.sleep(1)

    # Run wineboot
    print("Running wineboot --init...")
    try:
        subprocess.run([str(wine_bin), "wineboot", "--init"], env=env,
                       capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        print("  wineboot timed out")

    # Spawn explorer like our launcher does
    print("Spawning explorer.exe /desktop...")
    explorer_proc = subprocess.Popen(
        [str(wine_bin), "explorer.exe", "/desktop"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)

    # Now run regdump
    print("Running regdump.exe...")
    try:
        r = subprocess.run([str(wine_bin), str(exe)], env=env,
                           capture_output=True, text=True, timeout=30)
        output = r.stdout
    except subprocess.TimeoutExpired:
        output = "(TIMED OUT)"

    out_file = OUT_DIR / "iterate.txt"
    out_file.write_text(output)
    print(f"Saved to {out_file}")

    kill_wine()
    daemon_proc.kill()
    daemon_proc.wait()

    return output


def diff_outputs(stock: str, amph: str, label: str = "quark"):
    """Compare registry dumps and highlight differences."""
    print("\n" + "="*70)
    print(f"DIFF: stock wineserver vs {label}")
    print("="*70)

    stock_lines = [l.strip() for l in stock.splitlines() if l.strip()]
    amph_lines = [l.strip() for l in amph.splitlines() if l.strip()]

    stock_set = set(stock_lines)
    amph_set = set(amph_lines)

    only_stock = stock_set - amph_set
    only_amph = amph_set - stock_set

    # Group by section
    def section_of(line):
        if line.startswith("==="):
            return line
        return "misc"

    if only_stock:
        print(f"\n--- ONLY in stock wineserver ({len(only_stock)} lines) ---")
        for line in sorted(only_stock):
            print(f"  MISSING: {line}")

    if only_amph:
        print(f"\n--- ONLY in {label} ({len(only_amph)} lines) ---")
        for line in sorted(only_amph):
            print(f"  EXTRA:   {line}")

    if not only_stock and not only_amph:
        print("\n  IDENTICAL — no differences found!")

    # Specifically check EnumDisplayDevices
    print("\n--- EnumDisplayDevices comparison ---")
    stock_dd = [l for l in stock_lines if l.startswith("Adapter") or l.startswith("  Monitor") or l.startswith("Resolution")]
    amph_dd = [l for l in amph_lines if l.startswith("Adapter") or l.startswith("  Monitor") or l.startswith("Resolution")]

    print(f"Stock:       {len(stock_dd)} entries")
    for l in stock_dd:
        print(f"  {l}")

    print(f"{label}: {len(amph_dd)} entries")
    for l in amph_dd:
        print(f"  {l}")

    if not stock_dd:
        print("  WARNING: Stock wineserver returned NO display devices!")
    if not amph_dd:
        print(f"  WARNING: {label} returned NO display devices — THIS IS THE BUG!")

    # Check display mode
    print("\n--- Display Mode ---")
    stock_dm = [l for l in stock_lines if "Resolution:" in l or "FAILED" in l]
    amph_dm = [l for l in amph_lines if "Resolution:" in l or "FAILED" in l]
    print(f"Stock:       {stock_dm}")
    print(f"{label}: {amph_dm}")


def main():
    print("Display Registry Diff: Stock Wine vs Quark")
    print("=" * 70)

    # Step 1: compile helper
    exe = compile_regdump()

    # Step 2: run under stock wine
    stock_output = run_with_stock_wine(exe)

    # Step 3: run under quark
    amph_output = run_with_quark(exe)

    # Step 4: diff
    diff_outputs(stock_output, amph_output)

    # Summary
    print("\n" + "="*70)
    print("FILES SAVED:")
    print(f"  Stock:       {OUT_DIR}/stock_wine.txt")
    print(f"  Quark: {OUT_DIR}/quark.txt")
    print(f"  Daemon log:  {OUT_DIR}/daemon_stderr.log")
    print("="*70)


if __name__ == "__main__":
    main()

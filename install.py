#!/usr/bin/env python3
"""Build triskelion, clone Valve Wine, and apply triskelion patches."""

import filecmp
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
RUST_DIR = SCRIPT_DIR / "amphetamine"
PATCHES_DIR = SCRIPT_DIR / "patches" / "wine"

WINE_SRC_DIR = Path.home() / ".local" / "share" / "amphetamine" / "wine-src"
STEAM_COMPAT_DIR = Path.home() / ".local" / "share" / "Steam" / "compatibilitytools.d" / "amphetamine"
WINE_CLONE_URL = "https://github.com/ValveSoftware/wine.git"
WINE_CLONE_BRANCH = "proton_10.0"

CACHYOS_WINE_URL = "https://github.com/CachyOS/wine-cachyos.git"


def get_version():
    """Read version from amphetamine/Cargo.toml."""
    cargo_toml = RUST_DIR / "Cargo.toml"
    for line in cargo_toml.read_text().splitlines():
        if line.startswith("version"):
            return line.split('"')[1]
    return "0.0.0"

# Patch text: triskelion_has_posted inline function for win32u/message.c
WIN32U_FUNCTION = """\

/* triskelion: check if the shm ring has pending posted messages.
 * queue_ptr is from TEB->glReserved2, set by ntdll triskelion_claim_slot.
 * The ring's write_pos (offset 0) and read_pos (offset 64) are cacheline-aligned uint64_t. */
static inline BOOL triskelion_has_posted( volatile void *queue_ptr )
{
    volatile ULONGLONG *wp, *rp;
    if (!queue_ptr) return FALSE;
    wp = (volatile ULONGLONG *)queue_ptr;
    rp = (volatile ULONGLONG *)((char *)queue_ptr + 64);
    return *wp > *rp;
}
"""

# Patch text: server.c bypass block (inserted before FTRACE_BLOCK_START)
SERVER_BYPASS = """\
    /* triskelion: shared memory bypass for hot-path messages */
    ret = triskelion_try_bypass( req_ptr );
    if (ret != STATUS_NOT_IMPLEMENTED)
        return ret;

"""

# Patch text: win32u peek_message condition prefix
PEEK_MSG_PREFIX = """\
        /* triskelion: if the shm ring has pending posted messages,
         * disable check_queue_bits and force the server call path.
         * The bypass in ntdll server_call_unlocked will pop from the ring. */
        if (!triskelion_has_posted(NtCurrentTeb()->glReserved2) &&
            """


ENV_CONFIG_TEMPLATE = """\
# amphetamine custom environment variables
#
# Format: KEY=VALUE (one per line)
# Lines starting with # are comments. Blank lines are ignored.
# Variables set here override amphetamine's built-in defaults.
# Edit this file any time — changes apply on next game launch.
#
# --- Logging ---
# WINEDEBUG=-all
# DXVK_LOG_LEVEL=none
# DXVK_NVAPI_LOG_LEVEL=none
# VKD3D_DEBUG=none
# VKD3D_SHADER_DEBUG=none
# PROTON_LOG=0
#
# --- Wayland ---
# WINE_ENABLE_WAYLAND=1
#
# --- Sync ---
# WINE_NTSYNC=1
#
# --- Overlays ---
# MANGOHUD=1
# MANGOHUD_CONFIG=fps,frametime,gpu_temp,cpu_temp
#
# --- Performance ---
# DXVK_ASYNC=1
# mesa_glthread=true
# RADV_PERFTEST=gpl
#
# --- Game-specific ---
# PROTON_ENABLE_NVAPI=1
"""


def log(level, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}]   {msg}", file=sys.stderr)


def prompt_yn(question):
    """Prompt the user with a [Y/N] question. Returns True for yes, False for no."""
    while True:
        answer = input(f"{question} [Y/N] ").strip().lower()
        if answer == "y":
            return True
        if answer == "n":
            return False


def build_triskelion():
    log("INFO", "Building triskelion binary")
    ret = subprocess.run(["cargo", "build", "--release", "-p", "triskelion"], cwd=SCRIPT_DIR).returncode
    if ret != 0:
        log("ERROR", "cargo build failed")
        return ret

    # Workspace builds go to <repo>/target/, not <crate>/target/
    binary = SCRIPT_DIR / "target" / "release" / "triskelion"
    if not binary.exists():
        log("ERROR", f"Binary not found: {binary}")
        return 1

    dest = SCRIPT_DIR / "triskelion"
    shutil.copy2(binary, dest)
    os.chmod(dest, 0o755)
    log("INFO", f"Installed: {dest}")

    # Deploy to Steam compatibility tools directory
    STEAM_COMPAT_DIR.mkdir(parents=True, exist_ok=True)
    proton_dest = STEAM_COMPAT_DIR / "proton"
    shutil.copy2(binary, proton_dest)
    os.chmod(proton_dest, 0o755)
    log("INFO", f"Deployed to Steam: {proton_dest}")

    # Write VDF with current version
    version = get_version()
    vdf = STEAM_COMPAT_DIR / "compatibilitytool.vdf"
    vdf.write_text(f'''"compatibilitytools"
{{
  "compat_tools"
  {{
    "amphetamine"
    {{
      "install_path" "."
      "display_name" "amphetamine {version}"
      "from_oslist"  "windows"
      "to_oslist"    "linux"
    }}
  }}
}}
''')
    log("INFO", f"Updated VDF: amphetamine {version}")

    # Write toolmanifest.vdf (required by Steam's compatmanager)
    manifest = STEAM_COMPAT_DIR / "toolmanifest.vdf"
    manifest.write_text('''"manifest"
{
  "commandline" "/proton %verb%"
  "version" "2"
  "use_sessions" "1"
}
''')
    log("INFO", "Updated toolmanifest.vdf")

    return 0


def detect_cachyos_wine():
    """Detect if CachyOS Proton/Wine is installed."""
    # Check common CachyOS Proton installation paths
    cachyos_paths = [
        Path("/usr/share/steam/compatibilitytools.d/proton-cachyos"),
        Path.home() / ".steam" / "root" / "compatibilitytools.d" / "proton-cachyos",
        Path.home() / ".local" / "share" / "Steam" / "compatibilitytools.d" / "proton-cachyos",
    ]
    for p in cachyos_paths:
        if p.exists():
            log("INFO", f"CachyOS Wine detected: {p}")
            return True

    # Check via pacman (CachyOS is Arch-based)
    try:
        ret = subprocess.run(
            ["pacman", "-Q", "proton-cachyos"],
            capture_output=True, text=True
        )
        if ret.returncode == 0:
            log("INFO", f"CachyOS Wine detected: {ret.stdout.strip()}")
            return True
    except FileNotFoundError:
        pass

    return False


def get_cachyos_latest_branch():
    """Find the latest cachyos_10.0_*/main branch from remote."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", CACHYOS_WINE_URL],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None

        branches = []
        for line in result.stdout.splitlines():
            ref = line.split("\t")[1] if "\t" in line else ""
            ref = ref.replace("refs/heads/", "")
            if ref.startswith("cachyos_10.0_") and ref.endswith("/main"):
                branches.append(ref)

        if branches:
            branches.sort()
            return branches[-1]  # Latest by date
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def clone_wine(use_cachyos=False):
    # Check if existing source matches desired type
    marker = WINE_SRC_DIR / ".amphetamine_source"
    if (WINE_SRC_DIR / "dlls").exists():
        current_source = marker.read_text().strip() if marker.exists() else "valve"
        if use_cachyos and current_source != "cachyos":
            log("INFO", "Switching wine source from Valve to CachyOS...")
            shutil.rmtree(WINE_SRC_DIR)
        elif not use_cachyos and current_source == "cachyos":
            log("INFO", "Switching wine source from CachyOS to Valve...")
            shutil.rmtree(WINE_SRC_DIR)
        else:
            log("INFO", f"Wine source exists: {WINE_SRC_DIR} ({current_source})")
            return

    if use_cachyos:
        branch = get_cachyos_latest_branch()
        if not branch:
            log("WARN", "Could not find CachyOS Wine branch, falling back to Valve Wine")
            use_cachyos = False
        else:
            log("INFO", f"Cloning CachyOS Wine ({branch}) to {WINE_SRC_DIR}")
            WINE_SRC_DIR.parent.mkdir(parents=True, exist_ok=True)
            ret = subprocess.run([
                "git", "clone", "--depth", "1", "-b", branch,
                CACHYOS_WINE_URL, str(WINE_SRC_DIR),
            ]).returncode
            if ret != 0:
                log("ERROR", "CachyOS Wine clone failed, falling back to Valve Wine")
                if WINE_SRC_DIR.exists():
                    shutil.rmtree(WINE_SRC_DIR)
                use_cachyos = False
            else:
                marker.write_text("cachyos")
                log("INFO", "CachyOS Wine clone complete")
                return

    log("INFO", f"Cloning Valve Wine ({WINE_CLONE_BRANCH}) to {WINE_SRC_DIR}")
    WINE_SRC_DIR.parent.mkdir(parents=True, exist_ok=True)
    ret = subprocess.run([
        "git", "clone", "--depth", "1", "-b", WINE_CLONE_BRANCH,
        WINE_CLONE_URL, str(WINE_SRC_DIR),
    ]).returncode
    if ret != 0:
        log("ERROR", "git clone failed (GitHub may be down, retry later)")
        return
    marker.write_text("valve")
    log("INFO", "Clone complete")


def patch_copy_triskelion_c():
    src = PATCHES_DIR / "dlls" / "ntdll" / "unix" / "triskelion.c"
    dst = WINE_SRC_DIR / "dlls" / "ntdll" / "unix" / "triskelion.c"
    if dst.exists() and filecmp.cmp(src, dst, shallow=False):
        log("INFO", "triskelion.c already in place")
        return
    shutil.copy2(src, dst)
    log("INFO", f"Copied triskelion.c -> {dst}")


def patch_makefile_in():
    path = WINE_SRC_DIR / "dlls" / "ntdll" / "Makefile.in"
    text = path.read_text()
    if "unix/triskelion.c" in text:
        log("INFO", "Makefile.in already patched")
        return
    anchor = "\tunix/thread.c \\"
    if anchor not in text:
        log("ERROR", f"Anchor not found in {path}: {anchor!r}")
        sys.exit(1)
    text = text.replace(anchor, anchor + "\n\tunix/triskelion.c \\")
    path.write_text(text)
    log("INFO", "Patched Makefile.in: added unix/triskelion.c")


def patch_server_c():
    path = WINE_SRC_DIR / "dlls" / "ntdll" / "unix" / "server.c"
    text = path.read_text()
    if "triskelion_try_bypass" in text:
        log("INFO", "server.c already patched")
        return
    anchor = '    FTRACE_BLOCK_START("req %s", req->name)'
    if anchor not in text:
        log("ERROR", f"Anchor not found in {path}: {anchor!r}")
        sys.exit(1)
    text = text.replace(anchor, SERVER_BYPASS + anchor)
    path.write_text(text)
    log("INFO", "Patched server.c: added triskelion_try_bypass call")


def patch_unix_private_h():
    path = WINE_SRC_DIR / "dlls" / "ntdll" / "unix" / "unix_private.h"
    text = path.read_text()
    if "triskelion_try_bypass" in text:
        log("INFO", "unix_private.h already patched")
        return
    anchor = "extern unsigned int server_call_unlocked( void *req_ptr );"
    if anchor not in text:
        log("ERROR", f"Anchor not found in {path}: {anchor!r}")
        sys.exit(1)
    text = text.replace(anchor, anchor + "\nextern unsigned int triskelion_try_bypass( void *req_ptr );")
    path.write_text(text)
    log("INFO", "Patched unix_private.h: added triskelion_try_bypass declaration")


def patch_win32u_message():
    path = WINE_SRC_DIR / "dlls" / "win32u" / "message.c"
    text = path.read_text()
    if "triskelion_has_posted" in text:
        log("INFO", "win32u/message.c already patched")
        return

    # Modification A: insert triskelion_has_posted function after debug channel declarations
    func_anchor = "WINE_DECLARE_DEBUG_CHANNEL(relay);"
    if func_anchor not in text:
        log("ERROR", f"Anchor not found in {path}: {func_anchor!r}")
        sys.exit(1)
    text = text.replace(func_anchor, func_anchor + WIN32U_FUNCTION)

    # Modification B: prepend triskelion check to peek_message condition
    # Valve Wine:  "if (!filter->waited && NtGetTickCount() ..."
    # CachyOS Wine: "if ((using_server_or_ntsync() || !filter->waited) && NtGetTickCount() ..."
    valve_anchor = "!filter->waited && NtGetTickCount() - thread_info->last_getmsg_time < 3000"
    cachyos_anchor = "(using_server_or_ntsync() || !filter->waited) && NtGetTickCount() - thread_info->last_getmsg_time < 3000"

    if cachyos_anchor in text:
        original_condition = cachyos_anchor
    elif valve_anchor in text:
        original_condition = valve_anchor
    else:
        log("ERROR", f"Anchor not found in {path}: peek_message condition")
        sys.exit(1)

    text = text.replace(
        "        if (" + original_condition,
        PEEK_MSG_PREFIX + original_condition,
    )
    path.write_text(text)
    log("INFO", "Patched win32u/message.c: added triskelion_has_posted function + peek_message integration")


def configure_shader_cache():
    """Ask the user whether to enable per-game Vulkan shader cache optimization."""
    flag = STEAM_COMPAT_DIR / "shader_cache_enabled"

    print()
    print("  Shader cache optimization: amphetamine can configure per-game Vulkan")
    print("  shader caches (10 GB, organized per-prefix). This prevents compiled")
    print("  shaders from being evicted between sessions and reduces stutter.")
    print()
    print("  If you already manage shader cache settings yourself, say no.")
    print()

    if prompt_yn("  Enable shader cache optimization?"):
        flag.write_text("1")
        log("INFO", "Shader cache optimization enabled")
    else:
        if flag.exists():
            flag.unlink()
        log("INFO", "Shader cache optimization disabled")


def configure_custom_env():
    """Ask the user whether to create a custom environment variable config."""
    config_file = STEAM_COMPAT_DIR / "env_config"

    if config_file.exists():
        log("INFO", f"Custom env config: {config_file}")
        log("INFO", "  Edit it directly — changes apply on next game launch")
        return

    print()
    print("  Custom environment variables: amphetamine can create a config file")
    print("  where you define extra environment variables applied at game launch.")
    print("  Variables you set override amphetamine's built-in defaults.")
    print()
    print(f"  Config location: {config_file}")
    print()

    if prompt_yn("  Create custom environment config?"):
        config_file.write_text(ENV_CONFIG_TEMPLATE)
        log("INFO", f"Custom env config created: {config_file}")
        log("INFO", "  Uncomment variables to enable them")
    else:
        log("INFO", "Custom env config skipped (you can create it manually later)")


def check_ntsync():
    """Check if the kernel supports ntsync and inform the user."""
    ntsync_available = Path("/dev/ntsync").exists()
    if ntsync_available:
        log("INFO", "ntsync: /dev/ntsync available (kernel-native NT sync)")
    else:
        log("INFO", "ntsync: not available (using CAS + futex fallback)")
        log("INFO", "ntsync requires Linux 6.14+. Sync will work fine without it.")


def configure_verbose():
    """Handle --verbose / --no-verbose flags. Sticky: once enabled, stays
    enabled until explicitly disabled with --no-verbose."""
    flag = STEAM_COMPAT_DIR / "verbose_enabled"
    if "--verbose" in sys.argv:
        STEAM_COMPAT_DIR.mkdir(parents=True, exist_ok=True)
        flag.write_text("1")
        log("INFO", "Verbose diagnostics enabled (~/.cache/amphetamine/*.prom)")
    elif "--no-verbose" in sys.argv:
        if flag.exists():
            flag.unlink()
        log("INFO", "Verbose diagnostics disabled")
    elif flag.exists():
        log("INFO", "Verbose diagnostics: on (use --no-verbose to disable)")


def check_dependencies():
    """Verify all required dependencies before building."""
    ok = True

    # Rust 1.85+ (edition 2024)
    try:
        out = subprocess.run(["rustc", "--version"], capture_output=True, text=True)
        if out.returncode == 0:
            # "rustc 1.93.1 (01f6ddf75 ...)" -> "1.93.1"
            ver = out.stdout.split()[1]
            parts = ver.split(".")
            major, minor = int(parts[0]), int(parts[1])
            if major < 1 or (major == 1 and minor < 85):
                log("ERROR", f"Rust {ver} is too old. Need 1.85+ (edition 2024).")
                log("ERROR", "Update with: rustup update stable")
                ok = False
            else:
                log("INFO", f"Rust {ver}")
        else:
            log("ERROR", "rustc not found. Install Rust: https://rustup.rs")
            ok = False
    except FileNotFoundError:
        log("ERROR", "rustc not found. Install Rust: https://rustup.rs")
        ok = False

    # Git
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True)
        if out.returncode != 0:
            log("ERROR", "git not found. Install git.")
            ok = False
    except FileNotFoundError:
        log("ERROR", "git not found. Install git.")
        ok = False

    # Steam (native)
    steam_root = Path.home() / ".steam" / "root"
    if not steam_root.exists():
        log("ERROR", "Steam not found at ~/.steam/root")
        log("ERROR", "Install Steam natively (not Flatpak).")
        ok = False

    # Proton (Wine binaries)
    steam_common = Path.home() / ".steam" / "root" / "steamapps" / "common"
    proton_found = False

    proton_exp = steam_common / "Proton - Experimental" / "files" / "bin" / "wine64"
    if proton_exp.exists():
        log("INFO", f"Found Proton Experimental")
        proton_found = True
    elif steam_common.exists():
        for entry in steam_common.iterdir():
            if entry.name.startswith("Proton") and (entry / "files" / "bin" / "wine64").exists():
                log("INFO", f"Found {entry.name}")
                proton_found = True
                break

    if not proton_found:
        log("ERROR", "Proton not found. amphetamine requires Proton's Wine binaries.")
        log("ERROR", "Install 'Proton Experimental' from your Steam Library.")
        ok = False

    return ok


def main():
    configure_verbose()

    if not check_dependencies():
        return 1

    use_cachyos = detect_cachyos_wine()
    if use_cachyos:
        print()
        log("INFO", "amphetamine has detected CachyOS Wine.")
        log("INFO", "Compiling triskelion with CachyOS ntsync protocol support...")
        print()

    clone_wine(use_cachyos=use_cachyos)

    ret = build_triskelion()
    if ret != 0:
        return ret

    configure_shader_cache()
    configure_custom_env()

    check_ntsync()

    print()
    log("INFO", "Save data protection: enabled (automatic)")
    log("INFO", "  Pre-launch snapshots save data, restores if Steam Cloud sync")
    log("INFO", "  wipes files during first launch with a new compatibility tool.")
    print()

    if (WINE_SRC_DIR / "dlls").exists():
        patch_copy_triskelion_c()
        patch_makefile_in()
        patch_server_c()
        patch_unix_private_h()
        patch_win32u_message()

        log("INFO", f"Wine source patched: {WINE_SRC_DIR}")
        log("INFO", f"Next: triskelion configure {WINE_SRC_DIR} --execute && cd {WINE_SRC_DIR} && make -j$(nproc)")
    else:
        log("WARN", "Wine source not available, skipping patches")
        log("WARN", "Binary deployed to Steam -- tracing and profiling will work")

    return 0


if __name__ == "__main__":
    sys.exit(main())

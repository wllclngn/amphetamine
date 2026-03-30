#!/usr/bin/env python3
"""Build and deploy quark: triskelion binary, DXVK/VKD3D, optional ntsync ntdll, optional kernel module."""

import filecmp
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
RUST_DIR = SCRIPT_DIR / "rust"
PATCHES_DIR = SCRIPT_DIR / "patches" / "wine"

DATA_DIR = Path.home() / ".local" / "share" / "quark"
WINE_SRC_DIR = Path("/tmp/quark-wine-build/wine-src")
WINE_OBJ_DIR = Path("/tmp/quark-wine-build/wine-obj")
STEAM_COMPAT_DIR = Path.home() / ".local" / "share" / "Steam" / "compatibilitytools.d" / "quark"
WINE_CLONE_URL = "https://gitlab.winehq.org/wine/wine.git"
WINE_TAG = "wine-11.5"
PROTON_STEAM_DIR = Path.home() / ".local" / "share" / "Steam" / "steamapps" / "common" / "Proton 10.0" / "files"
EAC_RUNTIME_DIR = Path.home() / ".local" / "share" / "Steam" / "steamapps" / "common" / "Proton EasyAntiCheat Runtime" / "v2"

KMOD_SOURCE = SCRIPT_DIR / "c23"
KMOD_NAME = "triskelion_kmod"

# Essential build deps for Wine on Arch-based systems.
# WoW64 mode (--enable-archs=x86_64,i386) uses mingw for 32-bit PE DLLs,
# so lib32 system packages are NOT required.
WINE_BUILD_DEPS_ARCH = [
    # Build tools
    "base-devel", "mingw-w64-gcc", "autoconf", "bison", "flex", "perl",
    # Graphics / display
    "freetype2", "fontconfig", "vulkan-headers", "vulkan-icd-loader",
    "libx11", "libxext", "libxrandr", "libxinerama", "libxcursor",
    "libxcomposite", "libxi", "libxxf86vm",
    "wayland", "wayland-protocols",
    # Audio
    "alsa-lib", "libpulse",
    "gst-plugins-base-libs",
    # Networking / crypto
    "gnutls",
    # Input
    "sdl2",
    # Other
    "libusb", "v4l-utils",
]


def get_version():
    """Read version from rust/Cargo.toml."""
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

# Patch text: server.c bypass block (inserted before send_request)
SERVER_BYPASS = """\
    /* triskelion: ioctl relay + shared memory bypass */
    ret = triskelion_try_bypass( req_ptr );
    if (ret != 0xDEAD0001u)  /* TRISKELION_NOT_HANDLED */
        return ret;

"""

# Patch text: win32u peek_message condition prefix
PEEK_MSG_GUARD = """\
        /* triskelion: if the shm ring has pending posted messages,
         * skip check_queue_bits and force the server call path.
         * The bypass in ntdll server_call_unlocked will pop from the ring. */
        if (triskelion_has_posted(NtCurrentTeb()->glReserved2))
            ;  /* fall through to server call */
        else """


ENV_CONFIG_TEMPLATE = """\
# quark custom environment variables
#
# Format: KEY=VALUE (one per line)
# Lines starting with # are comments. Blank lines are ignored.
# Variables set here override quark's built-in defaults.
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
# --- Display driver ---
# Always winex11.drv + GLX over XWayland.
#
# --- Sync ---
# ntsync is always on — requires Linux 6.14+.
WINE_NTSYNC=1
# PROTON_NO_FSYNC=1
# WINEFSYNC_SPINCOUNT=100
#
# --- Overlays ---
# MANGOHUD=1
# MANGOHUD_CONFIG=fps,frametime,gpu_temp,cpu_temp
# DXVK_HUD=fps
#
# --- Frame rate ---
# DXVK_FRAME_RATE=0
#
# --- Upscaling ---
# WINE_FULLSCREEN_FSR=1
# WINE_FULLSCREEN_FSR_STRENGTH=2
#
# --- Performance ---
# DXVK_ASYNC=1
# mesa_glthread=true
# RADV_PERFTEST=gpl
# STAGING_SHARED_MEMORY=1
# __GL_THREADED_OPTIMIZATIONS=1
#
# --- CPU topology ---
# WINE_CPU_TOPOLOGY=8:0,1,2,3,4,5,6,7
#
# --- NVIDIA (DLSS, Reflex, NVAPI) ---
# PROTON_ENABLE_NVAPI=1
# DXVK_ENABLE_NVAPI=dxgi
# PROTON_HIDE_NVIDIA_GPU=0
#
# --- Gamescope ---
# ENABLE_GAMESCOPE_WSI=1
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


def get_latest_wine_tag():
    """Query upstream Wine for the latest stable release tag (e.g. wine-11.4)."""
    out = subprocess.run(
        ["git", "ls-remote", "--tags", WINE_CLONE_URL],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        log("ERROR", "Failed to query Wine release tags")
        return None

    # Match stable tags: wine-X.Y (no -rc, no -dev)
    tag_re = re.compile(r"refs/tags/(wine-(\d+)\.(\d+))$")
    tags = []
    for line in out.stdout.splitlines():
        m = tag_re.search(line)
        if m:
            tags.append((int(m.group(2)), int(m.group(3)), m.group(1)))

    if not tags:
        log("ERROR", "No stable Wine tags found")
        return None

    tags.sort(reverse=True)
    return tags[0][2]


def build_triskelion():
    log("INFO", "Building quark stack (3 binaries)...")
    cmd = ["cargo", "build", "--release", "-p", "triskelion"]
    ret = subprocess.run(cmd, cwd=SCRIPT_DIR).returncode
    if ret != 0:
        log("ERROR", "Build failed (cargo error)")
        return ret

    target_dir = SCRIPT_DIR / "target" / "release"

    for name in ["quark", "triskelion", "parallax"]:
        binary = target_dir / name
        if not binary.exists():
            log("ERROR", f"Binary not found: {binary}")
            return 1
        dest = SCRIPT_DIR / name
        shutil.copy2(binary, dest)
        os.chmod(dest, 0o755)

    # Deploy all three binaries to Steam compatibility tools directory
    STEAM_COMPAT_DIR.mkdir(parents=True, exist_ok=True)
    for name in ["quark", "triskelion", "parallax"]:
        src = target_dir / name
        dst = STEAM_COMPAT_DIR / name
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)

    # Steam VDF expects "proton" — symlink to quark (the launcher)
    proton_link = STEAM_COMPAT_DIR / "proton"
    if proton_link.exists() or proton_link.is_symlink():
        proton_link.unlink()
    proton_link.symlink_to("quark")
    log("INFO", "Deployed: quark, triskelion, parallax (proton -> quark)")

    # Write VDF with current version
    version = get_version()
    vdf = STEAM_COMPAT_DIR / "compatibilitytool.vdf"
    vdf.write_text(f'''"compatibilitytools"
{{
  "compat_tools"
  {{
    "quark"
    {{
      "install_path" "."
      "display_name" "quark {version}"
      "from_oslist"  "windows"
      "to_oslist"    "linux"
    }}
  }}
}}
''')
    log("INFO", f"Updated VDF: quark {version}")

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


def get_system_wine_version():
    """Get the installed system Wine version tag (e.g. 'wine-11.3')."""
    try:
        out = subprocess.run(["wine", "--version"], capture_output=True, text=True)
        if out.returncode == 0:
            # Output is like "wine-11.3"
            return out.stdout.strip()
    except FileNotFoundError:
        pass
    return None


def clone_wine():
    # Use Proton 10.0's Wine fork — provides fsync, ntsync, and protocol 856
    # which matches our triskelion server code and Proton's PE DLLs.
    tag = WINE_TAG

    if (WINE_SRC_DIR / "dlls").exists():
        # Check if existing clone matches the target version
        cloned_tag = None
        try:
            out = subprocess.run(
                ["git", "describe", "--tags", "--exact-match"],
                cwd=WINE_SRC_DIR, capture_output=True, text=True,
            )
            if out.returncode == 0:
                cloned_tag = out.stdout.strip()
        except Exception:
            pass
        if cloned_tag == tag:
            log("INFO", "Wine source: already cloned")
            return
        else:
            log("WARN", f"Wine source version mismatch: have {cloned_tag or '???'}, need {tag}")
            log("INFO", "Re-cloning Wine source...")
            shutil.rmtree(WINE_SRC_DIR)
            # Also nuke the obj dir — config.h and build artifacts are version-specific
            if WINE_OBJ_DIR.exists():
                shutil.rmtree(WINE_OBJ_DIR)

    log("INFO", f"Cloning Proton Wine ({tag})...")
    WINE_SRC_DIR.parent.mkdir(parents=True, exist_ok=True)
    ret = subprocess.run([
        "git", "clone", "--depth", "1", "-b", tag,
        WINE_CLONE_URL, str(WINE_SRC_DIR),
    ]).returncode
    if ret != 0:
        log("ERROR", "Clone failed — GitLab may be down, retry later")
        return
    log("INFO", f"Clone complete: {WINE_SRC_DIR}")


def patch_copy_triskelion_c():
    src = PATCHES_DIR / "dlls" / "ntdll" / "unix" / "triskelion.c"
    dst = WINE_SRC_DIR / "dlls" / "ntdll" / "unix" / "triskelion.c"
    if dst.exists() and filecmp.cmp(src, dst, shallow=False):
        log("INFO", "triskelion.c: already patched")
        return
    shutil.copy2(src, dst)
    log("INFO", "Patched triskelion.c")


def patch_makefile_in():
    path = WINE_SRC_DIR / "dlls" / "ntdll" / "Makefile.in"
    text = path.read_text()
    if "unix/triskelion.c" in text:
        log("INFO", "Makefile.in: already patched")
        return
    anchor = "\tunix/thread.c \\"
    if anchor not in text:
        log("ERROR", f"Anchor not found in {path}: {anchor!r}")
        sys.exit(1)
    text = text.replace(anchor, anchor + "\n\tunix/triskelion.c \\")
    path.write_text(text)
    log("INFO", "Patched Makefile.in: added unix/triskelion.c")


def patch_server_c():
    """Apply safe patches to server.c: diagnostics + socket guard fix.
    Does NOT add kernel module bypass hooks — those go in patch_server_c_bypass()."""
    path = WINE_SRC_DIR / "dlls" / "ntdll" / "unix" / "server.c"
    text = path.read_text()
    patched = False

    # Diagnostic: log every Wine process's server_init_process entry
    # Writes per-PID files so we can trace which processes reach init and
    # what their WINESERVERSOCKET is set to (critical for child process debugging).
    if "triskelion_server_init_diag" not in text:
        diag_anchor = "    server_pid = -1;\n    if (env_socket)"
        if diag_anchor not in text:
            log("WARN", "server_init_process diagnostic: anchor not found, skipping")
        else:
            text = text.replace(diag_anchor,
                '    server_pid = -1;\n'
                '\n'
                '    /* triskelion_server_init_diag: trace every Wine process init */\n'
                '    {\n'
                '        char diagpath[256];\n'
                '        snprintf( diagpath, sizeof(diagpath), "/tmp/quark/wine_init_%d.log", getpid() );\n'
                '        FILE *diag = fopen( diagpath, "w" );\n'
                '        if (diag)\n'
                '        {\n'
                '            fprintf( diag, "server_init_process: pid=%d WINESERVERSOCKET=%s\\n",\n'
                '                     getpid(), env_socket ? env_socket : "(none)" );\n'
                '            int fd;\n'
                '            for (fd = 0; fd < 10; fd++)\n'
                '            {\n'
                '                struct stat st;\n'
                '                if (fstat( fd, &st ) == 0)\n'
                '                    fprintf( diag, "  fd=%d type=0x%x\\n", fd, (unsigned)(st.st_mode & S_IFMT) );\n'
                '                else\n'
                '                    fprintf( diag, "  fd=%d CLOSED\\n", fd );\n'
                '            }\n'
                '            fclose( diag );\n'
                '        }\n'
                '    }\n'
                '\n'
                '    if (env_socket)')
            patched = True

    # Fix: WINESERVERSOCKET=0 clobber protection.
    # Wine's create_process does socketpair() which can return fd 0 if something
    # in Wine's init closed it. The child's set_stdio_fd then replaces fd 0 with
    # /dev/null. After exec, server_init_process tries recvmsg on /dev/null → crash.
    # Fix: verify the fd is actually a socket. If not, call server_connect() directly.
    if "triskelion_wineserversocket_guard" not in text:
        guard_anchor = (
            '        fd_socket = atoi( env_socket );\n'
            '        if (fcntl( fd_socket, F_SETFD, FD_CLOEXEC ) == -1)\n'
            '            fatal_perror( "Bad server socket %d", fd_socket );\n'
            '        unsetenv( "WINESERVERSOCKET" );'
        )
        if guard_anchor not in text:
            log("WARN", "WINESERVERSOCKET guard: anchor not found, skipping")
        else:
            text = text.replace(guard_anchor,
                '        /* triskelion_wineserversocket_guard: verify fd is a real socket.\n'
                '         * set_stdio_fd can replace fd 0 with /dev/null after fork. */\n'
                '        fd_socket = atoi( env_socket );\n'
                '        {\n'
                '            struct stat st;\n'
                '            if (fstat( fd_socket, &st ) != 0 || (st.st_mode & S_IFMT) != S_IFSOCK)\n'
                '            {\n'
                '                fd_socket = server_connect();\n'
                '            }\n'
                '            else\n'
                '            {\n'
                '                if (fcntl( fd_socket, F_SETFD, FD_CLOEXEC ) == -1)\n'
                '                    fatal_perror( "Bad server socket %d", fd_socket );\n'
                '                unsetenv( "WINESERVERSOCKET" );\n'
                '            }\n'
                '        }')
            patched = True

    if patched:
        path.write_text(text)
        log("INFO", "Patched server.c: diagnostics + socket guard")
    else:
        log("INFO", "server.c: already patched")


def patch_server_c_bypass():
    """Apply kernel module bypass hooks to server.c.
    Only call this when building for kernel module mode (not Rust daemon mode)."""
    path = WINE_SRC_DIR / "dlls" / "ntdll" / "unix" / "server.c"
    text = path.read_text()
    patched = False

    # Pre-hook: triskelion_try_bypass before send_request
    if "triskelion_try_bypass" not in text:
        anchor = "    if ((ret = send_request( req ))) return ret;\n    return wait_reply( req );"
        if anchor not in text:
            log("ERROR", f"Anchor not found in {path}: send_request/wait_reply block")
            sys.exit(1)
        text = text.replace(anchor,
            SERVER_BYPASS + anchor)
        patched = True

    # Post-hook: triskelion_post_call after wait_reply (ntsync shadow creation)
    if "triskelion_post_call" not in text:
        post_anchor = "    return wait_reply( req );"
        if post_anchor not in text:
            log("ERROR", f"Anchor not found in {path}: return wait_reply")
            sys.exit(1)
        text = text.replace(post_anchor,
            "    ret = wait_reply( req );\n"
            "    /* triskelion: shadow newly created sync objects with ntsync */\n"
            "    triskelion_post_call( req_ptr, ret );\n"
            "    return ret;")
        patched = True

    if patched:
        path.write_text(text)
        log("INFO", "Patched server.c: triskelion_try_bypass + triskelion_post_call")
    else:
        log("INFO", "server.c: bypass hooks already patched")


def patch_unix_private_h():
    path = WINE_SRC_DIR / "dlls" / "ntdll" / "unix" / "unix_private.h"
    text = path.read_text()
    patched = False

    if "triskelion_try_bypass" not in text:
        anchor = "extern unsigned int server_call_unlocked( void *req_ptr );"
        if anchor not in text:
            log("ERROR", f"Anchor not found in {path}: {anchor!r}")
            sys.exit(1)
        text = text.replace(anchor, anchor +
            "\nextern unsigned int triskelion_try_bypass( void *req_ptr );")
        patched = True

    if "triskelion_post_call" not in text:
        anchor2 = "extern unsigned int triskelion_try_bypass( void *req_ptr );"
        text = text.replace(anchor2, anchor2 +
            "\nextern void triskelion_post_call( void *req_ptr, unsigned int ret );")
        patched = True

    if patched:
        path.write_text(text)
        log("INFO", "Patched unix_private.h: triskelion declarations")
    else:
        log("INFO", "unix_private.h: already patched")


def patch_win32u_message():
    path = WINE_SRC_DIR / "dlls" / "win32u" / "message.c"
    text = path.read_text()
    if "triskelion_has_posted" in text:
        log("INFO", "win32u/message.c: already patched")
        return

    # Modification A: insert triskelion_has_posted function after debug channel declarations
    func_anchor = "WINE_DECLARE_DEBUG_CHANNEL(relay);"
    if func_anchor not in text:
        log("ERROR", f"Anchor not found in {path}: {func_anchor!r}")
        sys.exit(1)
    text = text.replace(func_anchor, func_anchor + WIN32U_FUNCTION)

    # Modification B: prepend triskelion check to check_queue_bits condition
    original_condition = "if (check_queue_bits( wake_mask, filter->mask, wake_mask | signal_bits, filter->mask | clear_bits,"
    if original_condition not in text:
        log("ERROR", f"Anchor not found in {path}: check_queue_bits condition")
        sys.exit(1)

    text = text.replace(
        "        " + original_condition,
        PEEK_MSG_GUARD + original_condition,
    )
    path.write_text(text)
    log("INFO", "Patched win32u/message.c: triskelion_has_posted + peek_message bypass")


def patch_win32u_sysparams():
    """Soften user_check_not_lock assertion to a warning.

    The assertion fires when load_desktop_driver is called while USER lock is held.
    This is a race condition in driver initialization — sometimes thread A (no lock)
    loads the driver first (safe), sometimes thread B (lock held) gets there first (crash).
    With triskelion (no explorer.exe), the race is more likely to manifest.

    The assertion prevents potential deadlocks, but with triskelion the deadlock
    doesn't actually occur — the second run always succeeds when the race doesn't hit.
    Converting to a warning lets driver loading proceed safely.
    """
    path = WINE_SRC_DIR / "dlls" / "win32u" / "sysparams.c"
    text = path.read_text()
    if "triskelion: softened" in text:
        log("INFO", "win32u/sysparams.c: already patched")
        return

    original = '        ERR( "BUG: holding USER lock\\n" );\n        assert( 0 );'
    replacement = '        ERR( "BUG: holding USER lock (triskelion: softened to warning)\\n" );\n        /* assert( 0 ); -- triskelion: softened to warning, race in driver init */'
    if original not in text:
        log("ERROR", f"Anchor not found in {path}: user_check_not_lock assert")
        sys.exit(1)

    text = text.replace(original, replacement)
    path.write_text(text)
    log("INFO", "Patched win32u/sysparams.c: user_check_not_lock assert → warning")


def configure_shader_cache():
    """Shader cache is now part of .quark_cache. This is a no-op for compat."""
    pass


def configure_custom_env():
    """Create/update env_config with forced defaults."""
    config_file = STEAM_COMPAT_DIR / "env_config"

    if config_file.exists():
        # Ensure forced values are present in existing config
        text = config_file.read_text()
        changed = False
        # Force ntsync
        if "WINE_NTSYNC=1" not in text or text.count("# WINE_NTSYNC=1") > 0:
            text = text.replace("# WINE_NTSYNC=1", "WINE_NTSYNC=1")
            if "WINE_NTSYNC=1" not in text:
                text += "\n# --- Sync (forced) ---\nWINE_NTSYNC=1\n"
            changed = True
        # Display driver: always winex11.drv + GLX over XWayland.
        if changed:
            config_file.write_text(text)
            log("INFO", f"Custom env config: updated forced defaults ({config_file})")
        else:
            log("INFO", f"Custom env config: found ({config_file})")
        log("INFO", "  Edit directly — changes apply on next game launch")
        return

    # Fresh install: generate from template
    config_file.write_text(ENV_CONFIG_TEMPLATE)
    log("INFO", f"Custom env config: created ({config_file})")
    log("INFO", "  Variables you set override quark's built-in defaults")


def find_steam_libraries():
    """Discover all Steam library folders (deduplicated by resolved path)."""
    steam_dirs = [
        Path.home() / ".local" / "share" / "Steam",
        Path.home() / ".steam" / "root",
    ]
    seen = set()
    libraries = []
    for steam in steam_dirs:
        steamapps = steam / "steamapps"
        if not steamapps.exists():
            continue
        resolved = steamapps.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        libraries.append(steamapps)
        # Parse libraryfolders.vdf for additional library paths
        vdf = steamapps / "libraryfolders.vdf"
        if vdf.exists():
            try:
                text = vdf.read_text(errors="replace")
                for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                    lib = Path(m.group(1)) / "steamapps"
                    if lib.exists():
                        lib_resolved = lib.resolve()
                        if lib_resolved not in seen:
                            seen.add(lib_resolved)
                            libraries.append(lib)
            except OSError:
                pass
    return libraries


# Engine detection: map DLL signatures to engine names
ENGINE_SIGNATURES = {
    "love2d":    ["love.dll", "lua51.dll"],
    "unity":     ["UnityPlayer.dll", "UnityEngine.dll"],
    "unreal4":   ["UE4-Win64-Shipping.exe"],
    "unreal5":   ["UnrealEditor.dll"],
    "sdl2":      ["SDL2.dll"],
    "godot":     ["godot.windows.opt.64.exe"],
    "gamemaker": ["data.win"],
    "rpgmaker":  ["RPGMV.dll", "nw.dll"],
    "ren'py":    ["renpy.dll", "lib/py3-win64"],
    "source2":   ["engine2.dll"],
    "source":    ["engine.dll", "hl2.exe"],
    "cryengine": ["CrySystem.dll"],
    "frostbite": ["FrostyEditor.dll"],
}


def detect_engine(install_dir: Path) -> str:
    """Fingerprint game engine from files in the install directory.

    Root-level files (depth 0) are checked first for primary engine DLLs.
    Subdirectory files (depth 1) are checked for secondary signatures.
    This avoids false positives from bundled tools (e.g. Elden Ring ships a
    Unity-based Adventure Guide, but the game itself is FROM SOFTWARE's engine).
    """
    try:
        root_files = set()
        sub_files = set()
        for f in install_dir.iterdir():
            if f.is_file():
                root_files.add(f.name.lower())
            elif f.is_dir():
                for f2 in f.iterdir():
                    if f2.is_file():
                        sub_files.add(f2.name.lower())
    except (OSError, PermissionError):
        return "unknown"

    # Primary detection: root-level files only (avoids bundled tool false positives)
    PRIMARY_SIGNATURES = {
        "love2d":    ["love.dll"],
        "unity":     ["unityplayer.dll"],
        "godot":     ["godot.windows.opt.64.exe"],
        "source2":   ["engine2.dll"],
        "source":    ["engine.dll", "hl2.exe"],
    }
    for engine, sigs in PRIMARY_SIGNATURES.items():
        if any(s in root_files for s in sigs):
            return engine

    # Secondary detection: root + subdirectory files
    all_files = root_files | sub_files
    for engine, sigs in ENGINE_SIGNATURES.items():
        if engine in PRIMARY_SIGNATURES:
            continue  # already checked at root level
        if any(s.lower() in all_files for s in sigs):
            return engine
    return "unknown"


def parse_appmanifest(path: Path) -> dict:
    """Parse a Steam appmanifest_*.acf file for key fields."""
    text = path.read_text(errors="replace")
    result = {}
    for key in ("appid", "name", "installdir"):
        m = re.search(rf'"{key}"\s+"([^"]+)"', text)
        if m:
            result[key] = m.group(1)
    return result


# Non-game appids to skip (runtimes, tools, redistributables)
NON_GAME_NAMES = {
    "steam linux runtime", "proton", "steamworks common redistributables",
}


# ── Unified Binary Cache (.quark_cache) ────────────────────────────────

OPCODE_COUNT = 306
CACHE_SIZE = 10_496  # 64 + 128 + 4896 + 4896 + 512

ENGINE_TYPE_MAP = {
    "unknown": 0, "unity": 1, "unreal4": 2, "unreal5": 3,
    "source": 4, "source2": 5, "godot": 6, "gamemaker": 7,
    "rpgmaker": 8, "ren'py": 9, "love2d": 10, "sdl2": 11,
    "cryengine": 12, "frostbite": 13,
}

# Engine-specific opcode profiles: opcode → (hint, priority)
# Hint values: 0=unknown, 1=critical, 2=needed, 3=safe_to_stub, 4=never_called
ENGINE_OPCODE_PROFILES = {
    "unity": {
        44: (0x01, 100),   # CreateFile — CRITICAL (asset loading)
        63: (0x01, 100),   # CreateMapping — CRITICAL (mmap'd bundles)
        49: (0x01, 90),    # GetDirectoryCacheEntry — CRITICAL (DLL search)
        29: (0x01, 100),   # Select — CRITICAL (core wait)
        30: (0x01, 80),    # CreateEvent — CRITICAL (thread sync)
        149: (0x03, 0),    # CreateNamedPipe — SAFE_TO_STUB
    },
    "unreal4": {
        29: (0x01, 100),   # Select — CRITICAL
        30: (0x01, 100),   # CreateEvent — CRITICAL (event-heavy sync)
        31: (0x01, 90),    # EventOp — CRITICAL
        40: (0x01, 80),    # CreateSemaphore — CRITICAL
        44: (0x01, 90),    # CreateFile — CRITICAL
    },
    "unreal5": {
        29: (0x01, 100),   # Select — CRITICAL
        30: (0x01, 100),   # CreateEvent — CRITICAL
        31: (0x01, 90),    # EventOp — CRITICAL
        40: (0x01, 80),    # CreateSemaphore — CRITICAL
        44: (0x01, 90),    # CreateFile — CRITICAL
    },
    "source2": {
        55: (0x02, 70),    # RecvSocket — NEEDED
        56: (0x02, 70),    # SendSocket — NEEDED
        60: (0x03, 0),     # GetNextConsoleRequest — SAFE_TO_STUB
    },
    "godot": {
        29: (0x01, 100),   # Select — CRITICAL
        44: (0x01, 90),    # CreateFile — CRITICAL
        30: (0x01, 80),    # CreateEvent — CRITICAL
    },
    "love2d": {
        29: (0x01, 100),   # Select — CRITICAL
        44: (0x01, 80),    # CreateFile — CRITICAL
    },
}


def write_quark_cache(cache_path, appid, engine, primary_exe=""):
    """Write a fresh .quark_cache binary file (10,496 bytes)."""

    buf = bytearray(CACHE_SIZE)
    now = int(time.time())
    engine_type = ENGINE_TYPE_MAP.get(engine, 0)
    flags = 0x01  # engine_detected

    # Header (64 bytes @ 0x0000)
    struct.pack_into("<4sIIIIIIII",
        buf, 0,
        b"AMPC", 2, int(appid), flags, engine_type,
        0, 0, now, 0,  # run_count, last_run, install_epoch, coverage
    )

    # Engine Profile (128 bytes @ 0x0040)
    engine_name = engine.encode("utf-8")[:31] + b"\x00"
    exe_name = primary_exe.encode("utf-8")[:63] + b"\x00"
    struct.pack_into("<32s64sII",
        buf, 0x0040,
        engine_name, exe_name,
        0, 80 if engine != "unknown" else 0,
    )

    # Opcode Intelligence (4896 bytes @ 0x00C0)
    profile = ENGINE_OPCODE_PROFILES.get(engine, {})
    for opcode in range(OPCODE_COUNT):
        offset = 0x00C0 + opcode * 16
        hint, priority = profile.get(opcode, (0x00, 0))
        engine_stub_safe = 1 if hint == 0x03 else 0
        struct.pack_into("<IHBx8x", buf, offset, hint, priority, engine_stub_safe)

    if profile:
        flags |= 0x02  # profiles_seeded
        struct.pack_into("<I", buf, 12, flags)

    # Shader Cache Index (512 bytes @ 0x2700)
    # Scan for existing DXVK/VKD3D caches in compatdata
    compat_dir = cache_path.parent
    for f in compat_dir.rglob("*.dxvk-cache"):
        try:
            rel = str(f.relative_to(compat_dir))[:191]
            sz = f.stat().st_size
            struct.pack_into("<192s", buf, 0x2700, rel.encode("utf-8"))
            struct.pack_into("<Q", buf, 0x2700 + 384, sz)
            flags |= 0x08  # has_shader_index
            struct.pack_into("<I", buf, 12, flags)
        except (OSError, ValueError):
            pass
        break
    for f in compat_dir.rglob("vkd3d-proton.cache"):
        try:
            rel = str(f.relative_to(compat_dir))[:191]
            sz = f.stat().st_size
            struct.pack_into("<192s", buf, 0x2700 + 192, rel.encode("utf-8"))
            struct.pack_into("<Q", buf, 0x2700 + 392, sz)
            flags |= 0x08
            struct.pack_into("<I", buf, 12, flags)
        except (OSError, ValueError):
            pass
        break

    cache_path.write_bytes(buf)


def generate_game_intelligence():
    """Discover installed Steam games and write .quark_cache for quark-mapped ones only.

    ONLY touches compatdata for games mapped to quark in Steam's CompatToolMapping.
    Never writes into Proton/other compat tool prefixes — their prefix directories are theirs."""
    log("INFO", "Game caches: scanning Steam libraries...")

    quark_appids = _get_quark_appids()

    libraries = find_steam_libraries()
    if not libraries:
        log("WARN", "Game caches: no Steam libraries found")
        return

    games = []
    for steamapps in libraries:
        for manifest in sorted(steamapps.glob("appmanifest_*.acf")):
            info = parse_appmanifest(manifest)
            appid = info.get("appid")
            if not appid:
                continue
            name = info.get("name", "")
            if any(skip in name.lower() for skip in NON_GAME_NAMES):
                continue
            install_dir = steamapps / "common" / info.get("installdir", "")
            if not install_dir.exists():
                continue
            engine = detect_engine(install_dir)
            games.append({
                "appid": appid,
                "name": info.get("name", "?"),
                "install_dir": install_dir,
                "engine": engine,
                "steamapps": steamapps,
            })

    if not games:
        log("INFO", "Game caches: no installed games found")
        return

    created = 0
    engine_counts = {}
    for game in games:
        engine = game["engine"]
        engine_counts[engine] = engine_counts.get(engine, 0) + 1

        # ONLY write cache for quark-mapped games — never touch other compat tools' prefixes
        if game["appid"] not in quark_appids:
            continue

        # Resolve compatdata dir for this game
        compatdata = game["steamapps"] / "compatdata" / game["appid"]
        if not compatdata.exists():
            continue  # game hasn't been launched yet

        cache_path = compatdata / ".quark_cache"
        if cache_path.exists():
            continue  # don't overwrite learned data

        write_quark_cache(cache_path, game["appid"], engine)
        created += 1

    log("INFO", f"Game caches: {created} new caches created ({len(games)} games, {len(quark_appids)} mapped to quark)")
    for engine, count in sorted(engine_counts.items(), key=lambda x: -x[1]):
        log("INFO", f"  {engine}: {count} game{'s' if count > 1 else ''}")


def require_ntsync():
    """Verify ntsync support. Linux 6.14+ required. Stops installation if missing."""
    kver = os.uname().release
    parts = kver.split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        log("ERROR", f"ntsync: cannot parse kernel version: {kver}")
        log("ERROR", "  quark requires Linux 6.14+ for /dev/ntsync")
        return False

    if major < 6 or (major == 6 and minor < 14):
        log("ERROR", f"ntsync: kernel {kver} too old — requires 6.14+")
        log("ERROR", "  /dev/ntsync provides kernel-native NT sync primitives")
        log("ERROR", "  quark has zero fallback sync mechanisms")
        log("ERROR", "  Update your kernel to 6.14+ to use quark")
        return False

    log("INFO", f"ntsync: kernel {kver} — requirement met (6.14+)")

    if Path("/dev/ntsync").exists():
        log("INFO", "ntsync: /dev/ntsync available — kernel-native NT sync enabled")
    else:
        log("WARN", "ntsync: /dev/ntsync not present — try: sudo modprobe ntsync")

    return True


def install_wine_build_deps():
    """Install Wine build dependencies via pacman (Arch-based only)."""
    try:
        subprocess.run(["pacman", "--version"], capture_output=True)
    except FileNotFoundError:
        log("ERROR", "pacman: not found — Wine build requires Arch-based system")
        log("ERROR", "  Install deps manually, then re-run install.py")
        return False

    missing = []
    for pkg in WINE_BUILD_DEPS_ARCH:
        ret = subprocess.run(["pacman", "-Q", pkg], capture_output=True, text=True)
        if ret.returncode != 0:
            missing.append(pkg)

    if not missing:
        log("INFO", "Wine build deps: all installed")
        return True

    log("INFO", f"Wine build deps: {len(missing)} packages needed")
    for pkg in missing:
        print(f"    {pkg}")

    if not prompt_yn(f"\n  Install {len(missing)} packages via pacman?"):
        log("WARN", "Wine build cancelled — missing dependencies")
        return False

    ret = subprocess.run(
        ["sudo", "pacman", "-S", "--needed", "--noconfirm"] + missing,
    ).returncode
    if ret != 0:
        log("ERROR", "Wine build deps: install failed")
        return False

    log("INFO", "Wine build deps: installed")
    return True


def deploy_system_wine():
    """Deploy system Wine binaries — wineserver replaced with triskelion.
    All .so files, loaders, and PE DLLs come from system Wine (/usr/lib/wine/).
    DXVK/VKD3D DLLs are sourced from Proton at runtime by the launcher.

    Two Wine sources:
    - wine-valve build (~/.cache/quark/wine-valve-build64): protocol 864, NVIDIA xwayland fixes
    - system Wine (/usr/lib/wine): protocol 930
    Default: wine-valve if built, else system Wine."""

    # Check for wine-valve build (protocol 864, xwayland + NVIDIA)
    wine_valve_build = Path.home() / ".cache" / "quark" / "wine-valve-build64"
    wine_valve_bin = wine_valve_build / "wine"
    wine_valve_unix = wine_valve_build / "dlls"  # DLLs are under dlls/*/name.so
    use_wine_valve = wine_valve_bin.exists() and (wine_valve_build / "dlls" / "ntdll" / "ntdll.so").exists()

    if use_wine_valve:
        log("INFO", "Using wine-valve build (protocol 864, NVIDIA xwayland fixes)")
    else:
        log("INFO", "Using system Wine (protocol 930). Build wine-valve for xwayland NVIDIA support.")

    sys_unix = Path("/usr/lib/wine/x86_64-unix")
    sys_bin = Path("/usr/bin")

    if not sys_unix.exists() or not (sys_bin / "wine").exists():
        log("ERROR", "System Wine not found at /usr/lib/wine/ and /usr/bin/wine")
        log("ERROR", "  Install Wine from your package manager (e.g. pacman -S wine)")
        return False

    # ── bin/ — system wine loader + wineserver → triskelion ──
    bin_dir = STEAM_COMPAT_DIR / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    for name in ["wine", "wine-preloader"]:
        src = sys_bin / name
        dst = bin_dir / name
        if src.exists():
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            # COPY (not symlink) — Wine resolves symlinks and looks for wineserver
            # relative to the REAL path. If we symlink to /usr/bin/wine, Wine finds
            # /usr/bin/wineserver (stock) instead of our bin/wineserver (triskelion).
            # Copying ensures Wine's real path is in our bin/ dir.
            shutil.copy2(src, dst)

    wine64_link = bin_dir / "wine64"
    if wine64_link.exists() or wine64_link.is_symlink():
        wine64_link.unlink()
    wine64_link.symlink_to("wine")

    wineserver_link = bin_dir / "wineserver"
    if wineserver_link.exists() or wineserver_link.is_symlink():
        wineserver_link.unlink()
    triskelion_bin = STEAM_COMPAT_DIR / "triskelion"
    wineserver_link.symlink_to(triskelion_bin)

    # ── lib/wine/ — hardlink files from system Wine (NOT symlink dirs) ──
    # Wine 11.4's ntdll resolves its own path via realpath(dladdr()).
    # If we symlink the dir → /usr/lib/wine/x86_64-unix, realpath resolves
    # ntdll.so to /usr/lib/wine/... and Wine computes bin_dir=/usr/bin,
    # finding /usr/bin/wineserver (stock) instead of our bin/wineserver (triskelion).
    # Hardlinks keep the same inode (zero disk overhead) but realpath resolves
    # to quark's tree → Wine finds quark/bin/wineserver → triskelion.
    wine_lib = STEAM_COMPAT_DIR / "lib" / "wine"
    wine_lib.mkdir(parents=True, exist_ok=True)

    for subdir in ["x86_64-unix", "x86_64-windows", "i386-windows", "i386-unix"]:
        src = Path("/usr/lib/wine") / subdir
        dst = wine_lib / subdir
        if not src.exists():
            continue
        if dst.is_symlink():
            dst.unlink()
        elif dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True, exist_ok=True)
        hardlinked = 0
        copied = 0
        for f in src.iterdir():
            if f.is_file():
                target = dst / f.name
                if target.exists():
                    target.unlink()  # always replace — prevents stale DLLs
                try:
                    os.link(f, target)  # hardlink: same inode, zero copy
                    hardlinked += 1
                except OSError:
                    shutil.copy2(f, target)  # cross-device fallback
                    copied += 1
        if copied > 0:
            log("WARN", f"  {subdir}: {copied} files COPIED (cross-device, won't auto-update on Wine upgrade)")
            log("WARN", f"  Re-run install.py after Wine updates to refresh DLLs")
        # Verify critical DLL matches system — catch stale copies
        for critical in ["ntdll.dll", "ntdll.so"]:
            sys_file = src / critical
            our_file = dst / critical
            if sys_file.exists() and our_file.exists():
                if sys_file.stat().st_size != our_file.stat().st_size:
                    log("ERROR", f"  {subdir}/{critical} SIZE MISMATCH! sys={sys_file.stat().st_size} ours={our_file.stat().st_size}")
                    log("ERROR", f"  This WILL cause crashes. Fixing...")
                    if our_file.exists():
                        our_file.unlink()
                    shutil.copy2(sys_file, our_file)

    # ── share/ — NLS, wine.inf from system Wine ──
    share_wine = STEAM_COMPAT_DIR / "share" / "wine"
    share_wine.mkdir(parents=True, exist_ok=True)

    for name in ["nls", "wine.inf"]:
        dst = share_wine / name
        src = Path("/usr/share/wine") / name
        if src.exists():
            if dst.is_symlink():
                dst.unlink()
            if not dst.exists():
                dst.symlink_to(src)

    # steam.exe is built as a Wine builtin from Proton's steam_helper by build_lsteamclient().
    # The old c23/steam_bridge.c (mingw native PE) is retired — Wine builtins require
    # the Wine PE signature that only winegcc produces.

    proton_files = None
    for name in ["Proton 10.0", "Proton - Experimental"]:
        p = Path.home() / ".local/share/Steam/steamapps/common" / name / "files"
        if p.exists():
            proton_files = p
            break

    # lsteamclient is built from source by build_lsteamclient() later.
    # steam.exe is our steam_bridge (deployed above). No Proton copies needed.

    log("INFO", f"System Wine deployed (symlinks to /usr/lib/wine/ + /usr/bin/wine)")

    # ── wine-valve overlay: NVIDIA xwayland fixes (protocol 864) ──
    if use_wine_valve:
        deployed = 0
        # wine-valve .so files: build64/dlls/name/name.so → lib/wine/x86_64-unix/name.so
        for dll_dir in sorted(wine_valve_build.glob("dlls/*")):
            so_name = dll_dir.name
            # Handle .drv extensions: winex11.drv → winex11.so
            so_file = dll_dir / f"{so_name}.so"
            if not so_file.exists():
                # Try without .drv suffix
                base = so_name.replace(".drv", "")
                so_file = dll_dir / f"{base}.so"
            if so_file.exists():
                dst = wine_lib / "x86_64-unix" / f"{so_name.replace('.drv', '')}.so"
                if dst.parent.exists():
                    shutil.copy2(so_file, dst)
                    deployed += 1
        # wine-valve PE DLLs: build64/dlls/name/name.dll → lib/wine/x86_64-windows/name.dll
        for dll_dir in sorted(wine_valve_build.glob("dlls/*")):
            dll_name = dll_dir.name
            for ext in [".dll", ".exe", ".drv", ".sys", ".ocx", ".cpl", ".acm", ".ax"]:
                pe_file = dll_dir / f"{dll_name}{ext}"
                if pe_file.exists():
                    dst = wine_lib / "x86_64-windows" / pe_file.name
                    if dst.parent.exists():
                        shutil.copy2(pe_file, dst)
                        deployed += 1
        # wine-valve wine binary
        if wine_valve_bin.exists():
            shutil.copy2(wine_valve_bin, bin_dir / "wine")
            shutil.copy2(wine_valve_bin, bin_dir / "wine64")
            deployed += 2
        log("INFO", f"wine-valve: deployed {deployed} files over system Wine")

    # ── Patched Wine DLLs — build from source with quark patches ──
    if not use_wine_valve:
        build_and_deploy_patched_wine(wine_lib)

    return True


def build_and_deploy_patched_wine(wine_lib: Path):
    """Build patched Wine .so files from source and overlay onto deployed system Wine.

    1. Clone Wine 11.5 to /tmp if not present
    2. Apply patches from patches/wine/ via git apply
    3. Configure and build patched ntdll.so + win32u.so
    4. Build lsteamclient.dll (PE) + lsteamclient.so (Unix, manual g++)
    5. Deploy over system Wine copies
    """
    patch_dir = SCRIPT_DIR / "patches" / "wine"
    patches = sorted(patch_dir.glob("*.patch")) if patch_dir.exists() else []
    if not patches:
        log("INFO", "No Wine patches found — using stock DLLs")
        return

    src_dir = WINE_SRC_DIR
    build_dir = WINE_OBJ_DIR

    # ── Clone Wine 11.5 if needed ──
    if not (src_dir / "dlls" / "win32u").exists():
        log("INFO", f"Cloning Wine {WINE_TAG} to {src_dir}...")
        src_dir.parent.mkdir(parents=True, exist_ok=True)
        if src_dir.exists():
            shutil.rmtree(src_dir)
        ret = subprocess.run([
            "git", "clone", "--depth", "1", "-b", WINE_TAG,
            WINE_CLONE_URL, str(src_dir),
        ]).returncode
        if ret != 0:
            log("ERROR", "Wine clone failed")
            return

    # ── Apply patches ──
    # Reset to clean state first
    subprocess.run(["git", "checkout", "HEAD", "--", "."], cwd=src_dir, capture_output=True)
    subprocess.run(["git", "clean", "-fd"], cwd=src_dir, capture_output=True)

    applied = 0
    for patch in patches:
        ret = subprocess.run(
            ["git", "apply", "--check", str(patch)],
            cwd=src_dir, capture_output=True
        )
        if ret.returncode == 0:
            subprocess.run(["git", "apply", str(patch)], cwd=src_dir, capture_output=True)
            applied += 1
            log("INFO", f"  Applied: {patch.name}")
        else:
            log("WARN", f"  Patch failed: {patch.name}")

    if applied == 0:
        log("WARN", "No patches applied — using stock DLLs")
        return
    log("INFO", f"Applied {applied}/{len(patches)} Wine patches")

    # ── Configure ──
    if not (build_dir / "Makefile").exists():
        log("INFO", "Configuring Wine build...")
        build_dir.mkdir(parents=True, exist_ok=True)
        # autoreconf needed if configure.ac was patched (lsteamclient registration)
        subprocess.run(["autoreconf", "-f"], cwd=src_dir, capture_output=True)
        ret = subprocess.run(
            [str(src_dir / "configure"), "--prefix=/usr", "--enable-win64", "--with-x", "--without-wayland"],
            cwd=build_dir, capture_output=True,
        )
        if ret.returncode != 0:
            log("ERROR", "Wine configure failed")
            return

    # ── Build patched .so files ──
    # winex11.drv: CachyOS system Wine is built Wayland-only (no winex11.drv.so).
    # Proton ships its own x11drv for XWayland — proven path for NVIDIA + GLX.
    # We build it from Wine 11.5 source, same as Valve does.
    # Credit: Valve/Proton's approach of bundling x11drv for XWayland compatibility.
    # Each entry: (unix .so build path, unix install name, optional PE .dll build path, PE install name)
    dll_targets = {
        "ntdll": ("dlls/ntdll/ntdll.so", "ntdll.so",
                   "dlls/ntdll/x86_64-windows/ntdll.dll", "ntdll.dll"),
        "win32u": ("dlls/win32u/win32u.so", "win32u.so", None, None),
        "winex11.drv": ("dlls/winex11.drv/winex11.so", "winex11.so", None, None),
    }

    deployed = 0
    for dll, (so_path, so_name, pe_path, pe_name) in dll_targets.items():
        build_targets = [so_path]
        if pe_path:
            build_targets.append(pe_path)
        log("INFO", f"  Building {dll}...")
        ret = subprocess.run(
            ["make", f"-j{os.cpu_count() or 4}"] + build_targets,
            cwd=build_dir, capture_output=True, timeout=300,
        )
        built_so = build_dir / so_path
        if built_so.exists():
            dst = wine_lib / "x86_64-unix" / so_name
            if dst.exists():
                dst.unlink()
            shutil.copy2(built_so, dst)
            deployed += 1
            log("INFO", f"  Deployed {so_name} ({built_so.stat().st_size // 1024}K)")
        else:
            log("WARN", f"  Build failed for {dll} (.so)")
        if pe_path:
            built_pe = build_dir / pe_path
            if built_pe.exists():
                dst = wine_lib / "x86_64-windows" / pe_name
                if dst.exists():
                    dst.unlink()
                shutil.copy2(built_pe, dst)
                log("INFO", f"  Deployed {pe_name} ({built_pe.stat().st_size // 1024}K)")
            else:
                log("WARN", f"  Build failed for {dll} (.dll)")

    # lsteamclient is built by build_lsteamclient() against wine-src (protocol 930).
    # Do NOT build it here — the old manual g++ path produced ABI-incompatible .so files.

    # Build tier0/vstdlib stubs (needed for steamclient.dll imports)
        stub_c = Path("/tmp/quark_stub.c")
        stub_c.write_text('#include <windows.h>\nBOOL WINAPI DllMain(HINSTANCE i, DWORD r, void *v) { return TRUE; }\n')
        # 64-bit stubs
        for stub_name in ["tier0_s64", "vstdlib_s64"]:
            stub_dll = wine_lib / "x86_64-windows" / f"{stub_name}.dll"
            subprocess.run([
                "x86_64-w64-mingw32-gcc", "-shared", "-o", str(stub_dll), str(stub_c),
            ], capture_output=True)
            if stub_dll.exists():
                log("INFO", f"    {stub_name}.dll stub deployed (64-bit)")
        # 32-bit stubs (steamclient.dll is 32-bit PE)
        i386_dir = wine_lib / "i386-windows"
        i386_dir.mkdir(parents=True, exist_ok=True)
        for stub_name in ["tier0_s", "vstdlib_s"]:
            stub_dll = i386_dir / f"{stub_name}.dll"
            subprocess.run([
                "i686-w64-mingw32-gcc", "-shared", "-o", str(stub_dll), str(stub_c),
            ], capture_output=True)
            if stub_dll.exists():
                log("INFO", f"    {stub_name}.dll stub deployed (32-bit)")

    if deployed:
        log("INFO", f"Deployed {deployed} patched Wine module(s)")
    else:
        log("WARN", "No patched modules built successfully")


def _get_quark_appids():
    """Read Steam's config.vdf to find which appids use quark as compat tool."""
    config_vdf = Path.home() / ".local/share/Steam/config/config.vdf"
    if not config_vdf.exists():
        return set()
    try:
        content = config_vdf.read_text()
        import re
        # VDF has nested braces: "CompatToolMapping" { "appid" { ... } "appid" { ... } }
        # Find the CompatToolMapping block start, then brace-match to find the end.
        start = content.find('"CompatToolMapping"')
        if start < 0:
            return set()
        # Find the opening brace
        brace_start = content.find('{', start)
        if brace_start < 0:
            return set()
        # Brace-match to find the closing brace (handles nested braces)
        depth = 1
        i = brace_start + 1
        while i < len(content) and depth > 0:
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
            i += 1
        block = content[brace_start + 1:i - 1]
        pairs = re.findall(r'"(\d+)"\s*\{[^}]*"name"\s*"quark"', block)
        return set(pairs)
    except Exception:
        return set()


def sync_prefix_dlls():
    """Sync system Wine DLLs into quark-managed prefix system32 directories.

    ONLY touches prefixes mapped to quark in Steam's CompatToolMapping.
    Proton prefixes are NEVER modified — their DLLs are ABI-incompatible with
    system Wine and overwriting them destroys the prefix.

    Triskelion uses system Wine, which REQUIRES matching DLLs.
    This runs once at install time — no per-launch cost.
    """
    quark_appids = _get_quark_appids()
    if not quark_appids:
        log("INFO", "No games mapped to quark — skipping prefix DLL sync")
        return

    sys_dlls = Path("/usr/lib/wine/x86_64-windows")
    if not sys_dlls.exists():
        log("WARN", "System Wine x86_64-windows not found — skipping prefix sync")
        return

    sys_ntdll = sys_dlls / "ntdll.dll"
    if not sys_ntdll.exists():
        return
    sys_hash = hashlib.md5(sys_ntdll.read_bytes()).hexdigest()

    compatdata = Path.home() / ".local/share/Steam/steamapps/compatdata"
    if not compatdata.exists():
        return

    # Known Proton appids — never touch these even if misconfigured
    PROTON_APPIDS = {"3658110", "1493710", "2805730", "1628350",  # Proton 10.0, Exp, 9.0, SLR sniper
                     "1580130", "1887720", "2180100", "2348590"}  # Proton 8.0, 7.0, hotfix, EAC
    synced_prefixes = 0
    for entry in compatdata.iterdir():
        if not entry.is_dir():
            continue
        appid = entry.name
        if appid in PROTON_APPIDS:
            continue  # Hardcoded safety: never touch Proton runtime prefixes
        if appid not in quark_appids:
            continue
        # Validate prefix is actually under compatdata (paranoia check)
        try:
            resolved = entry.resolve()
            if not str(resolved).startswith(str(compatdata.resolve())):
                log("WARN", f"  Prefix {appid}: path escapes compatdata ({resolved}) — skipping")
                continue
        except OSError:
            continue
        sys32 = entry / "pfx/drive_c/windows/system32"
        if not sys32.exists():
            continue
        pfx_ntdll = sys32 / "ntdll.dll"
        if not pfx_ntdll.exists():
            continue

        # Check if DLLs already match
        pfx_hash = hashlib.md5(pfx_ntdll.read_bytes()).hexdigest()
        if pfx_hash == sys_hash:
            continue

        # Mismatch — sync all x86_64-windows DLLs
        copied = 0
        for dll in sys_dlls.iterdir():
            if dll.is_file() and dll.suffix == ".dll":
                target = sys32 / dll.name
                # Safety: only write into system32
                if target.parent.name != "system32":
                    continue
                if target.exists():
                    try:
                        target.chmod(0o755)
                    except OSError:
                        pass
                try:
                    shutil.copy2(dll, target)
                    copied += 1
                except OSError:
                    pass

        # Also sync i386-windows (syswow64)
        syswow64 = entry / "pfx/drive_c/windows/syswow64"
        sys32_dlls = Path("/usr/lib/wine/i386-windows")
        if syswow64.exists() and sys32_dlls.exists():
            for dll in sys32_dlls.iterdir():
                if dll.is_file() and dll.suffix == ".dll":
                    target = syswow64 / dll.name
                    # Safety: only write into syswow64
                    if target.parent.name != "syswow64":
                        continue
                    if target.exists():
                        try:
                            target.chmod(0o755)
                        except OSError:
                            pass
                    try:
                        shutil.copy2(dll, target)
                        copied += 1
                    except OSError:
                        pass

        log("INFO", f"  Prefix {appid} (mapped: quark): synced {copied} DLLs (was {pfx_hash[:8]}, now {sys_hash[:8]})")
        synced_prefixes += 1

    if synced_prefixes:
        log("INFO", f"Synced {synced_prefixes} prefix(es) to system Wine DLLs")
    else:
        log("INFO", "All quark prefixes already match system Wine DLLs")



# winewayland build removed — always winex11.drv + GLX over XWayland.



def download_github_release(owner, repo, asset_glob):
    """Download latest release asset from GitHub. Returns path to cached tarball or None."""
    cache_dir = DATA_DIR / "downloads"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Check what we already have cached
    version_file = cache_dir / f"{repo}.version"
    cached_version = version_file.read_text().strip() if version_file.exists() else None

    # Query GitHub API for latest release
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log("WARN", f"{repo}: failed to query GitHub API: {e}")
        # Fall back to cached version if available
        existing = list(cache_dir.glob(f"{repo}-*.tar.*"))
        if existing:
            log("INFO", f"{repo}: using cached download")
            return existing[0]
        return None

    tag = data.get("tag_name", "")
    if tag == cached_version:
        existing = list(cache_dir.glob(f"{repo}-*.tar.*"))
        if existing:
            log("INFO", f"{repo}: {tag} (cached)")
            return existing[0]

    # Find matching asset
    download_url = None
    asset_name = None
    for asset in data.get("assets", []):
        name = asset["name"]
        if asset_glob in name and name.endswith((".tar.gz", ".tar.xz", ".tar.zst")):
            download_url = asset["browser_download_url"]
            asset_name = name
            break

    if not download_url:
        log("WARN", f"{repo}: no matching release asset found (looking for '{asset_glob}')")
        return None

    # Clean old cached versions
    for old in cache_dir.glob(f"{repo}-*.tar.*"):
        old.unlink()

    dest = cache_dir / asset_name
    log("INFO", f"{repo}: downloading {tag}...")
    try:
        urllib.request.urlretrieve(download_url, dest)
    except Exception as e:
        log("ERROR", f"{repo}: download failed: {e}")
        return None

    version_file.write_text(tag)
    log("INFO", f"{repo}: downloaded {asset_name}")
    return dest


def download_dxvk_vkd3d():
    """Download DXVK and VKD3D-proton from GitHub releases."""
    dxvk_tar = download_github_release("doitsujin", "dxvk", "dxvk-")
    vkd3d_tar = download_github_release("HansKristian-Work", "vkd3d-proton", "vkd3d-proton-")
    return dxvk_tar, vkd3d_tar


def deploy_dxvk_vkd3d(dxvk_tar, vkd3d_tar):
    """Extract DXVK and VKD3D-proton DLLs into quark's lib directory."""
    lib_dir = STEAM_COMPAT_DIR / "lib"

    if dxvk_tar:
        _deploy_tarball_dlls(dxvk_tar, lib_dir, "dxvk", "x64", "x32")
    if vkd3d_tar:
        _deploy_tarball_dlls(vkd3d_tar, lib_dir, "vkd3d-proton", "x64", "x86")


def _deploy_tarball_dlls(tarball, lib_dir, label, dir_64, dir_32):
    """Extract DLLs from a DXVK or VKD3D-proton tarball into lib/wine/{label}/."""
    staging = DATA_DIR / "staging" / label
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(tarball) as tf:
            try:
                tf.extractall(staging, filter="data")
            except TypeError:
                # Python < 3.12: no filter parameter
                tf.extractall(staging)
    except Exception as e:
        log("ERROR", f"{label}: failed to extract tarball: {e}")
        return

    # Find the extracted directory (e.g., dxvk-2.5.3/)
    extracted = list(staging.iterdir())
    if not extracted:
        log("ERROR", f"{label}: tarball extracted empty")
        return
    root = extracted[0]

    # Deploy 64-bit DLLs
    src_64 = root / dir_64
    dst_64 = lib_dir / "wine" / label / "x86_64-windows"
    if src_64.exists():
        dst_64.mkdir(parents=True, exist_ok=True)
        count = 0
        for dll in src_64.glob("*.dll"):
            shutil.copy2(dll, dst_64 / dll.name)
            count += 1
        log("INFO", f"{label}: deployed {count} 64-bit DLLs")

    # Deploy 32-bit DLLs
    src_32 = root / dir_32
    dst_32 = lib_dir / "wine" / label / "i386-windows"
    if src_32.exists():
        dst_32.mkdir(parents=True, exist_ok=True)
        count = 0
        for dll in src_32.glob("*.dll"):
            shutil.copy2(dll, dst_32 / dll.name)
            count += 1
        log("INFO", f"{label}: deployed {count} 32-bit DLLs")

    # Cleanup staging
    shutil.rmtree(staging, ignore_errors=True)


def find_proton():
    """Find Proton installation (optional). Returns path to files/ dir or None."""
    steam_common = Path.home() / ".steam" / "root" / "steamapps" / "common"

    proton_exp = steam_common / "Proton - Experimental" / "files"
    if (proton_exp / "bin").exists():
        return proton_exp

    if steam_common.exists():
        for entry in steam_common.iterdir():
            if entry.name.startswith("Proton"):
                files = entry / "files"
                if (files / "bin").exists():
                    return files

    return None


def deploy_steam_exe():
    """steam.exe is deployed by build_lsteamclient() as a Wine builtin."""
    bridge = STEAM_COMPAT_DIR / "lib" / "wine" / "x86_64-windows" / "steam.exe"
    if bridge.exists():
        log("INFO", f"steam.exe: Wine builtin ({bridge.stat().st_size // 1024}K)")
    else:
        log("INFO", "steam.exe: will be deployed by build_lsteamclient()")


### ── lsteamclient build (Steam API bridge) ─────────────────────────────


LSTEAMCLIENT_CACHE = DATA_DIR / "lsteamclient"
PROTON_GIT_URL = "https://github.com/ValveSoftware/Proton.git"
PROTON_GIT_TAG = "proton-10.0-4"
PROTON_CACHE = Path.home() / ".cache" / "quark" / "Proton"


def build_lsteamclient():
    """Build lsteamclient.dll from Proton source against Wine 11.5.

    lsteamclient bridges Windows Steam API calls to the native Linux Steam client.
    Built as a standard Wine DLL using Wine's build system against wine-src
    (protocol 930, matching system Wine runtime).

    Build steps:
    1. Get Proton source (cached in ~/.cache/quark/Proton/)
    2. Symlink lsteamclient/ into wine-src/dlls/
    3. Register in configure.ac (adds WINE_CONFIG_MAKEFILE entry)
    4. autoconf + configure + make in /tmp
    5. Cache and deploy the built DLL
    """
    dll_cache = LSTEAMCLIENT_CACHE / "lsteamclient.dll"
    so_cache = LSTEAMCLIENT_CACHE / "lsteamclient.so"

    # Already built? Deploy from cache.
    steam_cache = LSTEAMCLIENT_CACHE / "steam.exe"
    if dll_cache.exists() and so_cache.exists() and steam_cache.exists():
        log("INFO", f"lsteamclient: cached ({dll_cache.stat().st_size // 1024}K dll, {so_cache.stat().st_size // 1024}K so, {steam_cache.stat().st_size // 1024}K steam.exe)")
        _deploy_lsteamclient(dll_cache, so_cache)
        steam_dst = STEAM_COMPAT_DIR / "lib" / "wine" / "x86_64-windows" / "steam.exe"
        if steam_dst.exists():
            steam_dst.chmod(0o755)
        shutil.copy2(steam_cache, steam_dst)
        log("INFO", f"steam.exe: deployed Wine builtin ({steam_cache.stat().st_size // 1024}K)")
        return True

    # Need Proton source for lsteamclient
    proton_lsteamclient = PROTON_CACHE / "lsteamclient"
    if not proton_lsteamclient.exists():
        log("INFO", "lsteamclient: cloning Proton source (shallow)...")
        tmp_proton = Path("/tmp/proton-src-lsteamclient")
        if tmp_proton.exists():
            shutil.rmtree(tmp_proton)
        ret = subprocess.run([
            "git", "clone", "--depth", "1", "-b", PROTON_GIT_TAG,
            PROTON_GIT_URL, str(tmp_proton),
        ]).returncode
        if ret != 0:
            log("ERROR", "lsteamclient: failed to clone Proton source")
            return False
        PROTON_CACHE.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tmp_proton / "lsteamclient", proton_lsteamclient)
        if (tmp_proton / "steam_helper").exists():
            shutil.copytree(tmp_proton / "steam_helper", PROTON_CACHE / "steam_helper")
        shutil.rmtree(tmp_proton)
        log("INFO", "lsteamclient: Proton source cached")

    # Build against wine-src (protocol 930, matches system Wine runtime).
    # NOT wine-valve (protocol 864) — ABI mismatch causes wine client errors.
    wine_src = WINE_SRC_DIR
    if not wine_src.exists():
        log("WARN", "lsteamclient: wine-src not found — run install.py with Wine patches first")
        return False

    # Reset wine-src configure.ac to clean state
    subprocess.run(["git", "checkout", "--", "configure.ac"],
                   cwd=wine_src, capture_output=True)

    # Symlink into wine-src tree
    dll_link = wine_src / "dlls" / "lsteamclient"
    if dll_link.is_symlink():
        dll_link.unlink()
    if not dll_link.exists():
        dll_link.symlink_to(proton_lsteamclient)
        log("INFO", "lsteamclient: symlinked into wine-src/dlls/")

    # Apply configure.ac registration (idempotent)
    configure_ac = wine_src / "configure.ac"
    if "dlls/lsteamclient" not in configure_ac.read_text():
        text = configure_ac.read_text()
        text = text.replace(
            "WINE_CONFIG_MAKEFILE(dlls/lz32/tests)",
            "WINE_CONFIG_MAKEFILE(dlls/lz32/tests)\nWINE_CONFIG_MAKEFILE(dlls/lsteamclient)",
        )
        configure_ac.write_text(text)
        log("INFO", "lsteamclient: registered in configure.ac")

    # Reset Proton source to clean state before patching.
    # Patches modify lsteamclient source (symlinked into wine-src).
    subprocess.run(["git", "checkout", "--", "lsteamclient/"],
                   cwd=PROTON_CACHE, capture_output=True)

    # Apply source patches for Wine 11.5 API compatibility.
    # Patches use paths relative to the Proton cache root (a/lsteamclient/...).
    # --forward skips already-applied patches (idempotent).
    patch_dir = SCRIPT_DIR / "patches"
    for pname in ["006-lsteamclient-wine11-api-compat.patch", "007-lsteamclient-link-stdcxx.patch", "008-lsteamclient-non-fatal-steam-crash.patch"]:
        pfile = patch_dir / pname
        if pfile.exists():
            ret = subprocess.run(["patch", "-Np1", "--forward", "-i", str(pfile)],
                           cwd=PROTON_CACHE,
                           capture_output=True, text=True)
            combined = (ret.stdout or "") + (ret.stderr or "")
            if ret.returncode == 0:
                log("INFO", f"  Applied: {pname}")
            elif "already applied" in combined or "Reversed" in combined:
                log("INFO", f"  Already applied: {pname}")
            else:
                log("WARN", f"  Patch failed: {pname}")
                for line in (ret.stderr or "").split("\n")[-5:]:
                    if line.strip():
                        log("WARN", f"    {line}")

    # Patch 008 handles assert removal and RaiseException removal via GNU Quilt.

    # Also symlink steam_helper if available
    steam_helper_src = PROTON_CACHE / "steam_helper"
    if steam_helper_src.exists():
        helper_link = wine_src / "programs" / "steam_helper"
        if helper_link.is_symlink():
            helper_link.unlink()
        if not helper_link.exists():
            helper_link.symlink_to(steam_helper_src)
        if "programs/steam_helper" not in configure_ac.read_text():
            text = configure_ac.read_text()
            text = text.replace(
                "WINE_CONFIG_MAKEFILE(programs/start)",
                "WINE_CONFIG_MAKEFILE(programs/start)\nWINE_CONFIG_MAKEFILE(programs/steam_helper)",
            )
            configure_ac.write_text(text)

    # Build in /tmp
    build_dir = Path("/tmp/quark-lsteamclient-build")
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    # autoconf
    log("INFO", "lsteamclient: running autoconf...")
    ret = subprocess.run(["autoconf"], cwd=wine_src, capture_output=True).returncode
    if ret != 0:
        log("ERROR", "lsteamclient: autoconf failed")
        return False

    # configure against wine-src (protocol 930)
    log("INFO", "lsteamclient: configuring (protocol 930)...")
    ret = subprocess.run(
        [str(wine_src / "configure"), "--enable-win64", "--disable-tests"],
        cwd=build_dir, capture_output=True,
    ).returncode
    if ret != 0:
        log("ERROR", "lsteamclient: configure failed")
        return False

    # Build targets
    dll_target = build_dir / "dlls" / "lsteamclient" / "x86_64-windows" / "lsteamclient.dll"
    so_target = build_dir / "dlls" / "lsteamclient" / "lsteamclient.so"
    steam_target = build_dir / "programs" / "steam_helper" / "x86_64-windows" / "steam.exe"

    targets = [
        "dlls/lsteamclient/x86_64-windows/lsteamclient.dll",
        "dlls/lsteamclient/lsteamclient.so",
    ]
    if steam_helper_src.exists():
        targets.append("programs/steam_helper/x86_64-windows/steam.exe")

    log("INFO", "lsteamclient: building...")
    ret = subprocess.run(
        ["make", f"-j{os.cpu_count()}"] + targets,
        cwd=build_dir, capture_output=True, text=True,
    ).returncode
    if ret != 0:
        log("ERROR", "lsteamclient: build failed — retrying for error output")
        result = subprocess.run(
            ["make"] + targets,
            cwd=build_dir, capture_output=True, text=True,
        )
        for line in (result.stderr or "").split("\n")[-15:]:
            if line.strip():
                log("ERROR", f"  {line}")
        return False

    # Report sizes
    for label, path in [("lsteamclient.dll", dll_target), ("lsteamclient.so", so_target), ("steam.exe", steam_target)]:
        if path.exists():
            log("INFO", f"  {label}: {path.stat().st_size // 1024}K")

    # Cache
    LSTEAMCLIENT_CACHE.mkdir(parents=True, exist_ok=True)
    if dll_target.exists():
        shutil.copy2(dll_target, dll_cache)
    so_cache = LSTEAMCLIENT_CACHE / "lsteamclient.so"
    if so_target.exists():
        shutil.copy2(so_target, so_cache)
    steam_cache = LSTEAMCLIENT_CACHE / "steam.exe"
    if steam_target.exists():
        shutil.copy2(steam_target, steam_cache)

    # Clean up build tree
    shutil.rmtree(build_dir)
    log("INFO", "lsteamclient: build tree cleaned")

    _deploy_lsteamclient(dll_cache, so_cache if so_cache.exists() else None)

    # Deploy steam.exe (Wine builtin)
    if steam_cache.exists():
        steam_dst = STEAM_COMPAT_DIR / "lib" / "wine" / "x86_64-windows" / "steam.exe"
        if steam_dst.exists():
            steam_dst.chmod(0o755)
        shutil.copy2(steam_cache, steam_dst)
        log("INFO", f"steam.exe: deployed Wine builtin ({steam_cache.stat().st_size // 1024}K)")

    return True


def _deploy_lsteamclient(dll_path, so_path=None):
    """Deploy lsteamclient.dll + .so to the quark Wine lib directory."""
    # PE side → x86_64-windows
    win_dir = STEAM_COMPAT_DIR / "lib" / "wine" / "x86_64-windows"
    win_dir.mkdir(parents=True, exist_ok=True)
    dst = win_dir / "lsteamclient.dll"
    if dst.exists():
        dst.chmod(0o755)
    shutil.copy2(dll_path, dst)

    # Unix side → x86_64-unix (the actual Steam IPC bridge)
    if so_path and so_path.exists():
        unix_dir = STEAM_COMPAT_DIR / "lib" / "wine" / "x86_64-unix"
        unix_dir.mkdir(parents=True, exist_ok=True)
        dst_so = unix_dir / "lsteamclient.so"
        if dst_so.exists():
            dst_so.chmod(0o755)
        shutil.copy2(so_path, dst_so)
        log("INFO", f"lsteamclient: deployed PE + Unix to {STEAM_COMPAT_DIR / 'lib/wine/'}")
    else:
        log("INFO", f"lsteamclient: deployed PE only to {win_dir}")


### ── Kernel module (kmod/) ──────────────────────────────────────────────


def get_kernel_version():
    return os.uname().release


def check_kernel_headers(kver):
    return Path(f"/lib/modules/{kver}/build").exists()


def detect_kernel_compiler(kver):
    """Return extra make args if the kernel was built with LLVM."""
    auto_conf = Path(f"/lib/modules/{kver}/build/include/config/auto.conf")
    if auto_conf.exists():
        try:
            text = auto_conf.read_text()
            if "CONFIG_CC_IS_CLANG=y" in text:
                return ["LLVM=1"]
        except OSError:
            pass
    return []


def find_kmod_tool(name):
    path = shutil.which(name)
    if path:
        return path
    for sbin in ("/usr/sbin", "/sbin"):
        candidate = os.path.join(sbin, name)
        if os.path.isfile(candidate):
            return candidate
    return name


def get_module_vermagic(ko_path):
    """Extract vermagic kernel version from a .ko file."""
    modinfo = find_kmod_tool("modinfo")
    out = subprocess.run([modinfo, str(ko_path)], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if line.startswith("vermagic:"):
            return line.split()[1]
    return None


def build_and_install_kernel_module():
    """Build, install, and load the triskelion kernel module."""
    if not KMOD_SOURCE.exists():
        log("ERROR", f"Kernel module source not found: {KMOD_SOURCE}")
        return False

    kver = get_kernel_version()

    if not check_kernel_headers(kver):
        log("ERROR", f"Kernel headers not found for {kver}")
        log("ERROR", "  Arch: sudo pacman -S linux-headers  (or linux-cachyos-headers)")
        log("ERROR", "  Debian/Ubuntu: sudo apt install linux-headers-$(uname -r)")
        log("ERROR", "  Fedora: sudo dnf install kernel-devel")
        return False

    # Space-in-path handling: kernel build system can't handle spaces in M=
    build_src = KMOD_SOURCE
    tmp_build = Path("/tmp/triskelion-kmod")
    if " " in str(KMOD_SOURCE):
        log("INFO", "Staging kernel source to /tmp (path has spaces)...")
        if tmp_build.exists():
            shutil.rmtree(tmp_build)
        exclude = {".o", ".ko", ".mod", ".mod.c", ".mod.o", ".order", ".symvers"}
        shutil.copytree(
            KMOD_SOURCE, tmp_build,
            ignore=shutil.ignore_patterns(
                "*.o", "*.ko", "*.mod*", ".tmp*",
                "Module.symvers", "modules.order",
            ),
        )
        build_src = tmp_build

    # Detect compiler toolchain
    cc_args = detect_kernel_compiler(kver)
    kbuild = f"/lib/modules/{kver}/build"

    # Clean + build
    log("INFO", f"Building kernel module ({kver})...")
    make_base = ["make", "-C", kbuild, f"M={build_src}"] + cc_args

    subprocess.run(make_base + ["clean"], capture_output=True)
    result = subprocess.run(make_base + ["modules"], capture_output=True, text=True)

    if result.returncode != 0:
        log("ERROR", "Kernel module build failed:")
        for line in result.stderr.splitlines():
            if "error:" in line:
                log("ERROR", f"  {line.strip()}")
        return False

    ko_path = build_src / f"{KMOD_NAME}.ko"
    if not ko_path.exists():
        log("ERROR", f"{KMOD_NAME}.ko not found after build")
        return False

    log("INFO", f"Built: {KMOD_NAME}.ko ({ko_path.stat().st_size // 1024} KB)")

    # Verify vermagic matches running kernel
    vermagic = get_module_vermagic(ko_path)
    if vermagic and vermagic != kver:
        log("ERROR", f"Vermagic mismatch: module={vermagic}, kernel={kver}")
        log("ERROR", "  Install matching kernel headers and rebuild")
        return False

    # Install
    log("INFO", "Installing kernel module (sudo required)...")

    ret = subprocess.run(["sudo", "mkdir", "-p", f"/lib/modules/{kver}/extra"]).returncode
    if ret != 0:
        log("ERROR", "Failed to create module directory")
        return False

    ret = subprocess.run([
        "sudo", "cp", str(ko_path), f"/lib/modules/{kver}/extra/{KMOD_NAME}.ko"
    ]).returncode
    if ret != 0:
        log("ERROR", "Failed to copy kernel module")
        return False

    depmod = find_kmod_tool("depmod")
    subprocess.run(["sudo", depmod, "-a"], capture_output=True)

    # Auto-load on boot
    conf_path = f"/etc/modules-load.d/{KMOD_NAME}.conf"
    proc = subprocess.Popen(
        ["sudo", "tee", conf_path],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
    )
    proc.communicate(input=f"{KMOD_NAME}\n".encode())

    # Load module
    rmmod = find_kmod_tool("rmmod")
    modprobe = find_kmod_tool("modprobe")

    # Unload if already loaded
    check = subprocess.run(["lsmod"], capture_output=True, text=True)
    if KMOD_NAME in check.stdout:
        subprocess.run(["sudo", rmmod, KMOD_NAME], capture_output=True)

    ret = subprocess.run(["sudo", modprobe, KMOD_NAME]).returncode
    if ret != 0:
        log("ERROR", "Failed to load kernel module")
        return False

    # Verify
    if Path("/dev/triskelion").exists():
        log("INFO", "/dev/triskelion: live")
    else:
        log("WARN", "/dev/triskelion not found — module loaded but device missing")

    check = subprocess.run(["lsmod"], capture_output=True, text=True)
    if KMOD_NAME in check.stdout:
        log("INFO", f"Kernel module loaded: {KMOD_NAME}")
        log("INFO", f"  Auto-load on boot: {conf_path}")
        log("INFO", "  Re-run installer after kernel upgrades!")
        return True

    log("ERROR", "Kernel module failed to load")
    return False


def uninstall_kernel_module():
    """Unload, remove, and clean up the triskelion kernel module."""
    kver = get_kernel_version()
    ko_path = Path(f"/lib/modules/{kver}/extra/{KMOD_NAME}.ko")
    conf_path = Path(f"/etc/modules-load.d/{KMOD_NAME}.conf")
    removed = False

    # Unload if loaded
    check = subprocess.run(["lsmod"], capture_output=True, text=True)
    if KMOD_NAME in check.stdout:
        rmmod = find_kmod_tool("rmmod")
        subprocess.run(["sudo", rmmod, KMOD_NAME])
        log("INFO", f"Unloaded: {KMOD_NAME}")

    if ko_path.exists():
        subprocess.run(["sudo", "rm", str(ko_path)])
        removed = True

    if conf_path.exists():
        subprocess.run(["sudo", "rm", str(conf_path)])
        removed = True

    if removed:
        depmod = find_kmod_tool("depmod")
        subprocess.run(["sudo", depmod, "-a"], capture_output=True)
        log("INFO", "Kernel module uninstalled")

    return True


def resolve_kernel_headers():
    """Check if kernel headers are available for module build. Returns True/False."""
    if not KMOD_SOURCE.exists():
        return False

    kver = get_kernel_version()
    if not check_kernel_headers(kver):
        log("INFO", f"Kernel headers not found for {kver} — cannot build kernel module")
        log("INFO", "  Arch: sudo pacman -S linux-headers  (or linux-cachyos-headers)")
        log("INFO", "  Debian/Ubuntu: sudo apt install linux-headers-$(uname -r)")
        log("INFO", "  Fedora: sudo dnf install kernel-devel")
        return False

    log("INFO", f"Kernel headers found for {kver}")
    return True


def configure_verbose():
    """Handle --verbose / --no-verbose flags. Sticky: once enabled, stays
    enabled until explicitly disabled with --no-verbose."""
    flag = STEAM_COMPAT_DIR / "verbose_enabled"
    if "--verbose" in sys.argv:
        STEAM_COMPAT_DIR.mkdir(parents=True, exist_ok=True)
        flag.write_text("1")
        log("INFO", "Verbose diagnostics enabled (~/.cache/quark/*.prom)")
    elif "--no-verbose" in sys.argv:
        if flag.exists():
            flag.unlink()
        log("INFO", "Verbose diagnostics disabled")
    elif flag.exists():
        log("INFO", "Verbose diagnostics: on (use --no-verbose to disable)")


def check_dependencies():
    """Verify required dependencies: Rust, Git, Steam, system Wine."""
    ok = True

    # Rust 1.85+ (edition 2024)
    try:
        out = subprocess.run(["rustc", "--version"], capture_output=True, text=True)
        if out.returncode == 0:
            ver = out.stdout.split()[1]
            parts = ver.split(".")
            major, minor = int(parts[0]), int(parts[1])
            if major < 1 or (major == 1 and minor < 85):
                log("ERROR", f"Rust: {ver} — requires 1.85+ (edition 2024)")
                log("ERROR", "  Update: rustup update stable")
                ok = False
            else:
                log("INFO", f"Rust: {ver}")
        else:
            log("ERROR", "Rust: not found")
            log("ERROR", "  Install from https://rustup.rs")
            ok = False
    except FileNotFoundError:
        log("ERROR", "Rust: not found")
        log("ERROR", "  Install from https://rustup.rs")
        ok = False

    # Git
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True)
        if out.returncode == 0:
            log("INFO", "Git: found")
        else:
            log("ERROR", "Git: not found")
            ok = False
    except FileNotFoundError:
        log("ERROR", "Git: not found")
        ok = False

    # Steam (native)
    steam_root = Path.home() / ".steam" / "root"
    if steam_root.exists():
        log("INFO", "Steam: found")
    else:
        log("ERROR", "Steam: not found (~/.steam/root)")
        log("ERROR", "  Install Steam natively (not Flatpak)")
        ok = False

    # Proton (optional — only needed for xwayland path's steam.exe)
    if PROTON_STEAM_DIR.exists():
        log("INFO", f"Proton 10.0: found ({PROTON_STEAM_DIR})")
    else:
        log("INFO", "Proton 10.0: not installed (steam.exe will be fetched from GitHub if needed)")

    return ok


def kill_running_servers():
    """Kill any running triskelion daemon or wineserver processes.
    Must happen BEFORE cleaning files to avoid 'Text file busy' errors."""
    killed = []
    for name in ["triskelion", "wineserver"]:
        ret = subprocess.run(["pkill", "-9", name], capture_output=True)
        if ret.returncode == 0:
            killed.append(name)
    if killed:
        import time
        time.sleep(0.5)  # let processes actually die
        log("INFO", f"Killed running processes: {', '.join(killed)}")


def clean_runtime_state():
    """Remove ALL runtime artifacts — logs, sockets, shm segments, traces.
    These are the ghosts of old runs that cause 'still running old binary' problems."""
    cleaned = []

    # /tmp/quark/ — daemon logs, debug logs, traces, wine_init dumps
    tmp_dir = Path("/tmp/quark")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
        cleaned.append(str(tmp_dir))

    # /tmp/.wine-<uid>/server-*/socket — stale Unix sockets
    # Only clean sockets, not the entire server dir (Wine recreates it)
    uid = os.getuid()
    wine_tmp = Path(f"/tmp/.wine-{uid}")
    if wine_tmp.exists():
        for server_dir in wine_tmp.glob("server-*"):
            sock = server_dir / "socket"
            lock = server_dir / "lock"
            if sock.exists():
                sock.unlink()
                cleaned.append(str(sock))
            if lock.exists():
                lock.unlink()
                cleaned.append(str(lock))

    # /dev/shm/triskelion-* — shared memory segments from old daemon runs
    shm_dir = Path("/dev/shm")
    for shm_file in shm_dir.glob("triskelion-*"):
        shm_file.unlink()
        cleaned.append(str(shm_file))

    if cleaned:
        log("INFO", f"Cleaned {len(cleaned)} runtime artifacts")


def detect_old_builds():
    """Detect existing quark artifacts and offer to clean them before building.
    When user says yes, EVERYTHING goes. No per-item prompts. Clean slate."""
    found = []

    def dir_size(path):
        try:
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        except OSError:
            return 0

    # Steam deployment
    if STEAM_COMPAT_DIR.exists():
        found.append(("Steam compat tool", STEAM_COMPAT_DIR, dir_size(STEAM_COMPAT_DIR)))

    # Cargo build cache (workspace level)
    cargo_target = SCRIPT_DIR / "target"
    if cargo_target.exists():
        found.append(("Cargo build cache", cargo_target, dir_size(cargo_target)))

    # Wine source clone
    if WINE_SRC_DIR.exists():
        found.append(("Wine source clone", WINE_SRC_DIR, dir_size(WINE_SRC_DIR)))

    # Wine build objects
    if WINE_OBJ_DIR.exists():
        found.append(("Wine build objects", WINE_OBJ_DIR, dir_size(WINE_OBJ_DIR)))

    # Cached downloads (DXVK, VKD3D tarballs, steam.exe)
    downloads_dir = DATA_DIR / "downloads"
    cached_steam = DATA_DIR / "steam.exe"
    cache_items = []
    cache_size = 0
    if downloads_dir.exists():
        cache_size += dir_size(downloads_dir)
        cache_items.append(downloads_dir)
    if cached_steam.exists():
        cache_size += cached_steam.stat().st_size if cached_steam.exists() else 0
        cache_items.append(cached_steam)
    if cache_items:
        found.append(("Cached downloads", cache_items, cache_size))

    # Kernel module
    kver = get_kernel_version()
    ko_path = Path(f"/lib/modules/{kver}/extra/{KMOD_NAME}.ko")
    if ko_path.exists():
        found.append(("Kernel module", ko_path, ko_path.stat().st_size))

    # Runtime state (always present after any run)
    tmp_dir = Path("/tmp/quark")
    uid = os.getuid()
    wine_sockets = list(Path(f"/tmp/.wine-{uid}").glob("server-*/socket")) if Path(f"/tmp/.wine-{uid}").exists() else []
    shm_segments = list(Path("/dev/shm").glob("triskelion-*"))
    runtime_count = (1 if tmp_dir.exists() else 0) + len(wine_sockets) + len(shm_segments)
    if runtime_count:
        found.append(("Runtime state (logs, sockets, shm)", "[various /tmp paths]", 0))

    if not found:
        return

    def fmt_size(n):
        if n >= 1 << 30:
            return f"{n / (1 << 30):.1f} GB"
        if n >= 1 << 20:
            return f"{n / (1 << 20):.1f} MB"
        if n >= 1 << 10:
            return f"{n / (1 << 10):.0f} KB"
        return f"{n} B"

    total = sum(size for _, _, size in found)
    print()
    print("  Existing quark installation detected:")
    for label, path, size in found:
        size_str = fmt_size(size) if size else ""
        if isinstance(path, list) or isinstance(path, str):
            line = f"    - {label}"
        else:
            line = f"    - {label}: {path}"
        if size_str:
            line += f" ({size_str})"
        print(line)
    if total:
        print(f"    Total: {fmt_size(total)}")
    print()

    if not prompt_yn("  Clean everything for a fresh build?"):
        log("INFO", "Keeping existing artifacts — building on top")
        return

    # Kill running processes FIRST — prevents "Text file busy" on binary overwrites
    kill_running_servers()

    # Clean runtime state — logs, sockets, shm
    clean_runtime_state()

    # Clean build artifacts — one pass, no per-item prompts
    for label, path, _size in found:
        if label == "Kernel module":
            uninstall_kernel_module()
        elif label.startswith("Runtime state"):
            pass  # already handled above
        elif isinstance(path, list):
            for p in path:
                if p.is_dir():
                    shutil.rmtree(p)
                elif p.is_file():
                    p.unlink()
        elif isinstance(path, Path) and path.is_symlink():
            path.unlink()
            log("INFO", f"Removed: {path}")
        elif isinstance(path, Path) and path.is_dir():
            shutil.rmtree(path)
            log("INFO", f"Removed: {path}")
        elif isinstance(path, Path) and path.is_file():
            path.unlink()
            log("INFO", f"Removed: {path}")

    print()
    log("INFO", "Clean slate — proceeding with fresh build")


def deploy_eac_runtime():
    """Deploy Proton EasyAntiCheat Runtime DLLs into quark's Wine tree.

    Source: Steam tool 'Proton EasyAntiCheat Runtime' (App ID 1826330).
    Users must install this via Steam Library > Tools.
    """
    if not EAC_RUNTIME_DIR.exists():
        log("WARN", "Proton EasyAntiCheat Runtime not found")
        log("WARN", "  Games using EasyAntiCheat (Elden Ring, etc.) will not work")
        log("WARN", "  Install via: Steam > Library > Tools > 'Proton EasyAntiCheat Runtime'")
        return

    dst_pe64 = STEAM_COMPAT_DIR / "lib" / "wine" / "x86_64-windows"
    dst_so64 = STEAM_COMPAT_DIR / "lib" / "wine" / "x86_64-unix"
    dst_pe32 = STEAM_COMPAT_DIR / "lib" / "wine" / "i386-windows"
    dst_so32 = STEAM_COMPAT_DIR / "lib" / "wine" / "i386-unix"
    for d in [dst_pe64, dst_so64, dst_pe32, dst_so32]:
        d.mkdir(parents=True, exist_ok=True)

    deployed = 0
    # 64-bit
    src64 = EAC_RUNTIME_DIR / "lib64"
    for name in ["easyanticheat", "easyanticheat_x64"]:
        dll = src64 / f"{name}.dll"
        so = src64 / f"{name}.so"
        if dll.exists():
            shutil.copy2(dll, dst_pe64 / f"{name}.dll")
            deployed += 1
        if so.exists():
            shutil.copy2(so, dst_so64 / f"{name}.so")
            deployed += 1

    # 32-bit
    src32 = EAC_RUNTIME_DIR / "lib32"
    for name in ["easyanticheat_x86"]:
        dll = src32 / f"{name}.dll"
        so = src32 / f"{name}.so"
        if dll.exists():
            shutil.copy2(dll, dst_pe32 / f"{name}.dll")
            deployed += 1
        if so.exists():
            shutil.copy2(so, dst_so32 / f"{name}.so")
            deployed += 1

    if deployed > 0:
        log("INFO", f"EasyAntiCheat: deployed {deployed} runtime files from Steam")
    else:
        log("WARN", "EasyAntiCheat: runtime directory exists but no DLLs found")


def main():
    configure_verbose()

    if not check_dependencies():
        return 1

    # Step 1: Verify kernel supports ntsync (hard gate — no fallbacks)
    print()
    if not require_ntsync():
        return 1

    # Offer to clean old builds before proceeding
    detect_old_builds()

    # Step 2: Build and deploy triskelion binary
    ret = build_triskelion()
    if ret != 0:
        return ret

    # Step 3: Deploy system Wine binaries (symlinks, no compilation)
    print()
    if not deploy_system_wine():
        log("ERROR", "Failed to deploy system Wine — install Wine from your package manager")
        return 1

    # Step 3b: Sync system Wine DLLs into game prefixes
    sync_prefix_dlls()

    # Step 4: Download and deploy DXVK/VKD3D-proton from GitHub
    print()
    log("INFO", "Downloading DXVK and VKD3D-proton...")
    dxvk_tar, vkd3d_tar = download_dxvk_vkd3d()
    if dxvk_tar or vkd3d_tar:
        deploy_dxvk_vkd3d(dxvk_tar, vkd3d_tar)
    else:
        log("WARN", "DXVK/VKD3D: download failed — games needing D3D translation may fail")

    # Step 5: Proton bridge DLLs
    print()
    deploy_steam_exe()
    print()
    build_lsteamclient()

    # Step 6: Configuration (automatic — no prompts)
    configure_shader_cache()
    configure_custom_env()

    # Step 7: Game intelligence (discover games, detect engines)
    print()
    generate_game_intelligence()

    # Step 8: EasyAntiCheat runtime (optional — sourced from Steam)
    print()
    deploy_eac_runtime()

    print()
    log("INFO", "Save data protection: enabled (automatic)")
    log("INFO", "  Pre-launch snapshots save data, restores if Steam Cloud sync")
    log("INFO", "  wipes files during first launch with a new compatibility tool.")
    print()

    log("INFO", "Installation complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())

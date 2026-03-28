#!/usr/bin/env python3
"""Protocol conformance test: compare triskelion vs stock wineserver byte-for-byte.

Captures real Wine protocol traffic from stock wineserver using strace,
then replays the same scenario with triskelion and compares reply bytes.

Phase 1: strace stock wineserver while Wine initializes a prefix
Phase 2: Parse strace output → extract (opcode, request, reply) tuples
Phase 3: Run same scenario with triskelion (via iterate.py or direct)
Phase 4: Diff every reply per-opcode

Usage:
    python3 tests/protocol_conformance.py
    python3 tests/protocol_conformance.py --stock /usr/bin/wineserver
    python3 tests/protocol_conformance.py -v
"""

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

# ── Opcode name table ─────────────────────────────────────────────────

OPCODE_NAMES = {
    0: "new_process", 1: "get_new_process_info", 2: "new_thread",
    3: "get_startup_info", 4: "init_process_done", 5: "init_first_thread",
    6: "init_thread", 7: "terminate_process", 8: "terminate_thread",
    9: "get_process_info", 10: "get_process_debug_info",
    14: "get_thread_info", 15: "get_thread_times", 16: "set_thread_info",
    18: "resume_thread", 21: "close_handle", 22: "set_handle_info",
    23: "dup_handle", 27: "open_process", 28: "open_thread",
    29: "select", 30: "create_event", 31: "event_op",
    33: "open_event", 34: "create_keyed_event", 35: "open_keyed_event",
    36: "create_mutex", 37: "release_mutex", 38: "open_mutex",
    40: "create_semaphore", 41: "release_semaphore", 43: "open_semaphore",
    44: "create_file", 46: "alloc_file_handle", 48: "get_handle_fd",
    49: "get_directory_cache_entry",
    63: "create_mapping", 64: "open_mapping", 65: "get_mapping_info",
    66: "get_image_map_address", 67: "map_view", 68: "map_image_view",
    70: "get_image_view_info", 71: "unmap_view",
    86: "create_key", 87: "open_key", 88: "delete_key",
    89: "flush_key", 90: "enum_key", 91: "set_key_value",
    92: "get_key_value", 93: "enum_key_value", 94: "delete_key_value",
    95: "load_registry", 96: "unload_registry", 97: "save_registry",
    107: "add_atom", 108: "delete_atom", 109: "find_atom",
    110: "get_atom_information", 111: "add_user_atom",
    113: "get_msg_queue_handle", 114: "get_msg_queue",
    115: "set_queue_fd", 116: "set_queue_mask",
    117: "get_queue_status", 122: "get_message",
    131: "cancel_sync", 138: "ioctl",
    140: "create_named_pipe",
    142: "create_window", 143: "destroy_window",
    144: "get_desktop_window", 145: "set_window_owner",
    146: "get_window_info", 147: "init_window_info",
    148: "set_window_info", 149: "set_parent",
    154: "get_window_tree", 155: "set_window_pos",
    156: "get_window_rectangles", 160: "get_visible_region",
    161: "get_window_region", 162: "set_window_region",
    166: "set_window_property", 167: "remove_window_property",
    168: "get_window_property",
    170: "create_winstation", 171: "open_winstation",
    173: "set_winstation_monitors",
    174: "get_process_winstation", 175: "set_process_winstation",
    177: "create_desktop", 178: "open_desktop",
    182: "get_thread_desktop", 183: "set_thread_desktop",
    184: "set_user_object_info",
    188: "get_thread_input",
    192: "set_foreground_window", 193: "set_focus_window",
    194: "set_active_window", 195: "set_capture_window",
    203: "create_class", 204: "destroy_class",
    220: "open_token", 228: "get_token_sid",
    240: "open_directory", 249: "allocate_locally_unique_id",
    259: "make_process_system",
    263: "create_completion", 266: "remove_completion",
    274: "set_fd_eof_info",
    277: "alloc_user_handle", 278: "free_user_handle",
    279: "set_cursor",
    283: "create_job", 287: "set_job_limits",
    288: "set_job_completion_port",
    294: "get_next_thread",
    296: "get_inproc_sync_fd", 297: "get_inproc_alert_fd",
}

FIXED_REQUEST_SIZE = 64

# ── Strace parser ─────────────────────────────────────────────────────

def parse_strace_hex(hex_str):
    """Parse strace hex bytes like \\x41\\x42 into bytes."""
    result = bytearray()
    i = 0
    while i < len(hex_str):
        if i + 1 < len(hex_str) and hex_str[i] == '\\':
            c = hex_str[i + 1]
            if c == 'x' and i + 3 < len(hex_str):
                result.append(int(hex_str[i+2:i+4], 16))
                i += 4
            elif c == '0':
                result.append(0)
                i += 2
            elif c == 'n':
                result.append(10)
                i += 2
            elif c == 't':
                result.append(9)
                i += 2
            elif c == 'r':
                result.append(13)
                i += 2
            elif c == '\\':
                result.append(ord('\\'))
                i += 2
            elif c == '"':
                result.append(ord('"'))
                i += 2
            elif c.isdigit():
                # octal escape \NNN
                end = i + 2
                while end < len(hex_str) and end < i + 4 and hex_str[end].isdigit():
                    end += 1
                result.append(int(hex_str[i+1:end], 8))
                i = end
            else:
                i += 2
        else:
            result.append(ord(hex_str[i]))
            i += 1
    return bytes(result)


# Regex patterns for strace output.
# read(fd, "hex_data", requested_size) = actual_size
# read(fd, "hex_data"..., requested_size) = actual_size  (truncated)
RE_READ_EXACT = re.compile(
    r'read\((\d+),\s*"((?:[^"\\]|\\.)*)",\s*(\d+)\)\s*=\s*(\d+)'
)
RE_READ_TRUNC = re.compile(
    r'read\((\d+),\s*"((?:[^"\\]|\\.)*)"\.\.\.,\s*(\d+)\)\s*=\s*(\d+)'
)
# write(fd, "hex_data", size) = actual
RE_WRITE = re.compile(
    r'write\((\d+),\s*"((?:[^"\\]|\\.)*)"(?:\.\.\.)?,\s*(\d+)\)\s*=\s*(\d+)'
)
# writev(fd, [{iov_base="data", iov_len=N}, {iov_base="data2", iov_len=M}], count) = total
# Capture all iov entries
RE_WRITEV = re.compile(
    r'writev\((\d+),\s*\[(.*)\],\s*\d+\)\s*=\s*(\d+)'
)
RE_IOV_ENTRY = re.compile(
    r'\{iov_base="((?:[^"\\]|\\.)*)"(?:\.\.\.)?,\s*iov_len=(\d+)\}'
)


def parse_read_line(line):
    """Try to parse a read() strace line. Returns (fd, data_bytes, requested, actual) or None."""
    m = RE_READ_EXACT.match(line) or RE_READ_TRUNC.match(line)
    if not m:
        return None
    fd = int(m.group(1))
    hex_data = m.group(2)
    requested = int(m.group(3))
    actual = int(m.group(4))
    data = parse_strace_hex(hex_data)
    return fd, data, requested, actual


def parse_write_line(line):
    """Try to parse a write() strace line. Returns (fd, data_bytes) or None."""
    m = RE_WRITE.match(line)
    if m:
        fd = int(m.group(1))
        data = parse_strace_hex(m.group(2))
        return fd, data
    return None


def parse_writev_line(line):
    """Try to parse a writev() strace line. Returns (fd, concatenated_data) or None."""
    m = RE_WRITEV.match(line)
    if not m:
        return None
    fd = int(m.group(1))
    iov_str = m.group(2)
    # Extract all iov entries and concatenate their data
    result = bytearray()
    for iov_m in RE_IOV_ENTRY.finditer(iov_str):
        iov_data = parse_strace_hex(iov_m.group(1))
        result.extend(iov_data)
    if not result:
        return None
    return fd, bytes(result)


def extract_protocol_traffic(strace_file):
    """Parse strace output to extract (opcode, request, reply) tuples.

    Protocol identification:
    - Request pipe: server does read(fd, ..., 64) = 64 (exactly 64 bytes)
    - First 4 bytes = opcode (must be in OPCODE_NAMES)
    - Bytes 4-7 = request_size (variable data), bytes 8-11 = reply_size
    - Reply follows on a different fd as write() or writev()

    Returns: list of dicts with 'opcode', 'name', 'request', 'reply'
    """
    records = []

    # Pass 1: identify protocol request fds using two-tier detection
    # Tier 1 (strict): read(fd, buf, 64) = 64 — stock wineserver style
    # Tier 2 (flexible): read(fd, buf, N) where actual >= 64 — triskelion style (4096-byte buffer)
    fd_strict = {}   # fd -> count of strict-matching reads (requested==64, actual==64)
    fd_flex = {}     # fd -> count of flexible-matching reads (actual >= 64)
    fd_total = {}    # fd -> total reads

    def _valid_header(data):
        opcode = struct.unpack_from('<i', data, 0)[0]
        req_size = struct.unpack_from('<I', data, 4)[0]
        reply_size = struct.unpack_from('<I', data, 8)[0]
        if opcode not in OPCODE_NAMES:
            return False
        if req_size > 256 * 1024 or reply_size > 256 * 1024:
            return False
        return True

    with open(strace_file, 'r', errors='replace') as f:
        for line in f:
            parsed = parse_read_line(line.strip())
            if not parsed:
                continue
            fd, data, requested, actual = parsed
            fd_total[fd] = fd_total.get(fd, 0) + 1

            if actual < 64 or len(data) < 12:
                continue
            if not _valid_header(data):
                continue

            fd_flex[fd] = fd_flex.get(fd, 0) + 1
            if requested == 64 and actual == 64:
                fd_strict[fd] = fd_strict.get(fd, 0) + 1

    # Tier 1: strict detection (stock wineserver)
    request_fds = {fd for fd, count in fd_strict.items() if count >= 5}

    # Tier 2: only if strict found nothing, use flexible (triskelion)
    if not request_fds:
        for fd, hits in fd_flex.items():
            total = fd_total.get(fd, 0)
            if hits >= 5 and (hits / max(total, 1)) > 0.3:
                request_fds.add(fd)

    if not request_fds:
        print(f"  WARNING: No protocol fds identified in {strace_file}")
        for fd, count in sorted(fd_flex.items(), key=lambda x: -x[1])[:5]:
            print(f"    fd={fd}: {count} protocol-like reads / {fd_total.get(fd, 0)} total")
        return records

    # Pass 2: extract request-reply pairs
    pending = None

    with open(strace_file, 'r', errors='replace') as f:
        for line in f:
            line = line.strip()

            # Try read
            parsed = parse_read_line(line)
            if parsed:
                fd, data, requested, actual = parsed
                if fd in request_fds and actual >= 64 and len(data) >= 12:
                    opcode = struct.unpack_from('<i', data, 0)[0]
                    if opcode in OPCODE_NAMES:
                        pending = {
                            'opcode': opcode,
                            'name': OPCODE_NAMES[opcode],
                            'request': data[:64],
                            'reply': None,
                            'req_fd': fd,
                        }
                continue

            if not pending:
                continue

            # Try write (reply)
            parsed = parse_write_line(line)
            if parsed:
                fd, data = parsed
                if fd not in request_fds:
                    pending['reply'] = data
                    records.append(pending)
                    pending = None
                continue

            # Try writev (reply with variable data)
            parsed = parse_writev_line(line)
            if parsed:
                fd, data = parsed
                if fd not in request_fds:
                    pending['reply'] = data
                    records.append(pending)
                    pending = None
                continue

    return records


# ── Capture stock wineserver traffic ──────────────────────────────────

def _cleanup_wine_dirs(prefix_path, tmpdir):
    """Clean up Wine socket dir and tmpdir."""
    uid = os.getuid()
    try:
        st = os.stat(prefix_path)
        sd = f"/tmp/.wine-{uid}/server-{st.st_dev:x}-{st.st_ino:x}"
        shutil.rmtree(sd, ignore_errors=True)
    except OSError:
        pass
    shutil.rmtree(tmpdir, ignore_errors=True)


def capture_stock_traffic(stock_binary):
    """Run stock wineserver under strace with a wine session.
    Returns list of protocol records."""

    tmpdir = tempfile.mkdtemp(prefix="conform_stock_")
    prefix = os.path.join(tmpdir, "prefix")
    os.makedirs(prefix)
    trace_dir = os.path.join(tmpdir, "traces")
    os.makedirs(trace_dir)
    trace_prefix = os.path.join(trace_dir, "ws")

    env = os.environ.copy()
    env["WINEPREFIX"] = prefix
    env["WINEDLLOVERRIDES"] = "mscoree=d;mshtml=d"
    env["WINEDEBUG"] = "-all"
    env["WINESERVER"] = stock_binary

    try:
        # Start stock wineserver under strace
        strace_proc = subprocess.Popen(
            ["strace", "-ff", "-e", "trace=read,write,writev",
             "-xx", "-s", "4096", "-o", trace_prefix,
             stock_binary, "-f"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for socket
        from test_eventloop_ntsync import compute_socket_path
        sock_path = compute_socket_path(prefix)
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            time.sleep(0.1)

        # Run a wine session to generate traffic (prefix init can take a while under strace)
        wine_proc = subprocess.Popen(
            ["wine", "cmd", "/c", "echo READY"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wine_proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            wine_proc.kill()
            wine_proc.wait()

        time.sleep(1)

        # Kill the wineserver
        subprocess.run(
            [stock_binary, "-k"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        time.sleep(1)
        strace_proc.terminate()
        try:
            strace_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            strace_proc.kill()
            strace_proc.wait()

        # Parse all strace output files
        all_records = []
        for fname in sorted(os.listdir(trace_dir)):
            fpath = os.path.join(trace_dir, fname)
            if os.path.isfile(fpath):
                records = extract_protocol_traffic(fpath)
                all_records.extend(records)

        return all_records

    finally:
        _cleanup_wine_dirs(prefix, tmpdir)


# ── Capture triskelion traffic ────────────────────────────────────────

def capture_triskelion_traffic(triskelion_binary):
    """Run triskelion under strace with a wine session.
    Returns list of protocol records."""

    tmpdir = tempfile.mkdtemp(prefix="conform_tris_")
    prefix = os.path.join(tmpdir, "prefix")
    os.makedirs(prefix)
    trace_dir = os.path.join(tmpdir, "traces")
    os.makedirs(trace_dir)
    trace_prefix = os.path.join(trace_dir, "ts")

    env = os.environ.copy()
    env["WINEPREFIX"] = prefix
    env["WINEDLLOVERRIDES"] = "mscoree=d;mshtml=d"
    env["WINEDEBUG"] = "-all"
    env["WINESERVER"] = triskelion_binary
    env["RUST_LOG"] = "warn"

    try:
        # Start triskelion under strace
        strace_proc = subprocess.Popen(
            ["strace", "-ff", "-e", "trace=read,write,writev",
             "-xx", "-s", "4096", "-o", trace_prefix,
             triskelion_binary, "-f"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for socket
        from test_eventloop_ntsync import compute_socket_path
        sock_path = compute_socket_path(prefix)
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            time.sleep(0.1)

        # Run the same wine session
        wine_proc = subprocess.Popen(
            ["wine", "cmd", "/c", "echo READY"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wine_proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            wine_proc.kill()
            wine_proc.wait()

        time.sleep(1)
        strace_proc.terminate()
        try:
            strace_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            strace_proc.kill()
            strace_proc.wait()

        # Parse traces
        all_records = []
        for fname in sorted(os.listdir(trace_dir)):
            fpath = os.path.join(trace_dir, fname)
            if os.path.isfile(fpath):
                records = extract_protocol_traffic(fpath)
                all_records.extend(records)

        return all_records

    finally:
        _cleanup_wine_dirs(prefix, tmpdir)


# ── Comparison ────────────────────────────────────────────────────────

def compare_by_opcode(stock_records, tris_records, verbose=False):
    """Group records by opcode and compare reply structures.
    Returns list of divergence descriptions."""

    # Group by opcode
    stock_by_op = {}
    for r in stock_records:
        op = r['opcode']
        stock_by_op.setdefault(op, []).append(r)

    tris_by_op = {}
    for r in tris_records:
        op = r['opcode']
        tris_by_op.setdefault(op, []).append(r)

    divergences = []
    all_ops = sorted(set(list(stock_by_op.keys()) + list(tris_by_op.keys())))

    for op in all_ops:
        name = OPCODE_NAMES.get(op, f"op_{op}")
        stock_list = stock_by_op.get(op, [])
        tris_list = tris_by_op.get(op, [])

        if not stock_list:
            if verbose:
                print(f"  {name}: triskelion only ({len(tris_list)} calls)")
            continue
        if not tris_list:
            if verbose:
                print(f"  {name}: stock only ({len(stock_list)} calls)")
            continue

        # Compare first occurrence (most informative)
        sr = stock_list[0]
        tr = tris_list[0]

        if sr['reply'] is None or tr['reply'] is None:
            continue

        s_reply = sr['reply']
        t_reply = tr['reply']

        # Compare error codes (first 4 bytes)
        s_err = struct.unpack_from('<I', s_reply, 0)[0] if len(s_reply) >= 4 else 0xDEAD
        t_err = struct.unpack_from('<I', t_reply, 0)[0] if len(t_reply) >= 4 else 0xDEAD

        if s_err != t_err:
            divergences.append(
                f"{name}: ERROR MISMATCH stock=0x{s_err:08x} tris=0x{t_err:08x} "
                f"(stock_calls={len(stock_list)} tris_calls={len(tris_list)})"
            )
            continue

        # Compare reply lengths
        if len(s_reply) != len(t_reply):
            divergences.append(
                f"{name}: REPLY LEN stock={len(s_reply)} tris={len(t_reply)}"
            )
            continue

        # Compare reply bytes (skip first 8 = header, handle-valued fields vary)
        diffs = []
        for off in range(8, min(len(s_reply), len(t_reply))):
            if s_reply[off] != t_reply[off]:
                diffs.append(off)

        if diffs and len(diffs) > 0:
            # Check if ALL different bytes could be handle values
            # (handle values differ between servers, so we expect some diffs)
            desc = format_reply_diff(name, s_reply, t_reply, diffs)
            if verbose or not all_handle_like(diffs, s_reply, t_reply):
                divergences.append(f"{name}: REPLY DIFFERS ({len(diffs)} bytes)\n{desc}")
        elif verbose:
            print(f"  {name}: OK (stock={len(stock_list)} tris={len(tris_list)} calls)")

    return divergences


def all_handle_like(diffs, s_reply, t_reply):
    """Check if all differing offsets look like handle values (aligned u32s)."""
    # Group into contiguous ranges
    groups = []
    cur = [diffs[0]]
    for off in diffs[1:]:
        if off == cur[-1] + 1:
            cur.append(off)
        else:
            groups.append(cur)
            cur = [off]
    groups.append(cur)

    for g in groups:
        # Handle values are 4-byte aligned u32s
        if len(g) != 4 or g[0] % 4 != 0:
            return False
    return True


def format_reply_diff(name, s_reply, t_reply, diffs):
    """Format byte diffs for display."""
    lines = []
    groups = []
    cur = [diffs[0]]
    for off in diffs[1:]:
        if off == cur[-1] + 1:
            cur.append(off)
        else:
            groups.append(cur)
            cur = [off]
    groups.append(cur)

    for g in groups:
        a, b = g[0], g[-1] + 1
        s_hex = s_reply[a:b].hex()
        t_hex = t_reply[a:b].hex()
        interp = ""
        if b - a == 4:
            sv = struct.unpack_from('<I', s_reply, a)[0]
            tv = struct.unpack_from('<I', t_reply, a)[0]
            interp = f" (stock=0x{sv:08x}, tris=0x{tv:08x})"
        elif b - a == 8:
            sv = struct.unpack_from('<Q', s_reply, a)[0]
            tv = struct.unpack_from('<Q', t_reply, a)[0]
            interp = f" (stock=0x{sv:016x}, tris=0x{tv:016x})"
        lines.append(f"    [{a}:{b}] stock={s_hex} tris={t_hex}{interp}")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Protocol conformance: triskelion vs stock wineserver"
    )
    parser.add_argument(
        "--stock", default="/usr/bin/wineserver",
        help="Path to stock wineserver (default: /usr/bin/wineserver)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from test_eventloop_ntsync import find_triskelion_binary

    triskelion = find_triskelion_binary()
    if not triskelion:
        print("ERROR: triskelion binary not found. Run: cargo build --release")
        sys.exit(1)

    print(f"Stock:      {args.stock}")
    print(f"Triskelion: {triskelion}")
    print()

    # ── Phase 1: capture stock traffic ────────────────────────────────
    print("=" * 70)
    print("Phase 1: Capturing STOCK wineserver traffic (strace + wine cmd)")
    print("=" * 70)
    stock_records = capture_stock_traffic(args.stock)
    print(f"  {len(stock_records)} protocol operations captured")

    # Show opcode distribution
    op_counts = {}
    for r in stock_records:
        op_counts[r['name']] = op_counts.get(r['name'], 0) + 1
    print(f"  {len(op_counts)} unique opcodes")
    if args.verbose:
        for name, count in sorted(op_counts.items(), key=lambda x: -x[1]):
            print(f"    {count:>5}x  {name}")

    # ── Phase 2: capture triskelion traffic ───────────────────────────
    print()
    print("=" * 70)
    print("Phase 2: Capturing TRISKELION traffic (strace + wine cmd)")
    print("=" * 70)
    tris_records = capture_triskelion_traffic(triskelion)
    print(f"  {len(tris_records)} protocol operations captured")

    op_counts_t = {}
    for r in tris_records:
        op_counts_t[r['name']] = op_counts_t.get(r['name'], 0) + 1
    print(f"  {len(op_counts_t)} unique opcodes")

    # ── Phase 3: compare ──────────────────────────────────────────────
    print()
    print("=" * 70)
    print("Phase 3: COMPARING per-opcode reply structures")
    print("=" * 70)

    divergences = compare_by_opcode(stock_records, tris_records, verbose=args.verbose)

    if not divergences:
        print("\n  ALL REPLIES MATCH per-opcode (excluding handle-valued fields)")
    else:
        print(f"\n  {len(divergences)} DIVERGENCE(S):\n")
        for d in divergences:
            print(f"  {d}")

    # Summary
    print()
    print("=" * 70)
    common_ops = set(op_counts.keys()) & set(op_counts_t.keys())
    print(f"SUMMARY: {len(common_ops)} common opcodes tested, "
          f"{len(divergences)} divergences")
    print("=" * 70)

    return len(divergences)


if __name__ == "__main__":
    sys.exit(0 if main() == 0 else 1)

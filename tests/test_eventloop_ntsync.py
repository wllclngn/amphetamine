#!/usr/bin/env python3
"""EventLoop protocol-level tests for triskelion's ntsync state management.

Tests the triskelion wineserver replacement by speaking the Wine protocol
directly — no Wine/Proton needed. Starts triskelion as a subprocess,
connects as a fake Wine client, and exercises ntsync sync primitives.

Requirements:
    - triskelion binary built: cargo build --release (in rust/)
    - /dev/ntsync available (Linux 6.14+)
    - No Wine installation needed

Usage:
    python3 tests/test_eventloop_ntsync.py
    python3 tests/test_eventloop_ntsync.py -v
    python3 tests/test_eventloop_ntsync.py TestCreateEvent
"""

import array
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

# ---- Protocol constants ----

FIXED_REQUEST_SIZE = 64

# Opcode numbers (from dispatch table — must match compiled protocol)
OP_NEW_THREAD = 2
OP_GET_STARTUP_INFO = 3
OP_INIT_FIRST_THREAD = 5
OP_INIT_THREAD = 6
OP_CLOSE_HANDLE = 21
OP_DUP_HANDLE = 23
OP_SELECT = 29
OP_CREATE_EVENT = 30
OP_EVENT_OP = 31
OP_OPEN_EVENT = 32
OP_CREATE_MUTEX = 36
OP_RELEASE_MUTEX = 37
OP_CREATE_SEMAPHORE = 40
OP_RELEASE_SEMAPHORE = 41
OP_GET_INPROC_SYNC_FD = 296

# Event operations
EVENT_PULSE = 0
EVENT_SET = 1
EVENT_RESET = 2

# Status codes
STATUS_SUCCESS = 0
STATUS_TIMEOUT = 0x00000102
STATUS_PENDING = 0x00000103
STATUS_OBJECT_NAME_EXISTS = 0x40000000
STATUS_INVALID_HANDLE = 0xC0000008
STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034

# ntsync ioctl codes (for direct ntsync fd verification)
NTSYNC_IOC_EVENT_SET   = 0x80044E88
NTSYNC_IOC_EVENT_RESET = 0x80044E89
NTSYNC_IOC_WAIT_ANY    = 0xC0284E82

# Select opcodes (within select_op vararg)
SELECT_WAIT = 1
SELECT_WAIT_ALL = 2


# ---- Low-level SCM_RIGHTS helpers ----

def recv_fd(sock):
    """Receive one fd via SCM_RIGHTS. Returns (fd, data_u32)."""
    fds = array.array('i')
    msg, ancdata, flags, addr = sock.recvmsg(256, socket.CMSG_SPACE(64))
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            n_fds = len(cmsg_data) // 4
            fds.frombytes(cmsg_data[:n_fds * 4])
    data_val = struct.unpack('<I', msg[:4])[0] if len(msg) >= 4 else 0
    return (fds[0] if fds else -1), data_val


def send_fd_to_server(sock, fd, fd_number, thread_id=0):
    """Send fd via SCM_RIGHTS in wine_server_send_fd format.

    Data payload: { thread_id: u32, fd_number: i32 } (8 bytes)
    Ancillary: SCM_RIGHTS with the actual fd
    """
    data = struct.pack('<Ii', thread_id, fd_number)
    fds = array.array('i', [fd])
    sock.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])


def compute_socket_path(wineprefix):
    """Compute the socket path triskelion will use for a given WINEPREFIX."""
    st = os.stat(wineprefix)
    uid = os.getuid()
    base = f"/tmp/.wine-{uid}"
    server_dir = os.path.join(base, f"server-{st.st_dev:x}-{st.st_ino:x}")
    return os.path.join(server_dir, "socket")


def find_triskelion_binary():
    """Find the triskelion binary (release or debug)."""
    project_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
    # Workspace target is at project root, not rust/
    for profile in ('release', 'debug'):
        path = os.path.join(project_root, 'target', profile, 'triskelion')
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return os.path.abspath(path)
    return None


# ---- Wine protocol client ----

class WineClient:
    """A fake Wine client that speaks the Wine server protocol to triskelion.

    Performs the three-channel handshake (msg_fd / request_fd / reply_fd)
    and provides methods for each sync-related request type.
    """

    def __init__(self):
        self.msg_sock = None       # Unix socket (for SCM_RIGHTS)
        self.request_fd = None     # Pipe write end (client writes requests)
        self.reply_r = None        # Pipe read end (client reads replies)
        self.reply_w = None        # Pipe write end (server writes replies)
        self.wait_r = None         # Socket read end (client reads wake_up)
        self.wait_w = None         # Socket write end (server writes wake_up)
        self.pid = 0
        self.tid = 0
        self.protocol_version = 0

    def connect(self, socket_path, timeout=5.0):
        """Connect to triskelion and complete the three-channel handshake."""
        # 1. Connect to Unix socket
        self.msg_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.msg_sock.settimeout(timeout)
        self.msg_sock.connect(socket_path)

        # 2. Receive request pipe write end + protocol version from server
        self.request_fd, self.protocol_version = recv_fd(self.msg_sock)
        assert self.request_fd >= 0, "Failed to receive request pipe fd"
        assert self.protocol_version > 0, f"Bad protocol version: {self.protocol_version}"

        # 3. Create reply pipe (server writes on reply_w, client reads from reply_r)
        self.reply_r, self.reply_w = os.pipe()

        # 4. Create wait socketpair (server writes wake_up on wait_w, client reads from wait_r)
        wait_a, wait_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.wait_r = wait_a
        self.wait_w = wait_b
        self.wait_r.settimeout(timeout)

        # 5. Send reply_fd and wait_fd to server via SCM_RIGHTS on msg_fd
        # wine_server_send_fd format: 8 bytes = { thread_id: u32, fd_number: i32 }
        send_fd_to_server(self.msg_sock, self.reply_w, self.reply_w)
        send_fd_to_server(self.msg_sock, self.wait_w.fileno(), self.wait_w.fileno())

        # Let server process msg_fd events before we send requests
        time.sleep(0.05)

        # 6. Send InitFirstThread request
        unix_pid = os.getpid()
        unix_tid = unix_pid
        fields = struct.pack('<iiiii',
            unix_pid,               # unix_pid
            unix_tid,               # unix_tid
            0,                      # debug_level
            self.reply_w,           # reply_fd (fd number matching SCM_RIGHTS)
            self.wait_w.fileno(),   # wait_fd (fd number matching SCM_RIGHTS)
        )
        error, reply = self.send_request(OP_INIT_FIRST_THREAD, fields, max_reply_vararg=256)
        assert error == 0, f"InitFirstThread failed: error={error:#x}"

        # Parse InitFirstThreadReply: { header(8), pid(4), tid(4), ... }
        self.pid = struct.unpack_from('<I', reply, 8)[0]
        self.tid = struct.unpack_from('<I', reply, 12)[0]
        assert self.pid > 0, "Got zero PID"
        assert self.tid > 0, "Got zero TID"

        # Drain the ntsync device fd that init_first_thread sends via msg_fd.
        # Without this, the first get_inproc_sync_fd call would read the stale
        # device fd instead of the actual event fd.
        inproc_device = struct.unpack_from('<I', reply, 28)[0]
        if inproc_device != 0:
            try:
                self.msg_sock.settimeout(0.1)
                dev_fd, _ = recv_fd(self.msg_sock)
                if dev_fd >= 0:
                    os.close(dev_fd)  # We don't need the device fd in tests
                self.msg_sock.settimeout(timeout)
            except (socket.timeout, OSError):
                self.msg_sock.settimeout(timeout)

        return self.pid, self.tid

    def send_request(self, opcode, fields=b'', vararg=b'', max_reply_vararg=0):
        """Send a protocol request and read the fixed reply.

        Returns: (error, reply_bytes)
        """
        header = struct.pack('<iII', opcode, len(vararg), max_reply_vararg)
        body = header + fields
        body = body.ljust(FIXED_REQUEST_SIZE, b'\x00')
        body += vararg
        os.write(self.request_fd, body)

        # Read reply (64 bytes fixed + vararg)
        reply = os.read(self.reply_r, 64 + max_reply_vararg + 256)
        if len(reply) < 8:
            return 0xDEAD, reply
        error = struct.unpack_from('<I', reply, 0)[0]
        return error, reply

    # ---- Sync primitive operations ----

    def create_event(self, manual_reset=False, initial_state=False, name=None):
        """Create an ntsync event. Returns (error, handle)."""
        fields = struct.pack('<Iii',
            0x001F0003,                        # access
            1 if manual_reset else 0,          # manual_reset
            1 if initial_state else 0,         # initial_state
        )
        vararg = self._make_objattr(name)
        error, reply = self.send_request(OP_CREATE_EVENT, fields, vararg)
        handle = struct.unpack_from('<I', reply, 8)[0] if len(reply) >= 12 else 0
        return error, handle

    def event_op(self, handle, op):
        """Set/Reset/Pulse an event. Returns (error, prev_state)."""
        fields = struct.pack('<Ii', handle, op)
        error, reply = self.send_request(OP_EVENT_OP, fields)
        state = struct.unpack_from('<i', reply, 8)[0] if len(reply) >= 12 else 0
        return error, state

    def close_handle(self, handle):
        """Close a handle. Returns error code."""
        fields = struct.pack('<I', handle)
        error, _ = self.send_request(OP_CLOSE_HANDLE, fields)
        return error

    def create_mutex(self, owned=False, name=None):
        """Create a mutex. Returns (error, handle)."""
        fields = struct.pack('<Ii', 0x001F0003, 1 if owned else 0)
        vararg = self._make_objattr(name)
        error, reply = self.send_request(OP_CREATE_MUTEX, fields, vararg)
        handle = struct.unpack_from('<I', reply, 8)[0] if len(reply) >= 12 else 0
        return error, handle

    def release_mutex(self, handle):
        """Release a mutex. Returns (error, prev_count)."""
        fields = struct.pack('<I', handle)
        error, reply = self.send_request(OP_RELEASE_MUTEX, fields)
        prev = struct.unpack_from('<I', reply, 8)[0] if len(reply) >= 12 else 0
        return error, prev

    def create_semaphore(self, initial=0, max_count=1, name=None):
        """Create a semaphore. Returns (error, handle)."""
        fields = struct.pack('<III', 0x001F0003, initial, max_count)
        vararg = self._make_objattr(name)
        error, reply = self.send_request(OP_CREATE_SEMAPHORE, fields, vararg)
        handle = struct.unpack_from('<I', reply, 8)[0] if len(reply) >= 12 else 0
        return error, handle

    def release_semaphore(self, handle, count=1):
        """Release a semaphore. Returns (error, prev_count)."""
        fields = struct.pack('<II', handle, count)
        error, reply = self.send_request(OP_RELEASE_SEMAPHORE, fields)
        prev = struct.unpack_from('<I', reply, 8)[0] if len(reply) >= 12 else 0
        return error, prev

    def dup_handle(self, src_handle, options=2):
        """Duplicate a handle. options=2 means DUPLICATE_SAME_ACCESS.
        Returns (error, new_handle)."""
        # DupHandleRequest fields after header:
        #   src_process: u32, src_handle: u32, dst_process: u32,
        #   access: u32, attributes: u32, options: u32
        fields = struct.pack('<IIIIII', 0, src_handle, 0, 0, 0, options)
        error, reply = self.send_request(OP_DUP_HANDLE, fields)
        handle = struct.unpack_from('<I', reply, 8)[0] if len(reply) >= 12 else 0
        return error, handle

    def select_poll(self, handles, timeout=0):
        """Poll (non-blocking) for signaled handles via Select.

        Select always returns STATUS_PENDING on reply_fd.
        Actual result comes as wake_up_reply on wait_fd.

        Returns: signaled value (STATUS_TIMEOUT or index of signaled object)
        """
        # VARARG layout: [apc_result(40 bytes)] [select_op]
        # select_op: [opcode(u32)=SELECT_WAIT] [handles(u32 each)]
        apc_result = b'\x00' * 40
        select_op = struct.pack('<I', SELECT_WAIT)
        for h in handles:
            select_op += struct.pack('<I', h)
        vararg = apc_result + select_op

        # SelectRequest fields after header:
        #   flags(i32), cookie(u64), timeout(i64), size(u32), prev_apc(u32)
        cookie = 0x1234567800000001
        fields = struct.pack('<iQqII',
            0,              # flags
            cookie,         # cookie
            timeout,        # timeout (0 = poll)
            len(select_op), # size
            0,              # prev_apc
        )

        error, reply = self.send_request(OP_SELECT, fields, vararg)
        # error should be STATUS_PENDING (0x103) — meaning result is on wait_fd
        assert error == STATUS_PENDING, f"Select returned {error:#x}, expected STATUS_PENDING"

        # Read wake_up_reply from wait_fd: { cookie: u64, signaled: i32, _pad: i32 }
        try:
            wake_data = self.wait_r.recv(16)
            if len(wake_data) >= 12:
                wake_cookie, signaled = struct.unpack_from('<Qi', wake_data, 0)
                return signaled
            return -1
        except socket.timeout:
            return -1

    def get_inproc_sync_fd(self, handle):
        """Get the ntsync fd for a handle via GetInprocSyncFd.

        Returns (error, sync_type, received_fd_or_None).
        The fd is received via SCM_RIGHTS on msg_fd.
        """
        fields = struct.pack('<I', handle)
        # Before sending the request, we need to read the fd from msg_fd after the reply
        error, reply = self.send_request(OP_GET_INPROC_SYNC_FD, fields)

        if error != 0:
            return error, 0, None

        sync_type = struct.unpack_from('<i', reply, 8)[0] if len(reply) >= 12 else 0

        # The server sent an fd via SCM_RIGHTS on msg_fd — receive it
        received_fd, handle_val = recv_fd(self.msg_sock)
        return error, sync_type, received_fd

    def _make_objattr(self, name):
        """Build object_attributes VARARG (with optional UTF-16LE name)."""
        if name is not None:
            name_utf16 = name.encode('utf-16-le')
            # object_attributes: rootdir=0, attributes=0x40 (OBJ_OPENIF), sd_len=0, name_len
            return struct.pack('<IIII', 0, 0x40, 0, len(name_utf16)) + name_utf16
        else:
            return struct.pack('<IIII', 0, 0, 0, 0)

    def close(self):
        """Clean up all fds and sockets."""
        for attr in ('msg_sock', 'wait_r', 'wait_w'):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        for attr in ('request_fd', 'reply_r', 'reply_w'):
            fd = getattr(self, attr, None)
            if fd is not None and fd >= 0:
                try:
                    os.close(fd)
                except Exception:
                    pass


# ---- Test infrastructure ----

class TriskelionTestBase(unittest.TestCase):
    """Base class: starts a triskelion daemon with a temporary WINEPREFIX.

    A "keepalive" client stays connected for the class lifetime to prevent
    the daemon from auto-exiting when test clients disconnect (triskelion
    shuts down when all user processes are gone — correct wineserver behavior).
    """

    _daemon_pid = None
    _socket_path = None
    _wineprefix = None
    _tmpdir = None
    _keepalive = None

    @classmethod
    def setUpClass(cls):
        binary = find_triskelion_binary()
        if not binary:
            raise unittest.SkipTest(
                "triskelion binary not found — run 'cargo build --release' in rust/")

        if not os.path.exists('/dev/ntsync'):
            raise unittest.SkipTest("/dev/ntsync not available")

        # Create temporary WINEPREFIX
        cls._tmpdir = tempfile.mkdtemp(prefix='triskelion_test_')
        cls._wineprefix = cls._tmpdir
        cls._socket_path = compute_socket_path(cls._wineprefix)

        # Start triskelion server (it daemonizes — parent exits immediately)
        env = os.environ.copy()
        env['WINEPREFIX'] = cls._wineprefix
        proc = subprocess.Popen(
            [binary, 'server'],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=5)

        # Wait for socket to appear
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if os.path.exists(cls._socket_path):
                break
            time.sleep(0.02)
        else:
            raise RuntimeError(f"Triskelion socket never appeared: {cls._socket_path}")

        # Find daemon PID by scanning /proc for our WINEPREFIX
        cls._daemon_pid = cls._find_daemon_pid()

        # Connect a keepalive client so the daemon doesn't exit when test
        # clients disconnect (triskelion shuts down when 0 user processes remain)
        cls._keepalive = WineClient()
        cls._keepalive.connect(cls._socket_path)

    @classmethod
    def _find_daemon_pid(cls):
        """Find the triskelion daemon PID that has our WINEPREFIX."""
        try:
            result = subprocess.run(
                ['pgrep', '-x', 'triskelion'],
                capture_output=True, text=True
            )
            pids = [int(p) for p in result.stdout.strip().split('\n') if p.strip()]
            for pid in pids:
                try:
                    with open(f'/proc/{pid}/environ', 'rb') as f:
                        env_data = f.read()
                    if cls._wineprefix.encode() in env_data:
                        return pid
                except (PermissionError, FileNotFoundError):
                    continue
        except Exception:
            pass
        return None

    @classmethod
    def tearDownClass(cls):
        # Close keepalive first
        if cls._keepalive:
            cls._keepalive.close()
            cls._keepalive = None

        # Kill daemon
        if cls._daemon_pid:
            try:
                os.kill(cls._daemon_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(0.1)
            try:
                os.kill(cls._daemon_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        # Clean up socket dir and temp dir
        if cls._socket_path:
            socket_dir = os.path.dirname(cls._socket_path)
            if os.path.isdir(socket_dir):
                shutil.rmtree(socket_dir, ignore_errors=True)
        if cls._tmpdir and os.path.isdir(cls._tmpdir):
            shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def make_client(self):
        """Create and connect a new WineClient. Caller must close it."""
        client = WineClient()
        client.connect(self._socket_path)
        self.addCleanup(client.close)
        return client


# ---- Tests ----

class TestHandshake(TriskelionTestBase):
    """Verify the three-channel handshake works correctly."""

    def test_connect_and_init(self):
        """Client can connect, complete handshake, and get valid PID/TID."""
        c = self.make_client()
        self.assertGreater(c.pid, 0)
        self.assertGreater(c.tid, 0)
        self.assertGreater(c.protocol_version, 0)

    def test_multiple_clients(self):
        """Multiple clients can connect independently."""
        c1 = self.make_client()
        c2 = self.make_client()
        # Each gets a unique PID
        self.assertNotEqual(c1.pid, c2.pid)
        self.assertGreater(c1.tid, 0)
        self.assertGreater(c2.tid, 0)


class TestCreateEvent(TriskelionTestBase):
    """Test CreateEvent → handle allocation + ntsync object creation."""

    def test_create_unnamed_event(self):
        """Unnamed event returns a valid handle."""
        c = self.make_client()
        err, handle = c.create_event(manual_reset=True, initial_state=False)
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertGreater(handle, 0, "Expected non-zero handle")

    def test_create_event_manual_auto(self):
        """Both manual-reset and auto-reset events succeed."""
        c = self.make_client()
        err1, h1 = c.create_event(manual_reset=True)
        err2, h2 = c.create_event(manual_reset=False)
        self.assertEqual(err1, STATUS_SUCCESS)
        self.assertEqual(err2, STATUS_SUCCESS)
        self.assertNotEqual(h1, h2, "Handles must be unique")

    def test_create_named_event(self):
        """Named event creation succeeds."""
        c = self.make_client()
        err, handle = c.create_event(name="test_event_alpha")
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertGreater(handle, 0)

    def test_create_named_event_reuse(self):
        """Second CreateEvent with same name returns STATUS_OBJECT_NAME_EXISTS."""
        c = self.make_client()
        err1, h1 = c.create_event(name="test_reuse_event")
        self.assertEqual(err1, STATUS_SUCCESS)

        err2, h2 = c.create_event(name="test_reuse_event")
        self.assertEqual(err2, STATUS_OBJECT_NAME_EXISTS,
                         f"Expected STATUS_OBJECT_NAME_EXISTS, got {err2:#x}")
        self.assertGreater(h2, 0, "Reuse should still return a handle")

    def test_many_events(self):
        """Can create many events without exhausting resources."""
        c = self.make_client()
        handles = []
        for i in range(50):
            err, h = c.create_event()
            self.assertEqual(err, STATUS_SUCCESS)
            self.assertGreater(h, 0)
            handles.append(h)
        # All handles are unique
        self.assertEqual(len(set(handles)), 50)


class TestEventOp(TriskelionTestBase):
    """Test EventOp (Set/Reset/Pulse) on created events."""

    def test_set_event(self):
        """SetEvent succeeds on a valid handle."""
        c = self.make_client()
        err, handle = c.create_event(manual_reset=True, initial_state=False)
        self.assertEqual(err, STATUS_SUCCESS)

        err, prev = c.event_op(handle, EVENT_SET)
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertEqual(prev, 0, "Event was unsignaled, prev should be 0")

    def test_set_already_signaled(self):
        """SetEvent on already-signaled event returns prev=1."""
        c = self.make_client()
        err, handle = c.create_event(manual_reset=True, initial_state=True)
        self.assertEqual(err, STATUS_SUCCESS)

        err, prev = c.event_op(handle, EVENT_SET)
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertEqual(prev, 1, "Event was signaled, prev should be 1")

    def test_reset_event(self):
        """ResetEvent clears a signaled event."""
        c = self.make_client()
        err, handle = c.create_event(manual_reset=True, initial_state=True)
        self.assertEqual(err, STATUS_SUCCESS)

        err, prev = c.event_op(handle, EVENT_RESET)
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertEqual(prev, 1, "Event was signaled before reset")

    def test_pulse_event(self):
        """PulseEvent sets then resets atomically."""
        c = self.make_client()
        err, handle = c.create_event(manual_reset=True, initial_state=False)
        self.assertEqual(err, STATUS_SUCCESS)

        err, prev = c.event_op(handle, EVENT_PULSE)
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertEqual(prev, 0, "Event was unsignaled before pulse")

    def test_event_op_invalid_handle(self):
        """EventOp on a nonexistent handle returns STATUS_INVALID_HANDLE."""
        c = self.make_client()
        err, _ = c.event_op(0xDEAD, EVENT_SET)
        self.assertEqual(err, STATUS_INVALID_HANDLE,
                         f"Expected STATUS_INVALID_HANDLE, got {err:#x}")

    def test_event_op_after_close(self):
        """EventOp after CloseHandle returns STATUS_INVALID_HANDLE."""
        c = self.make_client()
        err, handle = c.create_event()
        self.assertEqual(err, STATUS_SUCCESS)

        c.close_handle(handle)

        err, _ = c.event_op(handle, EVENT_SET)
        self.assertEqual(err, STATUS_INVALID_HANDLE)


class TestCreateMutex(TriskelionTestBase):
    """Test CreateMutex + ReleaseMutex."""

    def test_create_unowned_mutex(self):
        """Unowned mutex creation succeeds."""
        c = self.make_client()
        err, handle = c.create_mutex(owned=False)
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertGreater(handle, 0)

    def test_create_owned_mutex(self):
        """Owned mutex creation succeeds."""
        c = self.make_client()
        err, handle = c.create_mutex(owned=True)
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertGreater(handle, 0)

    def test_named_mutex_reuse(self):
        """Named mutex returns STATUS_OBJECT_NAME_EXISTS on second create."""
        c = self.make_client()
        err1, h1 = c.create_mutex(name="test_mutex")
        self.assertEqual(err1, STATUS_SUCCESS)

        err2, h2 = c.create_mutex(name="test_mutex")
        self.assertEqual(err2, STATUS_OBJECT_NAME_EXISTS)
        self.assertGreater(h2, 0)


class TestCreateSemaphore(TriskelionTestBase):
    """Test CreateSemaphore + ReleaseSemaphore."""

    def test_create_semaphore(self):
        """Semaphore creation succeeds."""
        c = self.make_client()
        err, handle = c.create_semaphore(initial=0, max_count=10)
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertGreater(handle, 0)

    def test_release_semaphore(self):
        """ReleaseSemaphore returns previous count."""
        c = self.make_client()
        err, handle = c.create_semaphore(initial=2, max_count=10)
        self.assertEqual(err, STATUS_SUCCESS)

        err, prev = c.release_semaphore(handle, 1)
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertEqual(prev, 2, "Previous count should be 2")

    def test_release_semaphore_invalid(self):
        """ReleaseSemaphore on invalid handle returns STATUS_INVALID_HANDLE."""
        c = self.make_client()
        err, _ = c.release_semaphore(0xBEEF)
        self.assertEqual(err, STATUS_INVALID_HANDLE)

    def test_named_semaphore_reuse(self):
        """Named semaphore returns STATUS_OBJECT_NAME_EXISTS on second create."""
        c = self.make_client()
        err1, h1 = c.create_semaphore(initial=1, max_count=5, name="test_sem")
        self.assertEqual(err1, STATUS_SUCCESS)

        err2, h2 = c.create_semaphore(initial=0, max_count=10, name="test_sem")
        self.assertEqual(err2, STATUS_OBJECT_NAME_EXISTS)
        self.assertGreater(h2, 0)


class TestCloseHandle(TriskelionTestBase):
    """Test CloseHandle cleans up ntsync objects."""

    def test_close_event(self):
        """CloseHandle on an event succeeds."""
        c = self.make_client()
        err, handle = c.create_event()
        self.assertEqual(err, STATUS_SUCCESS)

        err = c.close_handle(handle)
        self.assertEqual(err, STATUS_SUCCESS)

    def test_close_then_event_op(self):
        """EventOp after close returns INVALID_HANDLE."""
        c = self.make_client()
        err, handle = c.create_event()
        self.assertEqual(err, STATUS_SUCCESS)

        c.close_handle(handle)
        err, _ = c.event_op(handle, EVENT_SET)
        self.assertEqual(err, STATUS_INVALID_HANDLE)

    def test_close_and_reuse(self):
        """After closing, freelist reuses the event (verified by creating many)."""
        c = self.make_client()
        # Create and close a bunch of events — exercises the freelist
        for _ in range(20):
            err, h = c.create_event(manual_reset=True)
            self.assertEqual(err, STATUS_SUCCESS)
            c.close_handle(h)
        # Create one more — should succeed (freelist or fresh)
        err, h = c.create_event(manual_reset=True)
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertGreater(h, 0)


class TestDupHandle(TriskelionTestBase):
    """Test DupHandle duplicates ntsync objects."""

    def test_dup_event(self):
        """Duplicated event handle is valid and independent."""
        c = self.make_client()
        err, h1 = c.create_event(manual_reset=True, initial_state=False)
        self.assertEqual(err, STATUS_SUCCESS)

        err, h2 = c.dup_handle(h1)
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertNotEqual(h1, h2)

        # Set via original — both should see it
        err, _ = c.event_op(h1, EVENT_SET)
        self.assertEqual(err, STATUS_SUCCESS)

        # The dup'd handle should also be settable
        err, prev = c.event_op(h2, EVENT_SET)
        self.assertEqual(err, STATUS_SUCCESS)
        # prev should be 1 (already signaled from h1)
        self.assertEqual(prev, 1, "Dup'd event should share state with original")

    def test_dup_survives_close_original(self):
        """Closing the original handle doesn't invalidate the dup."""
        c = self.make_client()
        err, h1 = c.create_event(manual_reset=True, initial_state=False)
        self.assertEqual(err, STATUS_SUCCESS)

        err, h2 = c.dup_handle(h1)
        self.assertEqual(err, STATUS_SUCCESS)

        c.close_handle(h1)

        # Dup should still work
        err, _ = c.event_op(h2, EVENT_SET)
        self.assertEqual(err, STATUS_SUCCESS)


class TestSelectPoll(TriskelionTestBase):
    """Test Select with timeout=0 (poll mode)."""

    def test_poll_signaled_event(self):
        """Polling a signaled event returns its index (0)."""
        c = self.make_client()
        err, handle = c.create_event(manual_reset=True, initial_state=True)
        self.assertEqual(err, STATUS_SUCCESS)

        result = c.select_poll([handle])
        self.assertEqual(result, 0, "Signaled event should return index 0")

    def test_poll_unsignaled_event(self):
        """Polling an unsignaled event returns STATUS_TIMEOUT."""
        c = self.make_client()
        err, handle = c.create_event(manual_reset=True, initial_state=False)
        self.assertEqual(err, STATUS_SUCCESS)

        result = c.select_poll([handle])
        self.assertEqual(result, STATUS_TIMEOUT,
                         f"Unsignaled event should timeout, got {result:#x}")

    def test_poll_multiple_first_signaled(self):
        """Polling multiple handles returns index of first signaled."""
        c = self.make_client()
        _, h1 = c.create_event(manual_reset=True, initial_state=True)   # signaled
        _, h2 = c.create_event(manual_reset=True, initial_state=False)  # unsignaled

        result = c.select_poll([h1, h2])
        self.assertEqual(result, 0, "First handle is signaled → index 0")

    def test_poll_multiple_second_signaled(self):
        """Polling multiple handles returns index of second when first is unsignaled."""
        c = self.make_client()
        _, h1 = c.create_event(manual_reset=True, initial_state=False)  # unsignaled
        _, h2 = c.create_event(manual_reset=True, initial_state=True)   # signaled

        result = c.select_poll([h1, h2])
        self.assertEqual(result, 1, "Second handle is signaled → index 1")

    def test_poll_no_handles(self):
        """Polling with no handles returns STATUS_TIMEOUT."""
        c = self.make_client()
        result = c.select_poll([])
        self.assertEqual(result, STATUS_TIMEOUT)

    def test_poll_after_set(self):
        """Create unsignaled → SetEvent → poll sees it signaled."""
        c = self.make_client()
        err, handle = c.create_event(manual_reset=True, initial_state=False)
        self.assertEqual(err, STATUS_SUCCESS)

        # Should be unsignaled
        result = c.select_poll([handle])
        self.assertEqual(result, STATUS_TIMEOUT)

        # Signal it
        c.event_op(handle, EVENT_SET)

        # Should be signaled now
        result = c.select_poll([handle])
        self.assertEqual(result, 0)

    def test_poll_after_reset(self):
        """Create signaled → ResetEvent → poll sees it unsignaled."""
        c = self.make_client()
        err, handle = c.create_event(manual_reset=True, initial_state=True)
        self.assertEqual(err, STATUS_SUCCESS)

        # Should be signaled
        result = c.select_poll([handle])
        self.assertEqual(result, 0)

        # Reset it
        c.event_op(handle, EVENT_RESET)

        # Should be unsignaled now
        result = c.select_poll([handle])
        self.assertEqual(result, STATUS_TIMEOUT)

    def test_poll_semaphore(self):
        """Polling a semaphore with count>0 returns signaled."""
        c = self.make_client()
        _, handle = c.create_semaphore(initial=1, max_count=5)

        result = c.select_poll([handle])
        self.assertEqual(result, 0, "Semaphore with count>0 should be signaled")

    def test_poll_empty_semaphore(self):
        """Polling a semaphore with count=0 returns timeout."""
        c = self.make_client()
        _, handle = c.create_semaphore(initial=0, max_count=5)

        result = c.select_poll([handle])
        self.assertEqual(result, STATUS_TIMEOUT)

    def test_poll_unowned_mutex(self):
        """Polling an unowned mutex returns signaled (acquirable)."""
        c = self.make_client()
        _, handle = c.create_mutex(owned=False)

        result = c.select_poll([handle])
        self.assertEqual(result, 0, "Unowned mutex should be acquirable")


class TestSelectFallback(TriskelionTestBase):
    """Test Select with handles NOT in ntsync_objects (signaled fallback)."""

    def test_poll_unknown_handle(self):
        """Polling a handle that was never registered as a sync object
        should create a signaled fallback (not hang)."""
        c = self.make_client()
        # Allocate a handle via CreateEvent, close it (removes ntsync obj),
        # then create a plain handle (not a sync object)
        # Actually, just use a handle number that doesn't exist — if the
        # server creates a signaled fallback, poll should return 0
        err, handle = c.create_event(manual_reset=True, initial_state=True)
        self.assertEqual(err, STATUS_SUCCESS)

        # Close it to remove from ntsync_objects
        c.close_handle(handle)

        # Now poll it — should get a signaled fallback (not hang)
        result = c.select_poll([handle])
        self.assertEqual(result, 0,
                         "Fallback should create signaled event → immediate return")


class TestGetInprocSyncFd(TriskelionTestBase):
    """Test GetInprocSyncFd sends ntsync fds to clients."""

    def test_get_event_fd(self):
        """GetInprocSyncFd for an event returns a valid ntsync fd."""
        c = self.make_client()
        err, handle = c.create_event(manual_reset=True, initial_state=False)
        self.assertEqual(err, STATUS_SUCCESS)

        err, sync_type, fd = c.get_inproc_sync_fd(handle)
        self.assertEqual(err, STATUS_SUCCESS)
        # sync_type 2 = INPROC_SYNC_EVENT
        self.assertEqual(sync_type, 2, f"Expected event type (2), got {sync_type}")
        self.assertIsNotNone(fd)
        self.assertGreaterEqual(fd, 0, "Expected valid fd")
        os.close(fd)

    def test_inproc_fd_mirrors_state(self):
        """ntsync fd from GetInprocSyncFd reflects SetEvent."""
        c = self.make_client()
        err, handle = c.create_event(manual_reset=True, initial_state=False)
        self.assertEqual(err, STATUS_SUCCESS)

        err, _, ntsync_fd = c.get_inproc_sync_fd(handle)
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertGreaterEqual(ntsync_fd, 0)

        # Set event via protocol
        c.event_op(handle, EVENT_SET)

        # Verify via direct ntsync ioctl: reset the event and check prev state
        import fcntl
        buf = bytearray(4)
        try:
            fcntl.ioctl(ntsync_fd, NTSYNC_IOC_EVENT_RESET, buf, True)
            prev = struct.unpack('<I', buf)[0]
            # prev=1 means it was signaled before reset → SetEvent worked
            self.assertEqual(prev, 1, "Event should have been signaled by SetEvent")
        except OSError:
            self.fail("ntsync fd should be a valid event")
        finally:
            os.close(ntsync_fd)

    def test_get_device_fd(self):
        """GetInprocSyncFd with handle=0 returns the ntsync device fd."""
        c = self.make_client()
        err, sync_type, fd = c.get_inproc_sync_fd(0)
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertGreaterEqual(fd, 0)
        # Verify it's a real /dev/ntsync fd by trying to create an event on it
        try:
            buf = bytearray(struct.pack('<II', 0, 0))  # NtsyncEventArgs: manual=0, signaled=0
            result = fcntl.ioctl(fd, 0x40084E87, buf, True)  # NTSYNC_IOC_CREATE_EVENT
            self.assertGreaterEqual(result, 0, "Should be able to create event on device fd")
            os.close(result)  # close the created event
        except OSError as e:
            self.fail(f"Expected valid ntsync device fd: {e}")
        finally:
            os.close(fd)

    def test_fallback_for_unknown_handle(self):
        """GetInprocSyncFd for unknown handle creates signaled fallback."""
        c = self.make_client()
        err, handle = c.create_event()
        c.close_handle(handle)

        # Now the handle is gone from ntsync_objects — fallback path
        err, sync_type, fd = c.get_inproc_sync_fd(handle)
        self.assertEqual(err, STATUS_SUCCESS, "Fallback should succeed")
        self.assertEqual(sync_type, 2, "Fallback is an event (INPROC_SYNC_EVENT)")
        self.assertGreaterEqual(fd, 0)
        os.close(fd)

        # Verify the handle was re-inserted by polling it via Select
        result = c.select_poll([handle])
        self.assertEqual(result, 0,
                         "Fallback event should be signaled → immediate return")


class TestNamedSyncCrossProcess(TriskelionTestBase):
    """Test named sync objects shared across different client processes."""

    def test_named_event_cross_process(self):
        """Named event created by client A can be opened by client B."""
        a = self.make_client()
        b = self.make_client()

        err, h_a = a.create_event(manual_reset=True, initial_state=False,
                                  name="cross_proc_event")
        self.assertEqual(err, STATUS_SUCCESS)

        # Client B creates the same named event → gets STATUS_OBJECT_NAME_EXISTS
        err, h_b = b.create_event(manual_reset=True, initial_state=False,
                                  name="cross_proc_event")
        self.assertEqual(err, STATUS_OBJECT_NAME_EXISTS)
        self.assertGreater(h_b, 0)

        # Set from A, poll from B
        a.event_op(h_a, EVENT_SET)

        result = b.select_poll([h_b])
        self.assertEqual(result, 0,
                         "SetEvent from A should be visible when polling from B")

    def test_named_mutex_cross_process(self):
        """Named mutex shared across clients."""
        a = self.make_client()
        b = self.make_client()

        err, h_a = a.create_mutex(name="cross_proc_mutex")
        self.assertEqual(err, STATUS_SUCCESS)

        err, h_b = b.create_mutex(name="cross_proc_mutex")
        self.assertEqual(err, STATUS_OBJECT_NAME_EXISTS)
        self.assertGreater(h_b, 0)


class TestWinebootPattern(TriskelionTestBase):
    """Reproduce the wineboot signal chain that was deadlocking.

    The real pattern:
    1. start.exe creates named event "__wineboot_event" (OBJ_OPENIF)
    2. start.exe creates wineboot.exe process
    3. start.exe waits on [event, process_handle] (WaitAny)
    4. wineboot opens "__wineboot_event" (OBJ_OPENIF → gets existing)
    5. wineboot signals the event
    6. start.exe should wake up

    This test verifies steps 1, 4, 5 work correctly through the protocol.
    We can't test the full process creation chain here, but we verify the
    named event create → reuse → signal → poll path.
    """

    def test_wineboot_event_signal_chain(self):
        """Named event create → reuse → set → poll: the wineboot pattern."""
        start = self.make_client()
        wineboot = self.make_client()

        # start.exe creates the event
        err, h_start = start.create_event(
            manual_reset=True, initial_state=False,
            name="__wineboot_event")
        self.assertEqual(err, STATUS_SUCCESS)

        # Verify it's unsignaled
        result = start.select_poll([h_start])
        self.assertEqual(result, STATUS_TIMEOUT)

        # wineboot opens the same event
        err, h_boot = wineboot.create_event(
            manual_reset=True, initial_state=False,
            name="__wineboot_event")
        self.assertEqual(err, STATUS_OBJECT_NAME_EXISTS,
                         "wineboot should get NAME_EXISTS for existing event")

        # wineboot signals it
        err, prev = wineboot.event_op(h_boot, EVENT_SET)
        self.assertEqual(err, STATUS_SUCCESS)
        self.assertEqual(prev, 0, "Event was unsignaled before wineboot set it")

        # start.exe polls — should see it signaled
        result = start.select_poll([h_start])
        self.assertEqual(result, 0,
                         "start.exe should see the event signaled by wineboot")


class TestDisconnectSignaling(TriskelionTestBase):
    """Test that client disconnect triggers exit event signaling."""

    def test_disconnect_signals_nothing_for_bare_client(self):
        """Disconnecting a client that has no thread exit events is clean."""
        c = self.make_client()
        pid = c.pid
        # Just close — should not crash the server
        c.close()
        # Verify server is still alive
        time.sleep(0.1)
        c2 = self.make_client()
        self.assertGreater(c2.pid, 0)


# ---- Entry point ----

if __name__ == '__main__':
    import fcntl
    unittest.main()

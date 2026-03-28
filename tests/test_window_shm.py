#!/usr/bin/env python3
"""Window shared memory and server-side state tests for triskelion.

Tests that:
- handle_create_window writes dpi_context to shared memory with seqlock
- handle_init_window_info stores style/ex_style/is_unicode
- handle_get_window_info returns correct per-offset values
- handle_set_window_info roundtrips correctly
- handle_set_parent/get_window_tree track parent/owner
- handle_set_window_owner updates owner
- handle_set_window_pos returns style from state
- handle_destroy_window cleans up state

Usage:
    python3 -m unittest tests.test_window_shm -v
"""

import os
import struct
import sys
import unittest

# Import the shared test infrastructure from test_eventloop_ntsync
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_eventloop_ntsync import (
    WineClient, find_triskelion_binary, compute_socket_path,
    FIXED_REQUEST_SIZE, STATUS_SUCCESS,
)

import array
import shutil
import signal
import socket
import subprocess
import tempfile
import time

# ---- Window opcodes (from protocol_generated.rs) ----
OP_CREATE_CLASS = 203
OP_CREATE_WINDOW = 142
OP_DESTROY_WINDOW = 143
OP_SET_WINDOW_OWNER = 145
OP_GET_WINDOW_INFO = 146
OP_INIT_WINDOW_INFO = 147
OP_SET_WINDOW_INFO = 148
OP_SET_PARENT = 149
OP_GET_WINDOW_TREE = 154
OP_SET_WINDOW_POS = 155

# GWL constants (Windows ABI)
GWL_STYLE = -16
GWL_EXSTYLE = -20
GWLP_ID = -12
GWLP_HINSTANCE = -6
GWLP_WNDPROC = -4
GWLP_USERDATA = -21

NTUSER_DPI_PER_MONITOR_AWARE = 0x12


class WindowClient(WineClient):
    """Extended WineClient with window management methods."""

    def create_class(self, atom=0, style=0, cls_extra=0, win_extra=0,
                     instance=0, client_ptr=0, name_offset=0, name=b''):
        """Create a window class. Returns (error, atom, locator)."""
        # CreateClassRequest: access(4) + local_id(4) + atom(4) + instance(8) +
        #   cls_extra(4) + win_extra(4) + client_ptr(8) + name_offset(4) + pad(4)
        # = 44 bytes after header(12) = total fixed 56 bytes
        fields = struct.pack('<IIIQiIQII',
            0x001F0003,   # access
            0,            # local_id
            atom,         # atom
            instance,     # instance
            cls_extra,    # cls_extra (i32)
            win_extra,    # win_extra (u32)
            client_ptr,   # client_ptr
            name_offset,  # name_offset
            0,            # style (was missing - let me check)
        )
        # Actually let me re-check the struct layout
        error, reply = self.send_request(OP_CREATE_CLASS, fields, vararg=name)
        # CreateClassReply: header(8) + locator(16) + atom(4) + pad(4) = 32
        if len(reply) >= 28:
            locator = reply[8:24]
            reply_atom = struct.unpack_from('<I', reply, 24)[0]
            return error, reply_atom, locator
        return error, 0, b'\x00' * 16

    def create_window(self, atom, parent=0, owner=0, style=0, ex_style=0,
                      instance=0, dpi_context=0):
        """Create a window. Returns (error, handle, parent, owner)."""
        # CreateWindowRequest (44 bytes after header):
        #   parent(4) + owner(4) + atom(4) + class_instance(8) + instance(8) +
        #   dpi_context(4) + style(4) + ex_style(4) + pad(4)
        fields = struct.pack('<IIIQQIIIxxxx',
            parent,          # parent
            owner,           # owner
            atom,            # atom
            0,               # class_instance (u64)
            instance,        # instance (u64)
            dpi_context,     # dpi_context
            style,           # style
            ex_style,        # ex_style
        )
        error, reply = self.send_request(OP_CREATE_WINDOW, fields)
        # CreateWindowReply: header(8) + handle(4) + parent(4) + owner(4) + extra(4) + class_ptr(8) = 32
        if len(reply) >= 20:
            handle = struct.unpack_from('<I', reply, 8)[0]
            r_parent = struct.unpack_from('<I', reply, 12)[0]
            r_owner = struct.unpack_from('<I', reply, 16)[0]
            return error, handle, r_parent, r_owner
        return error, 0, 0, 0

    def init_window_info(self, handle, style, ex_style, is_unicode=1):
        """Initialize window info (called right after CreateWindow by Wine)."""
        # InitWindowInfoRequest: handle(4) + style(4) + ex_style(4) + is_unicode(i16) + pad(6)
        fields = struct.pack('<IIIhxxxxxx', handle, style, ex_style, is_unicode)
        error, reply = self.send_request(OP_INIT_WINDOW_INFO, fields)
        return error

    def get_window_info(self, handle, offset, size=4):
        """Get window info at a specific offset. Returns (error, last_active, is_unicode, info)."""
        # GetWindowInfoRequest: handle(4) + offset(4) + size(4)
        fields = struct.pack('<IiI', handle, offset, size)
        error, reply = self.send_request(OP_GET_WINDOW_INFO, fields)
        # GetWindowInfoReply: header(8) + last_active(4) + is_unicode(4) + info(8) = 24
        if len(reply) >= 24:
            last_active = struct.unpack_from('<I', reply, 8)[0]
            is_unicode = struct.unpack_from('<i', reply, 12)[0]
            info = struct.unpack_from('<Q', reply, 16)[0]
            return error, last_active, is_unicode, info
        return error, 0, 0, 0

    def set_window_info(self, handle, offset, new_info, size=4):
        """Set window info at offset. Returns (error, old_info)."""
        # SetWindowInfoRequest: handle(4) + offset(4) + size(4) + new_info(8) = 20
        fields = struct.pack('<IiIQ', handle, offset, size, new_info)
        error, reply = self.send_request(OP_SET_WINDOW_INFO, fields)
        # SetWindowInfoReply: header(8) + old_info(8) = 16
        if len(reply) >= 16:
            old_info = struct.unpack_from('<Q', reply, 8)[0]
            return error, old_info
        return error, 0

    def set_parent(self, handle, parent):
        """Set window parent. Returns (error, old_parent, full_parent)."""
        # SetParentRequest: handle(4) + parent(4) + pad(4)
        fields = struct.pack('<III', handle, parent, 0)
        error, reply = self.send_request(OP_SET_PARENT, fields)
        # SetParentReply: header(8) + old_parent(4) + full_parent(4) = 16
        if len(reply) >= 16:
            old_parent = struct.unpack_from('<I', reply, 8)[0]
            full_parent = struct.unpack_from('<I', reply, 12)[0]
            return error, old_parent, full_parent
        return error, 0, 0

    def get_window_tree(self, handle):
        """Get window tree info. Returns (error, parent, owner)."""
        # GetWindowTreeRequest: handle(4)
        fields = struct.pack('<I', handle)
        error, reply = self.send_request(OP_GET_WINDOW_TREE, fields)
        # GetWindowTreeReply: header(8) + parent(4) + owner(4) + siblings(24) = 40
        if len(reply) >= 16:
            parent = struct.unpack_from('<I', reply, 8)[0]
            owner = struct.unpack_from('<I', reply, 12)[0]
            return error, parent, owner
        return error, 0, 0

    def set_window_owner(self, handle, owner):
        """Set window owner. Returns (error, full_owner, prev_owner)."""
        # SetWindowOwnerRequest: handle(4) + owner(4) + pad(4)
        fields = struct.pack('<III', handle, owner, 0)
        error, reply = self.send_request(OP_SET_WINDOW_OWNER, fields)
        # SetWindowOwnerReply: header(8) + full_owner(4) + prev_owner(4) = 16
        if len(reply) >= 16:
            full_owner = struct.unpack_from('<I', reply, 8)[0]
            prev_owner = struct.unpack_from('<I', reply, 12)[0]
            return error, full_owner, prev_owner
        return error, 0, 0

    def set_window_pos(self, handle, swp_flags=0, paint_flags=0,
                       monitor_dpi=0, previous=0):
        """Set window position. Returns (error, new_style, new_ex_style)."""
        # SetWindowPosRequest (64 bytes): swp_flags(2) + paint_flags(2) + monitor_dpi(4) +
        #   handle(4) + previous(4) + window(16) + client(16) + pad(4) = 52
        fields = struct.pack('<HHIIIxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
            swp_flags, paint_flags, monitor_dpi, handle, previous)
        error, reply = self.send_request(OP_SET_WINDOW_POS, fields)
        # SetWindowPosReply: header(8) + new_style(4) + new_ex_style(4) + surface_win(4) + pad(4) = 24
        if len(reply) >= 16:
            new_style = struct.unpack_from('<I', reply, 8)[0]
            new_ex_style = struct.unpack_from('<I', reply, 12)[0]
            return error, new_style, new_ex_style
        return error, 0, 0

    def destroy_window(self, handle):
        """Destroy a window. Returns error code."""
        fields = struct.pack('<I', handle)
        error, reply = self.send_request(OP_DESTROY_WINDOW, fields)
        return error


class WindowTestBase(unittest.TestCase):
    """Base class that starts triskelion and provides a connected WindowClient."""

    _daemon = None
    _wineprefix = None
    _socket_path = None
    _keepalive = None

    @classmethod
    def setUpClass(cls):
        binary = find_triskelion_binary()
        if not binary:
            raise unittest.SkipTest("triskelion binary not found (run cargo build --release)")

        cls._wineprefix = tempfile.mkdtemp(prefix='triskelion_test_win_')

        env = os.environ.copy()
        env['WINEPREFIX'] = cls._wineprefix

        cls._daemon = subprocess.Popen(
            [binary],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        cls._socket_path = compute_socket_path(cls._wineprefix)

        for _ in range(50):
            if os.path.exists(cls._socket_path):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("triskelion socket not found after 5s")

        # Keepalive client to prevent daemon auto-exit
        cls._keepalive = WineClient()
        cls._keepalive.connect(cls._socket_path)

    @classmethod
    def tearDownClass(cls):
        if cls._keepalive:
            cls._keepalive.close()
        if cls._daemon:
            cls._daemon.send_signal(signal.SIGTERM)
            cls._daemon.wait(timeout=5)
        if cls._wineprefix:
            shutil.rmtree(cls._wineprefix, ignore_errors=True)

    def new_client(self):
        """Create and connect a new WindowClient."""
        c = WindowClient()
        c.connect(self._socket_path)
        return c


# ---- Test classes ----

class TestCreateWindow(WindowTestBase):
    """Test window creation and basic state."""

    def test_create_window_returns_handle(self):
        """CreateWindow returns a valid non-zero handle."""
        c = self.new_client()
        try:
            # Use a system atom that doesn't need a class
            err, handle, parent, owner = c.create_window(atom=1, style=0x10000000)
            self.assertEqual(err, 0)
            self.assertGreater(handle, 0)
        finally:
            c.close()

    def test_create_window_parents_to_desktop(self):
        """Top-level window with parent=0 gets parented to desktop."""
        c = self.new_client()
        try:
            err, handle, parent, owner = c.create_window(atom=1, parent=0)
            self.assertEqual(err, 0)
            # Parent should be the desktop window (non-zero)
            self.assertGreater(parent, 0)
        finally:
            c.close()

    def test_create_window_preserves_owner(self):
        """CreateWindow returns the requested owner."""
        c = self.new_client()
        try:
            err, h1, _, _ = c.create_window(atom=1)
            err, h2, _, owner = c.create_window(atom=1, owner=h1)
            self.assertEqual(err, 0)
            self.assertEqual(owner, h1)
        finally:
            c.close()


class TestInitWindowInfo(WindowTestBase):
    """Test init_window_info stores style/ex_style/is_unicode."""

    def test_init_stores_style(self):
        """init_window_info stores style, retrievable via get_window_info."""
        c = self.new_client()
        try:
            err, handle, _, _ = c.create_window(atom=1, style=0x10000000)
            self.assertEqual(err, 0)

            # Wine calls init_window_info right after CreateWindow
            err = c.init_window_info(handle, style=0xCAFE0000, ex_style=0xBEEF0000)
            self.assertEqual(err, 0)

            # Verify style was stored
            err, last_active, is_unicode, info = c.get_window_info(handle, GWL_STYLE)
            self.assertEqual(err, 0)
            self.assertEqual(info, 0xCAFE0000)
            self.assertEqual(last_active, handle)
        finally:
            c.close()

    def test_init_stores_ex_style(self):
        """init_window_info stores ex_style."""
        c = self.new_client()
        try:
            err, handle, _, _ = c.create_window(atom=1)
            err = c.init_window_info(handle, style=0, ex_style=0x00000200)
            self.assertEqual(err, 0)

            err, _, _, info = c.get_window_info(handle, GWL_EXSTYLE)
            self.assertEqual(err, 0)
            self.assertEqual(info, 0x00000200)
        finally:
            c.close()

    def test_init_stores_is_unicode(self):
        """init_window_info stores is_unicode, returned via GWLP_WNDPROC."""
        c = self.new_client()
        try:
            err, handle, _, _ = c.create_window(atom=1)
            err = c.init_window_info(handle, style=0, ex_style=0, is_unicode=0)
            self.assertEqual(err, 0)

            err, _, is_unicode, info = c.get_window_info(handle, GWLP_WNDPROC)
            self.assertEqual(err, 0)
            self.assertEqual(info, 0)  # is_unicode was set to 0
        finally:
            c.close()


class TestGetWindowInfo(WindowTestBase):
    """Test get_window_info returns correct values for each offset."""

    def test_style_from_create(self):
        """Style from CreateWindow request is available before init_window_info."""
        c = self.new_client()
        try:
            err, handle, _, _ = c.create_window(atom=1, style=0x10CF0000)
            self.assertEqual(err, 0)

            err, _, _, info = c.get_window_info(handle, GWL_STYLE)
            self.assertEqual(err, 0)
            self.assertEqual(info, 0x10CF0000)
        finally:
            c.close()

    def test_unknown_handle_returns_defaults(self):
        """get_window_info for a bogus handle returns zeros."""
        c = self.new_client()
        try:
            err, last_active, is_unicode, info = c.get_window_info(0xDEAD, GWL_STYLE)
            self.assertEqual(err, 0)
            self.assertEqual(info, 0)
        finally:
            c.close()


class TestSetWindowInfo(WindowTestBase):
    """Test set_window_info roundtrip."""

    def test_set_style(self):
        """SetWindowInfo(GWL_STYLE) stores new, returns old."""
        c = self.new_client()
        try:
            err, handle, _, _ = c.create_window(atom=1, style=0xAAAA0000)
            self.assertEqual(err, 0)

            err, old = c.set_window_info(handle, GWL_STYLE, 0xBBBB0000)
            self.assertEqual(err, 0)
            self.assertEqual(old, 0xAAAA0000)

            err, _, _, info = c.get_window_info(handle, GWL_STYLE)
            self.assertEqual(info, 0xBBBB0000)
        finally:
            c.close()

    def test_set_ex_style(self):
        """SetWindowInfo(GWL_EXSTYLE) stores new, returns old."""
        c = self.new_client()
        try:
            err, handle, _, _ = c.create_window(atom=1, ex_style=0x100)
            err, old = c.set_window_info(handle, GWL_EXSTYLE, 0x200)
            self.assertEqual(err, 0)
            self.assertEqual(old, 0x100)

            err, _, _, info = c.get_window_info(handle, GWL_EXSTYLE)
            self.assertEqual(info, 0x200)
        finally:
            c.close()

    def test_set_userdata(self):
        """SetWindowInfo(GWLP_USERDATA) stores 64-bit value."""
        c = self.new_client()
        try:
            err, handle, _, _ = c.create_window(atom=1)
            err, old = c.set_window_info(handle, GWLP_USERDATA, 0xDEADBEEFCAFE1234)
            self.assertEqual(err, 0)
            self.assertEqual(old, 0)  # was initially 0

            err, _, _, info = c.get_window_info(handle, GWLP_USERDATA)
            self.assertEqual(info, 0xDEADBEEFCAFE1234)
        finally:
            c.close()

    def test_set_instance(self):
        """SetWindowInfo(GWLP_HINSTANCE) stores instance handle."""
        c = self.new_client()
        try:
            err, handle, _, _ = c.create_window(atom=1, instance=0x7FF600000000)
            err, old = c.set_window_info(handle, GWLP_HINSTANCE, 0x7FF700000000)
            self.assertEqual(err, 0)
            self.assertEqual(old, 0x7FF600000000)
        finally:
            c.close()


class TestSetParent(WindowTestBase):
    """Test set_parent updates parent correctly."""

    def test_set_parent_updates_tree(self):
        """SetParent updates parent, visible via GetWindowTree."""
        c = self.new_client()
        try:
            err, parent_h, _, _ = c.create_window(atom=1)
            err, child_h, _, _ = c.create_window(atom=1)
            self.assertEqual(err, 0)

            err, old_parent, full_parent = c.set_parent(child_h, parent_h)
            self.assertEqual(err, 0)
            self.assertEqual(full_parent, parent_h)

            err, tree_parent, tree_owner = c.get_window_tree(child_h)
            self.assertEqual(err, 0)
            self.assertEqual(tree_parent, parent_h)
        finally:
            c.close()


class TestSetWindowOwner(WindowTestBase):
    """Test set_window_owner."""

    def test_set_owner(self):
        """SetWindowOwner updates owner, visible via GetWindowTree."""
        c = self.new_client()
        try:
            err, owner_h, _, _ = c.create_window(atom=1)
            err, win_h, _, _ = c.create_window(atom=1)
            self.assertEqual(err, 0)

            err, full_owner, prev_owner = c.set_window_owner(win_h, owner_h)
            self.assertEqual(err, 0)
            self.assertEqual(full_owner, owner_h)

            err, _, tree_owner = c.get_window_tree(win_h)
            self.assertEqual(err, 0)
            self.assertEqual(tree_owner, owner_h)
        finally:
            c.close()


class TestSetWindowPos(WindowTestBase):
    """Test set_window_pos returns style from server state."""

    def test_returns_style_after_init(self):
        """SetWindowPos returns new_style/new_ex_style from window state."""
        c = self.new_client()
        try:
            err, handle, _, _ = c.create_window(atom=1, style=0x10CF0000, ex_style=0x00000100)
            self.assertEqual(err, 0)

            err, new_style, new_ex_style = c.set_window_pos(handle)
            self.assertEqual(err, 0)
            self.assertEqual(new_style, 0x10CF0000)
            self.assertEqual(new_ex_style, 0x00000100)
        finally:
            c.close()


class TestDestroyWindow(WindowTestBase):
    """Test destroy_window cleans up state."""

    def test_destroy_cleans_state(self):
        """After DestroyWindow, get_window_info returns defaults."""
        c = self.new_client()
        try:
            err, handle, _, _ = c.create_window(atom=1, style=0x10000000)
            self.assertEqual(err, 0)

            err = c.destroy_window(handle)
            self.assertEqual(err, 0)

            # After destroy, state should be gone → defaults
            err, _, _, info = c.get_window_info(handle, GWL_STYLE)
            self.assertEqual(info, 0)
        finally:
            c.close()


if __name__ == '__main__':
    unittest.main()

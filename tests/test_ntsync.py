#!/usr/bin/env python3
"""Unit tests for /dev/ntsync kernel driver.

Tests the kernel ioctl interface that triskelion's ntsync.rs wraps.
No triskelion process needed — exercises the driver directly.

Usage:
    python3 tests/test_ntsync.py
    python3 tests/test_ntsync.py -v          # verbose
    python3 tests/test_ntsync.py TestEvents   # run one class
"""

import ctypes
import errno
import fcntl
import os
import struct
import sys
import threading
import time
import unittest

# ---- ioctl codes (must match ntsync.rs / linux/ntsync.h) ----

NTSYNC_IOC_CREATE_SEM   = 0x40084E80
NTSYNC_IOC_SEM_RELEASE  = 0xC0044E81
NTSYNC_IOC_WAIT_ANY     = 0xC0284E82
NTSYNC_IOC_WAIT_ALL     = 0xC0284E83
NTSYNC_IOC_CREATE_MUTEX = 0x40084E84
NTSYNC_IOC_MUTEX_UNLOCK = 0xC0084E85
NTSYNC_IOC_CREATE_EVENT = 0x40084E87
NTSYNC_IOC_EVENT_SET    = 0x80044E88
NTSYNC_IOC_EVENT_RESET  = 0x80044E89
NTSYNC_IOC_EVENT_PULSE  = 0x80044E8A


# ---- helpers ----

def open_ntsync():
    """Open /dev/ntsync. Skip all tests if unavailable."""
    try:
        return os.open("/dev/ntsync", os.O_RDWR | os.O_CLOEXEC)
    except OSError:
        return None


def create_sem(dev_fd, count, maximum):
    """Create semaphore. Returns object fd."""
    buf = bytearray(struct.pack("II", count, maximum))
    return fcntl.ioctl(dev_fd, NTSYNC_IOC_CREATE_SEM, buf, True)


def sem_release(obj_fd, count):
    """Release semaphore. Returns previous count."""
    buf = bytearray(struct.pack("I", count))
    fcntl.ioctl(obj_fd, NTSYNC_IOC_SEM_RELEASE, buf, True)
    return struct.unpack("I", buf)[0]


def create_mutex(dev_fd, owner, count):
    """Create mutex. Returns object fd."""
    buf = bytearray(struct.pack("II", owner, count))
    return fcntl.ioctl(dev_fd, NTSYNC_IOC_CREATE_MUTEX, buf, True)


def mutex_unlock(obj_fd, owner):
    """Unlock mutex. Returns previous count."""
    buf = bytearray(struct.pack("II", owner, 0))
    fcntl.ioctl(obj_fd, NTSYNC_IOC_MUTEX_UNLOCK, buf, True)
    _owner, count = struct.unpack("II", buf)
    return count


def create_event(dev_fd, manual, signaled):
    """Create event. Returns object fd."""
    buf = bytearray(struct.pack("II", int(manual), int(signaled)))
    return fcntl.ioctl(dev_fd, NTSYNC_IOC_CREATE_EVENT, buf, True)


def event_set(obj_fd):
    """Set event. Returns previous state."""
    buf = bytearray(4)
    fcntl.ioctl(obj_fd, NTSYNC_IOC_EVENT_SET, buf, True)
    return struct.unpack("I", buf)[0]


def event_reset(obj_fd):
    """Reset event. Returns previous state."""
    buf = bytearray(4)
    fcntl.ioctl(obj_fd, NTSYNC_IOC_EVENT_RESET, buf, True)
    return struct.unpack("I", buf)[0]


def event_pulse(obj_fd):
    """Pulse event. Returns previous state."""
    buf = bytearray(4)
    fcntl.ioctl(obj_fd, NTSYNC_IOC_EVENT_PULSE, buf, True)
    return struct.unpack("I", buf)[0]


def wait_any(dev_fd, obj_fds, timeout_ns, owner=0, alert_fd=0):
    """Wait for any object. Returns (index) or raises OSError on timeout/alert."""
    count = len(obj_fds)
    fds_array = (ctypes.c_uint32 * count)(*obj_fds)
    buf = bytearray(struct.pack("QQIIIIII",
        timeout_ns,
        ctypes.addressof(fds_array),
        count,
        0,      # index (output)
        0,      # flags (CLOCK_MONOTONIC)
        owner,
        alert_fd,
        0,      # pad
    ))
    fcntl.ioctl(dev_fd, NTSYNC_IOC_WAIT_ANY, buf, True)
    _timeout, _objs, _count, index, _flags, _owner, _alert, _pad = struct.unpack("QQIIIIII", buf)
    return index


def wait_all(dev_fd, obj_fds, timeout_ns, owner=0, alert_fd=0):
    """Wait for all objects. Returns 0 or raises OSError."""
    count = len(obj_fds)
    fds_array = (ctypes.c_uint32 * count)(*obj_fds)
    buf = bytearray(struct.pack("QQIIIIII",
        timeout_ns,
        ctypes.addressof(fds_array),
        count,
        0, 0, owner, alert_fd, 0,
    ))
    fcntl.ioctl(dev_fd, NTSYNC_IOC_WAIT_ALL, buf, True)
    _timeout, _objs, _count, index, _flags, _owner, _alert, _pad = struct.unpack("QQIIIIII", buf)
    return index


def poll_signaled(dev_fd, obj_fd):
    """Quick poll: is object signaled right now? Non-destructive for manual events."""
    try:
        wait_any(dev_fd, [obj_fd], 1)  # 1ns = already expired
        return True
    except OSError:
        return False


def monotonic_ns():
    """Current CLOCK_MONOTONIC in nanoseconds."""
    t = time.clock_gettime(time.CLOCK_MONOTONIC)
    return int(t * 1_000_000_000)


# ---- Test classes ----

class NtsyncTestCase(unittest.TestCase):
    """Base class that opens /dev/ntsync and tracks fds for cleanup."""

    @classmethod
    def setUpClass(cls):
        cls.dev_fd = open_ntsync()
        if cls.dev_fd is None:
            raise unittest.SkipTest("/dev/ntsync not available")

    def setUp(self):
        self._fds = []

    def tearDown(self):
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass

    def track(self, fd):
        """Register fd for auto-close in tearDown."""
        self._fds.append(fd)
        return fd


class TestEvents(NtsyncTestCase):
    """Test ntsync event create/set/reset/pulse."""

    def test_create_manual_unsignaled(self):
        e = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        self.assertGreater(e, 0)
        self.assertFalse(poll_signaled(self.dev_fd, e))

    def test_create_manual_signaled(self):
        e = self.track(create_event(self.dev_fd, manual=True, signaled=True))
        self.assertTrue(poll_signaled(self.dev_fd, e))

    def test_create_auto_unsignaled(self):
        e = self.track(create_event(self.dev_fd, manual=False, signaled=False))
        self.assertFalse(poll_signaled(self.dev_fd, e))

    def test_create_auto_signaled(self):
        e = self.track(create_event(self.dev_fd, manual=False, signaled=True))
        # Auto-reset: poll consumes the signal
        self.assertTrue(poll_signaled(self.dev_fd, e))
        # Second poll: should be unsignaled now
        self.assertFalse(poll_signaled(self.dev_fd, e))

    def test_set_reset_manual(self):
        e = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        prev = event_set(e)
        self.assertEqual(prev, 0)  # was unsignaled
        self.assertTrue(poll_signaled(self.dev_fd, e))
        prev = event_reset(e)
        self.assertEqual(prev, 1)  # was signaled
        self.assertFalse(poll_signaled(self.dev_fd, e))

    def test_set_idempotent(self):
        e = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        event_set(e)
        prev = event_set(e)
        self.assertEqual(prev, 1)  # already signaled
        self.assertTrue(poll_signaled(self.dev_fd, e))

    def test_reset_idempotent(self):
        e = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        prev = event_reset(e)
        self.assertEqual(prev, 0)  # already unsignaled

    def test_pulse_manual(self):
        e = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        prev = event_pulse(e)
        self.assertEqual(prev, 0)
        # Pulse sets then immediately resets — no waiters means stays unsignaled
        self.assertFalse(poll_signaled(self.dev_fd, e))

    def test_auto_reset_consumed_by_wait(self):
        """Auto-reset event: wait consumes the signal (only one waiter wakes)."""
        e = self.track(create_event(self.dev_fd, manual=False, signaled=True))
        # First wait succeeds
        idx = wait_any(self.dev_fd, [e], 1)
        self.assertEqual(idx, 0)
        # Second wait times out (signal consumed)
        with self.assertRaises(OSError) as ctx:
            wait_any(self.dev_fd, [e], 1)
        self.assertEqual(ctx.exception.errno, errno.ETIMEDOUT)

    def test_manual_not_consumed_by_wait(self):
        """Manual-reset event: wait does NOT consume the signal."""
        e = self.track(create_event(self.dev_fd, manual=True, signaled=True))
        idx = wait_any(self.dev_fd, [e], 1)
        self.assertEqual(idx, 0)
        # Still signaled
        idx = wait_any(self.dev_fd, [e], 1)
        self.assertEqual(idx, 0)

    def test_dup_shares_state(self):
        """dup'd fd points to same kernel object — set on one, poll on other."""
        e1 = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        e2 = self.track(os.dup(e1))
        event_set(e1)
        self.assertTrue(poll_signaled(self.dev_fd, e2))
        event_reset(e2)
        self.assertFalse(poll_signaled(self.dev_fd, e1))


class TestSemaphores(NtsyncTestCase):
    """Test ntsync semaphore create/release/wait."""

    def test_create_zero_count(self):
        s = self.track(create_sem(self.dev_fd, 0, 10))
        self.assertGreater(s, 0)
        self.assertFalse(poll_signaled(self.dev_fd, s))

    def test_create_nonzero_count(self):
        s = self.track(create_sem(self.dev_fd, 3, 10))
        self.assertTrue(poll_signaled(self.dev_fd, s))

    def test_release_increments(self):
        s = self.track(create_sem(self.dev_fd, 0, 10))
        prev = sem_release(s, 1)
        self.assertEqual(prev, 0)  # was 0
        self.assertTrue(poll_signaled(self.dev_fd, s))

    def test_release_returns_previous(self):
        s = self.track(create_sem(self.dev_fd, 5, 10))
        prev = sem_release(s, 2)
        self.assertEqual(prev, 5)  # was 5, now 7

    def test_release_over_max_fails(self):
        s = self.track(create_sem(self.dev_fd, 9, 10))
        with self.assertRaises(OSError):
            sem_release(s, 5)  # 9+5=14 > max=10

    def test_wait_decrements(self):
        s = self.track(create_sem(self.dev_fd, 2, 10))
        # Each wait consumes one count
        wait_any(self.dev_fd, [s], 1)
        wait_any(self.dev_fd, [s], 1)
        # Now count=0, should timeout
        with self.assertRaises(OSError) as ctx:
            wait_any(self.dev_fd, [s], 1)
        self.assertEqual(ctx.exception.errno, errno.ETIMEDOUT)

    def test_release_then_wait_cycle(self):
        s = self.track(create_sem(self.dev_fd, 0, 100))
        for i in range(10):
            sem_release(s, 1)
            idx = wait_any(self.dev_fd, [s], 1)
            self.assertEqual(idx, 0)


class TestMutexes(NtsyncTestCase):
    """Test ntsync mutex create/unlock/wait."""

    def test_create_unowned(self):
        m = self.track(create_mutex(self.dev_fd, owner=0, count=0))
        self.assertGreater(m, 0)
        # Unowned mutex is signaled (available)
        self.assertTrue(poll_signaled(self.dev_fd, m))

    def test_create_owned(self):
        m = self.track(create_mutex(self.dev_fd, owner=42, count=1))
        # Owned mutex is unsignaled for non-owner
        self.assertFalse(poll_signaled(self.dev_fd, m))

    def test_owned_mutex_available_to_owner(self):
        """Owner can acquire owned mutex (recursive)."""
        m = self.track(create_mutex(self.dev_fd, owner=42, count=1))
        # owner=42 should be able to wait (recursive acquisition)
        idx = wait_any(self.dev_fd, [m], 1, owner=42)
        self.assertEqual(idx, 0)

    def test_unlock(self):
        m = self.track(create_mutex(self.dev_fd, owner=42, count=1))
        prev = mutex_unlock(m, owner=42)
        self.assertEqual(prev, 1)  # was recursion count 1
        # Now unowned — anyone can acquire
        self.assertTrue(poll_signaled(self.dev_fd, m))

    def test_unlock_wrong_owner_fails(self):
        m = self.track(create_mutex(self.dev_fd, owner=42, count=1))
        with self.assertRaises(OSError):
            mutex_unlock(m, owner=99)  # not the owner

    def test_recursive_lock(self):
        m = self.track(create_mutex(self.dev_fd, owner=42, count=1))
        # Recursive acquire by same owner
        wait_any(self.dev_fd, [m], 1, owner=42)  # count → 2
        wait_any(self.dev_fd, [m], 1, owner=42)  # count → 3
        # Need 3 unlocks to release
        mutex_unlock(m, owner=42)  # count → 2
        mutex_unlock(m, owner=42)  # count → 1
        self.assertFalse(poll_signaled(self.dev_fd, m))  # still owned
        mutex_unlock(m, owner=42)  # count → 0, released
        self.assertTrue(poll_signaled(self.dev_fd, m))


class TestWaitAny(NtsyncTestCase):
    """Test NTSYNC_IOC_WAIT_ANY with multiple objects."""

    def test_timeout_no_signal(self):
        e = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        with self.assertRaises(OSError) as ctx:
            wait_any(self.dev_fd, [e], 1)
        self.assertEqual(ctx.exception.errno, errno.ETIMEDOUT)

    def test_first_signaled(self):
        e1 = self.track(create_event(self.dev_fd, manual=True, signaled=True))
        e2 = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        idx = wait_any(self.dev_fd, [e1, e2], 1)
        self.assertEqual(idx, 0)

    def test_second_signaled(self):
        e1 = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        e2 = self.track(create_event(self.dev_fd, manual=True, signaled=True))
        idx = wait_any(self.dev_fd, [e1, e2], 1)
        self.assertEqual(idx, 1)

    def test_both_signaled_returns_lowest(self):
        e1 = self.track(create_event(self.dev_fd, manual=True, signaled=True))
        e2 = self.track(create_event(self.dev_fd, manual=True, signaled=True))
        idx = wait_any(self.dev_fd, [e1, e2], 1)
        self.assertEqual(idx, 0)

    def test_mixed_types(self):
        """Wait on event + semaphore together."""
        e = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        s = self.track(create_sem(self.dev_fd, 1, 10))
        idx = wait_any(self.dev_fd, [e, s], 1)
        self.assertEqual(idx, 1)  # semaphore is signaled

    def test_signal_from_another_thread(self):
        """Blocking wait woken by set from another thread."""
        e = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        result = [None]

        def waiter():
            deadline = monotonic_ns() + 2_000_000_000  # 2s
            try:
                result[0] = wait_any(self.dev_fd, [e], deadline)
            except OSError as ex:
                result[0] = ex

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)
        event_set(e)
        t.join(timeout=2)
        self.assertFalse(t.is_alive())
        self.assertEqual(result[0], 0)


class TestWaitAll(NtsyncTestCase):
    """Test NTSYNC_IOC_WAIT_ALL with multiple objects."""

    def test_all_signaled(self):
        e1 = self.track(create_event(self.dev_fd, manual=True, signaled=True))
        e2 = self.track(create_event(self.dev_fd, manual=True, signaled=True))
        idx = wait_all(self.dev_fd, [e1, e2], 1)
        self.assertEqual(idx, 0)

    def test_one_unsignaled_times_out(self):
        e1 = self.track(create_event(self.dev_fd, manual=True, signaled=True))
        e2 = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        with self.assertRaises(OSError) as ctx:
            wait_all(self.dev_fd, [e1, e2], 1)
        self.assertEqual(ctx.exception.errno, errno.ETIMEDOUT)


class TestAlert(NtsyncTestCase):
    """Test alert mechanism (cancelling waits via alert event)."""

    def test_alert_cancels_wait(self):
        """Setting alert event cancels a blocking wait with ECANCELED."""
        e = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        alert = self.track(create_event(self.dev_fd, manual=False, signaled=False))
        result = [None]
        err = [None]

        def waiter():
            deadline = monotonic_ns() + 5_000_000_000  # 5s
            try:
                idx = wait_any(self.dev_fd, [e], deadline, alert_fd=alert)
                result[0] = idx
            except OSError as ex:
                err[0] = ex.errno

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)  # give thread time to enter kernel wait
        event_set(alert)
        t.join(timeout=2)
        self.assertFalse(t.is_alive())
        # Alert can manifest as either ECANCELED or index==count (1, past the object array)
        if err[0] is not None:
            self.assertEqual(err[0], errno.ECANCELED)
        else:
            # ntsync returns index=count when alert fires
            self.assertEqual(result[0], 1)

    def test_alert_not_triggered(self):
        """If alert is not set, normal wait proceeds."""
        e = self.track(create_event(self.dev_fd, manual=True, signaled=True))
        alert = self.track(create_event(self.dev_fd, manual=False, signaled=False))
        idx = wait_any(self.dev_fd, [e], 1, alert_fd=alert)
        self.assertEqual(idx, 0)


class TestThreadExitPattern(NtsyncTestCase):
    """Test the exact pattern triskelion uses for thread exit signaling.

    This reproduces the core lifecycle:
    1. Create manual-reset unsignaled event (thread exit event)
    2. dup() it — one copy in ntsync_objects, one in thread_exit_events
    3. Main thread waits on the ntsync_objects copy
    4. Worker thread "exits" — server signals the thread_exit_events copy
    5. Main thread's wait should wake up
    """

    def test_dup_signal_wakes_waiter(self):
        """Signal on dup'd fd wakes waiter on original fd."""
        original = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        duped = self.track(os.dup(original))

        result = [None]

        def waiter():
            deadline = monotonic_ns() + 2_000_000_000
            try:
                result[0] = wait_any(self.dev_fd, [original], deadline)
            except OSError as ex:
                result[0] = ex

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)
        # Signal on the DUP'd copy (simulates disconnect_client calling event_set)
        event_set(duped)
        t.join(timeout=2)
        self.assertFalse(t.is_alive(), "Waiter thread should have woken up")
        self.assertEqual(result[0], 0)

    def test_close_original_dup_still_works(self):
        """Closing original fd doesn't affect dup'd fd's kernel object."""
        original = create_event(self.dev_fd, manual=True, signaled=False)
        duped = self.track(os.dup(original))
        os.close(original)  # close original — dup should still work

        event_set(duped)
        self.assertTrue(poll_signaled(self.dev_fd, duped))

    def test_full_thread_exit_lifecycle(self):
        """Full triskelion thread exit flow:
        1. new_thread: create event, dup, store both
        2. get_inproc_sync_fd: dup original, send to "client"
        3. client waits on its copy via kernel ioctl
        4. disconnect: signal exit_events copy
        5. client wakes
        """
        # Step 1: new_thread creates event + dup
        handle_obj = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        exit_obj = self.track(os.dup(handle_obj))

        # Step 2: get_inproc_sync_fd dups handle_obj for client
        client_fd = self.track(os.dup(handle_obj))

        # Step 3: client waits on its copy
        result = [None]

        def client_wait():
            deadline = monotonic_ns() + 3_000_000_000
            try:
                result[0] = wait_any(self.dev_fd, [client_fd], deadline)
            except OSError as ex:
                result[0] = ex

        t = threading.Thread(target=client_wait)
        t.start()
        time.sleep(0.05)

        # Step 4: disconnect_client signals exit_obj (the dup'd copy)
        event_set(exit_obj)

        # Step 5: client should wake
        t.join(timeout=2)
        self.assertFalse(t.is_alive(), "Client wait should have been woken by exit event")
        self.assertEqual(result[0], 0)


class TestFreelist(NtsyncTestCase):
    """Test the freelist recycling pattern used by triskelion."""

    def test_reset_then_reuse(self):
        """Freelist pattern: reset event, reuse it."""
        e = self.track(create_event(self.dev_fd, manual=True, signaled=True))
        self.assertTrue(poll_signaled(self.dev_fd, e))

        # Simulate freelist: reset before storing
        event_reset(e)
        self.assertFalse(poll_signaled(self.dev_fd, e))

        # Reuse: set for new purpose
        event_set(e)
        self.assertTrue(poll_signaled(self.dev_fd, e))

    def test_signaled_fallback_pattern(self):
        """get_or_create_event(manual=true, signaled=true) pattern.
        Freelist returns unsignaled event, then caller sets it.
        """
        e = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        # Simulate freelist return (unsignaled)
        self.assertFalse(poll_signaled(self.dev_fd, e))
        # Caller wants signaled — must call event_set
        event_set(e)
        self.assertTrue(poll_signaled(self.dev_fd, e))

    def test_many_create_close_cycle(self):
        """Stress: create and close many events without leaking."""
        for _ in range(500):
            e = create_event(self.dev_fd, manual=True, signaled=False)
            event_set(e)
            event_reset(e)
            os.close(e)


class TestEdgeCases(NtsyncTestCase):
    """Edge cases and error paths."""

    def test_wait_empty_list(self):
        """Wait with zero objects — should fail immediately."""
        with self.assertRaises(OSError):
            wait_any(self.dev_fd, [], 1)

    def test_set_on_semaphore_fails(self):
        """event_set on a semaphore fd should fail (wrong object type)."""
        s = self.track(create_sem(self.dev_fd, 0, 10))
        with self.assertRaises(OSError):
            event_set(s)

    def test_sem_release_on_event_fails(self):
        """sem_release on an event fd should fail."""
        e = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        with self.assertRaises(OSError):
            sem_release(e, 1)

    def test_mutex_unlock_on_event_fails(self):
        """mutex_unlock on an event fd should fail."""
        e = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        with self.assertRaises(OSError):
            mutex_unlock(e, owner=1)

    def test_wait_with_invalid_fd(self):
        """Wait with a bogus fd should fail."""
        with self.assertRaises(OSError):
            wait_any(self.dev_fd, [99999], 1)

    def test_concurrent_set_and_wait(self):
        """Hammer set/wait from multiple threads."""
        e = self.track(create_event(self.dev_fd, manual=True, signaled=False))
        errors = []

        def setter():
            for _ in range(100):
                try:
                    event_set(e)
                    event_reset(e)
                except OSError as ex:
                    errors.append(ex)

        def waiter():
            for _ in range(100):
                try:
                    wait_any(self.dev_fd, [e], 1)
                except OSError:
                    pass  # timeout is fine

        threads = [threading.Thread(target=setter) for _ in range(4)]
        threads += [threading.Thread(target=waiter) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(len(errors), 0, f"Got errors during concurrent access: {errors}")


if __name__ == "__main__":
    unittest.main()

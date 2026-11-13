"""Bound host NPC-principal reads even under repeated receive timeout callbacks.

No network socket, external credentials or real DB changes. The fake handler
runs the actual production _line method with an adversarial recv schedule.
"""
from __future__ import annotations

import socket
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.telnet import LocalTelnetHandler


class _BusyTimeoutSocket:
    def __init__(self):
        self.reads = 0

    def recv(self, _size):
        self.reads += 1
        if self.reads <= 4:
            raise socket.timeout("fake rapid timeout")
        return b""


class RoleLookupBudgetTest(unittest.TestCase):
    def test_repeated_rapid_socket_timeouts_do_not_multiply_role_reads(self):
        calls = []

        def current_role(name):
            self.assertEqual(name, "Moss")
            calls.append(time.monotonic())
            return False

        sock = _BusyTimeoutSocket()
        fake_handler = SimpleNamespace(
            request=sock,
            server=SimpleNamespace(is_reserved_name=current_role),
            _pending=bytearray(), _claimed_name="Moss",
            _last_input_monotonic=time.monotonic(),
            _last_reservation_check=time.monotonic()-2,
            _presence_lease=None,
        )
        result = LocalTelnetHandler._line(fake_handler)
        self.assertIsNone(result)
        self.assertEqual(sock.reads, 5)
        # All four timeout callbacks and the first read occur far below one
        # second. The host role store must be consulted no more than once.
        self.assertLessEqual(len(calls), 1,
                             "Rapid timeout wakeups bypass the one-per-second role-read budget")


if __name__ == "__main__":
    unittest.main()

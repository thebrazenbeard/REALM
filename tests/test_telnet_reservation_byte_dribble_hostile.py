"""Falsify periodic revocation when client keeps recv() non-idle with partial bytes.

No production state, no actual account tokens, loopback ephemeral TCP only.
"""
from __future__ import annotations
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.gateway import Gateway
from realm.principal_roles import reserve_npc
from realm.telnet import LocalTelnetServer


class PartialLineRevocationTest(unittest.TestCase):
    def test_reserved_principal_ejected_despite_partial_line_byte_dribble(self):
        with tempfile.TemporaryDirectory() as t:
            gw = Gateway(Path(t) / "game.sqlite3", world_id="local_npc_guard")
            gw.join("Moss")
            srv = LocalTelnetServer(gw.engine, "isolated-secret", port=0)
            thread = threading.Thread(target=srv.serve_forever,
                                      kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            try:
                with socket.create_connection(srv.server_address, timeout=2) as c:
                    c.settimeout(2)
                    def until(marker):
                        received=bytearray()
                        while marker not in received:
                            chunk=c.recv(4096)
                            if not chunk: raise AssertionError("closed before marker")
                            received.extend(chunk)
                    until(b"Token: ")
                    c.sendall(b"isolated-secret\r\n")
                    until(b"Name: ")
                    c.sendall(b"Moss\r\n")
                    until(b"REALM> ")
                    self.assertTrue(srv.is_name_active("Moss"))
                    self.assertTrue(reserve_npc(gw.engine,"Moss"))
                    # Bytes arrive frequently enough to suppress the 1s recv
                    # timeout. Never send a complete gameplay command.
                    for _ in range(10):
                        try:
                            c.sendall(b"x")
                        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                            # A revoked socket may close before next write.
                            break
                        time.sleep(0.25)
                    # Expected security goal: role check is bounded by wall
                    # time, not entirely dependent on socket timeout.
                    self.assertFalse(
                        srv.is_name_active("Moss"),
                        "reserved NPC still owns a Telnet socket after 2.5s of partial input"
                    )
            finally:
                srv.shutdown()
                srv.server_close()
                thread.join(3)


if __name__ == "__main__":
    unittest.main()

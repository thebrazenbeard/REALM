"""Red acceptance for RFC 854/855 escaped-IAC handling inside SB frames.

An IAC IAC pair encodes literal data byte 255. A following *bare* SE (240)
cannot terminate subnegotiation; only IAC SE may. Until the actual terminator,
all content must remain out of the line-command body. These tests exercise
only the local parser, not a socket, token gate, player identity, or server.
"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.telnet import IAC, SB, SE, _TelnetFilter


class TelnetEscapedSubnegotiationAcceptanceTests(unittest.TestCase):
    def test_doubled_iac_followed_by_bare_se_must_not_expose_command_bytes(self):
        parser = _TelnetFilter()
        # IAC SB option 1, escaped data IAC IAC, bare SE is *still data*.
        deceptive = bytes([IAC, SB, 1, IAC, IAC, SE]) + b"say FORGED_INSIDE_SB"
        body, reply = parser.feed(deceptive)
        self.assertEqual(b"", body, "Subnegotiation body escaped into a REALM player command")
        self.assertEqual(b"", reply)
        body, _ = parser.feed(bytes([IAC, SE]) + b"normal outside text")
        self.assertEqual(b"normal outside text", body)

    def test_escaped_iac_split_at_every_tcp_boundary_cannot_close_sb(self):
        full = bytes([IAC, SB, 7, IAC, IAC, SE]) + b"take PRIVATE_ITEM"
        for split in range(1, len(full)):
            parser = _TelnetFilter()
            first, _ = parser.feed(full[:split])
            second, _ = parser.feed(full[split:])
            self.assertEqual(
                b"", first + second,
                f"Escaped IAC false termination leaked command at split {split}",
            )
            after, _ = parser.feed(bytes([IAC, SE]) + b"look")
            self.assertEqual(b"look", after)


if __name__ == "__main__":
    unittest.main()

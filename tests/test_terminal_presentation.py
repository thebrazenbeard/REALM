"""Generic terminal rendering and opt-in loopback Telnet color controls."""
from __future__ import annotations

import socket
import sqlite3
from contextlib import closing
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.engine import Engine
from realm.telnet import LocalTelnetServer
from realm.terminal_presentation import render_terminal, palette_preview


class TerminalPaletteTests(unittest.TestCase):
    def test_colors_only_room_prose_not_player_chat_or_item_values(self):
        view = ("The Landing\nCopper coils blink amber under a light grey lamp. "
                "Black dust covers a coppery label.\n"
                "Exits: north, east\nItems: Copper token")
        colored = render_terminal(view, ansi=True, room=True)
        self.assertIn("\x1b[1;36mThe Landing\x1b[0m", colored)
        self.assertIn("\x1b[38;2;184;115;51mCopper\x1b[0m", colored)
        self.assertIn("\x1b[38;2;255;191;0mamber\x1b[0m", colored)
        self.assertIn("\x1b[38;2;211;211;211mlight grey\x1b[0m", colored)
        self.assertIn("\x1b[30;47mBlack\x1b[0m", colored)
        self.assertIn("coppery", colored)
        self.assertIn("\x1b[37mExits:\x1b[0m \x1b[1;33mnorth, east\x1b[0m", colored)
        self.assertIn("\x1b[37mItems:\x1b[0m \x1b[1;32mCopper token\x1b[0m", colored)
        self.assertNotIn("\x1b[38;2;184;115;51mCopper token", colored)
        chat = render_terminal("You say: amber light grey black", ansi=True)
        self.assertIn("\x1b[1;35mamber light grey black\x1b[0m", chat)
        self.assertNotIn("\x1b[38;2;255;191;0m", chat)
        self.assertEqual(view, render_terminal(view, ansi=False, room=True))

    def test_color_aliases_and_forged_ansi_are_inert(self):
        view = ("Antechamber\nA light-gray panel faces dark grey rails; "
                "pink and cyan lamps, cyanide and coppery. "
                "\x1b]52;c;SECRETS\x07\x1b[31m\nExits: west")
        colored = render_terminal(view, ansi=True, room=True)
        self.assertIn("\x1b[38;2;211;211;211mlight-gray\x1b[0m", colored)
        self.assertIn("\x1b[38;2;144;144;144mdark grey\x1b[0m", colored)
        self.assertIn("\x1b[38;2;255;105;180mpink\x1b[0m", colored)
        self.assertIn("\x1b[38;2;0;255;255mcyan\x1b[0m", colored)
        self.assertIn("cyanide", colored)
        self.assertNotIn("\x1b]52;", colored)
        self.assertNotIn("\x07", colored)
        self.assertNotIn("\x1b[31m", colored)
        self.assertEqual(1, colored.count("\x1b[1;36m"))

    def test_material_phrases_highlight_only_grounded_objects(self):
        view = ("The Workshop\n"
                "A wooden table stands by a steel rack and a bare table. "
                "A table of steel supports a crate made of oak.\nExits: south")
        shown = render_terminal(view, ansi=True, room=True)
        self.assertIn("\x1b[38;2;159;103;58mwooden table\x1b[0m", shown)
        self.assertIn("\x1b[38;2;157;173;185msteel rack\x1b[0m", shown)
        self.assertIn("\x1b[38;2;157;173;185mtable of steel\x1b[0m", shown)
        self.assertIn("\x1b[38;2;159;103;58mcrate made of oak\x1b[0m", shown)
        self.assertIn("a bare table", shown)
        self.assertNotIn("\x1b[38;2;159;103;58mbare table", shown)

    def test_lighting_cues_and_scope_are_grounded(self):
        view = ("Observation Deck\n"
                "Faint floor lights illuminate the path. Bright overhead lights "
                "shine near a brilliant white beacon; one lamp is off.\n"
                "Exits: west\nYou say: faint floor lights")
        shown = render_terminal(view, ansi=True, room=True)
        self.assertIn("\x1b[38;2;154;129;54mFaint floor lights\x1b[0m", shown)
        self.assertIn("\x1b[38;2;255;248;215mBright overhead lights\x1b[0m", shown)
        self.assertIn("\x1b[38;2;255;248;215mbrilliant white beacon\x1b[0m", shown)
        self.assertIn("one lamp is off", shown)
        self.assertIn("\x1b[1;35mfaint floor lights\x1b[0m", shown)
        self.assertNotIn("\x1b[38;2;154;129;54mfaint floor lights", shown)

    def test_ambiguous_disjunction_does_not_choose_material_or_light(self):
        view = ("The Lobby\n"
                "A wooden or steel table is rumored to be here. Its bulbs might "
                "be dim or bright lights depending on the hour.\nExits: north")
        shown = render_terminal(view, ansi=True, room=True)
        self.assertIn("wooden or steel table", shown)
        self.assertIn("dim or bright lights", shown)
        self.assertNotIn("\x1b[38;2;157;173;185msteel table", shown)
        self.assertNotIn("\x1b[38;2;255;248;215mbright lights", shown)

    def test_palette_preview_is_opt_in_and_reset_delimited(self):
        self.assertIn("Classic:", palette_preview(ansi=False))
        self.assertNotIn("\x1b[", palette_preview(ansi=False))
        self.assertIn("\x1b[38;2;0;255;255mcyan\x1b[0m", palette_preview(ansi=True))
        self.assertIn("\x1b[30;47mblack\x1b[0m", palette_preview(ansi=True))


class TelnetPaletteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = Engine(Path(self.tmp.name) / "world.sqlite3")
        self.host = LocalTelnetServer(self.engine, "color-secret", 0)
        self.thread = threading.Thread(target=self.host.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_host)

    def stop_host(self):
        self.host.shutdown()
        self.host.server_close()
        self.thread.join(timeout=2)

    def until(self, sock, marker):
        data = bytearray()
        for _ in range(30):
            data.extend(sock.recv(4096))
            if marker in data:
                return data.decode("utf-8", errors="replace")
        self.fail("Missing terminal marker")

    def test_unauthenticated_sender_cannot_access_palette_and_commands_are_read_only(self):
        sock = socket.create_connection(self.host.server_address, timeout=3)
        sock.settimeout(3)
        self.addCleanup(sock.close)
        self.until(sock, b"Token: ")
        sock.sendall(b"color on\r\n")
        self.assertIn("Access denied", self.until(sock, b"Access denied"))
        sock.close()

        sock = socket.create_connection(self.host.server_address, timeout=3)
        sock.settimeout(3)
        self.addCleanup(sock.close)
        self.until(sock, b"Token: ")
        sock.sendall(b"color-secret\r\n")
        self.until(sock, b"Name: ")
        sock.sendall(b"Explorer\r\n")
        initial = self.until(sock, b"REALM> ")
        self.assertNotIn("\x1b[", initial)
        with closing(sqlite3.connect(self.engine.db_path)) as db:
            baseline = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        sock.sendall(b"color on\r\n")
        self.assertIn("ANSI colors enabled", self.until(sock, b"REALM> "))
        sock.sendall(b"look\r\n")
        room = self.until(sock, b"REALM> ")
        self.assertIn("\x1b[1;36mThe Atrium\x1b[0m", room)
        self.assertIn("\x1b[37mExits:\x1b[0m", room)
        sock.sendall(b"palette\r\n")
        self.assertIn("\x1b[38;2;0;255;255mcyan\x1b[0m", self.until(sock, b"REALM> "))
        sock.sendall(b"colour off\r\n")
        self.assertIn("ANSI colors disabled", self.until(sock, b"REALM> "))
        sock.sendall(b"look\r\n")
        self.assertNotIn("\x1b[", self.until(sock, b"REALM> "))
        with closing(sqlite3.connect(self.engine.db_path)) as db:
            after = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(baseline, after)
        sock.sendall(b"quit\r\n")
        self.assertIn("position is saved", self.until(sock, b"position is saved"))
        sock.close()
        for _ in range(100):
            if not self.host.is_name_active("Explorer"):
                break
            threading.Event().wait(0.01)
        self.assertFalse(self.host.is_name_active("Explorer"))


if __name__ == "__main__":
    unittest.main()

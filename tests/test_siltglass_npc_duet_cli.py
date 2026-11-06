"""Model-free safety and persistence tests for the opt-in CPU GGUF CLI."""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import run_siltglass_npc_duet as demo


class SiltglassModelAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.cli = self.work / "llama-completion.exe"
        self.cli.write_bytes(b"fake test executable")
        self.models = {
            "FerrySteward": self.work / "stealth-rifle-Q4_K_M.gguf",
            "CrossingInspector": self.work / "npc-voice-soup06.Q5_K_M.gguf",
        }

    def test_speech_rejects_thinking_template_multiline_and_oversize(self):
        for text in (
            "", "hi\nthere", "[Start thinking] maybe", "<think>reason</think>",
            "<|im_start|>assistant", "FACT: something", "OUT: testing",
            "a" * 281, "hi\x1b[31m", "hello\x00world",
            "[end of text] in a story", "hi [end of text] [end of text]",
            "Loading model...\nI am a steward.",
            "available commands:\nI am a steward.",
            "I am a steward.\n[ Prompt: 100 t/s ]",
            "> <|im_start|>system\nI am a steward.",
            "Hello\n[end of text]", "Hello\r\n[end of text]",
            "Hello\n\n[end of text]", "Hello\n [end of text]",
            "llama.cpp: context size 512 [end of text]",
            "main: seed = 123 [end of text]",
            "llama_print_timings: tokens 80 [end of text]",
            "Loading model... [end of text]",
            "Hello [end of text] [end of text]",
            "Hello [end of text]\nExiting...",
            "Hello", "Hello [end of text]\nOther line",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                demo.parse_model_speech(text)
        self.assertEqual(
            "Crossing not yet inspected.",
            demo.parse_model_speech(" Crossing not yet inspected. [end of text]\n\n"),
        )

    def test_prompt_explicitly_distinguishes_room_truth_from_prior_speech(self):
        from realm.conversations import DialogueContext
        context = DialogueContext(
            "CrossingInspector", "FerrySteward", "Measured inspector", "tidegate",
            "The Tidegate", "Four marks on the wall.",
            "The river will rise tomorrow.", 1, "trial",
        )
        prompt = demo._prompt(context)
        self.assertIn("Four marks on the wall", prompt)
        self.assertIn("without independent confirmation", prompt)
        self.assertIn("The river will rise tomorrow", prompt)
        self.assertIn("TONE:measured", prompt)

    def test_provider_uses_offline_cpu_and_soup_reasoning_off(self):
        from realm.conversations import DialogueContext
        context = DialogueContext(
            "CrossingInspector", "FerrySteward", "Measured inspector", "tidegate",
            "The Tidegate", "A public slate awaits reports.", "The road is open.", 1, "trial",
        )
        class FakeResult:
            returncode = 0
            stdout = "I need the ledger before deciding. [end of text]\n\n"
        with patch.object(demo.subprocess, "run", return_value=FakeResult()) as launch:
            result = demo.local_provider("CrossingInspector", self.models["CrossingInspector"], self.cli)(context)
        self.assertEqual({"command": "say I need the ledger before deciding."}, result)
        args = launch.call_args.args[0]
        self.assertIn("--offline", args)
        self.assertIn("-ngl", args)
        self.assertEqual("0", args[args.index("-ngl") + 1])
        self.assertIn("--no-conversation", args)
        self.assertNotIn("--single-turn", args)
        self.assertEqual("40", args[args.index("-n") + 1])
        self.assertEqual("off", args[args.index("--reasoning") + 1])
        self.assertEqual(60, launch.call_args.kwargs["timeout"])

    def test_two_distinct_model_proposals_are_committed_and_reopened(self):
        class FakeResult:
            returncode = 0

            def __init__(self, stdout):
                self.stdout = stdout

        utterances = ["The public slate asks us to report altered paths.",
                      "We must inspect the marks before allowing passage."]
        with patch.object(demo, "verify_artifact", side_effect=["a" * 64, "b" * 64]), \
             patch.object(demo.subprocess, "run", side_effect=[FakeResult(s + " [end of text]\n\n") for s in utterances]) as launch:
            db_path = self.work / "brand_new.sqlite3"
            result = demo.run_fresh_duet(db_path, self.cli, self.models, turns=2)
        self.assertEqual(2, launch.call_count)
        self.assertEqual("verified_local_duet_receipts", result["status"])
        self.assertEqual("siltglass-reach", result["world_id"])
        self.assertEqual(2, result["durable_event_count"])
        self.assertEqual(2, result["durable_receipt_count"])
        self.assertEqual(utterances, [item["speech"] for item in result["turns"]])
        with closing(sqlite3.connect(db_path)) as db:
            identities = db.execute("SELECT actor FROM npc_actor_registry ORDER BY actor").fetchall()
            events = db.execute("SELECT actor,payload_json FROM events WHERE kind='say' ORDER BY id").fetchall()
        self.assertEqual(2, len(identities))
        self.assertEqual("FerrySteward", events[0][0])
        self.assertEqual("CrossingInspector", events[1][0])
        self.assertEqual(utterances[1], json.loads(events[1][1])["text"])
        with self.assertRaises(FileExistsError):
            demo.run_fresh_duet(db_path, self.cli, self.models, turns=2)

    def test_invalid_model_output_leaves_no_false_success_receipt(self):
        class FakeResult:
            returncode = 0

            def __init__(self, stdout):
                self.stdout = stdout

        db_path = self.work / "failure_after_one.sqlite3"
        with patch.object(demo, "verify_artifact", return_value="c" * 64), \
             patch.object(demo.subprocess, "run", side_effect=[
                 FakeResult("We should report the unusual water marks. [end of text]\n"),
                 FakeResult("<think>Maybe the river is magic</think> [end of text]\n"),
             ]):
            with self.assertRaisesRegex(ValueError, "thinking"):
                demo.run_fresh_duet(db_path, self.cli, self.models, turns=2)
        with closing(sqlite3.connect(db_path)) as db:
            event_count = db.execute("SELECT COUNT(*) FROM events WHERE kind='say'").fetchone()[0]
            receipt_count = db.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0]
        self.assertEqual(1, event_count)
        self.assertEqual(1, receipt_count)


if __name__ == "__main__":
    unittest.main()

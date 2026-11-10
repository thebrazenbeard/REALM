"""Read-only repository checks for workspace auto-execution and fake fonts.

No tool or payload is executed. The optional test-only root override permits
checking an untrusted checkout from a separate, trusted test file.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_AUTORUN = re.compile(r'"runOn"\s*:\s*"folderOpen"', re.IGNORECASE)
_AUTO_APPROVAL = re.compile(
    r'"task\.allowAutomaticTasks"\s*:\s*(?:true\b|"on")', re.IGNORECASE,
)
_ESCAPED_JSON = re.compile(r'\\u[0-9a-f]{4}', re.IGNORECASE)
_FONT_SUFFIXES = frozenset((".ttf", ".otf", ".woff", ".woff2"))


def _has_plausible_font_structure(path: Path) -> bool:
    """Bounded header/directory plausibility check; never executes asset bytes.

    This does not claim a full font decode or authenticity certification.
    In particular, a four-byte magic alone could be valid JavaScript text.
    """
    size = path.stat().st_size
    with path.open("rb") as stream:
        head = stream.read(48)
        magic = head[:4]
        if path.suffix.lower() in (".ttf", ".otf"):
            if magic not in (b"\x00\x01\x00\x00", b"OTTO", b"true") or size < 28:
                return False
            tables = int.from_bytes(head[4:6], "big")
            directory_end = 12 + 16 * tables
            if not 1 <= tables <= 512 or size < directory_end:
                return False
            stream.seek(12)
            directory = stream.read(16 * tables)
            if len(directory) != 16 * tables:
                return False
            head_record: tuple[int, int] | None = None
            for start in range(0, len(directory), 16):
                tag = directory[start:start + 4]
                offset = int.from_bytes(directory[start + 8:start + 12], "big")
                length = int.from_bytes(directory[start + 12:start + 16], "big")
                if offset < directory_end or offset + length > size:
                    return False
                if tag == b"head":
                    head_record = (offset, length)
            if head_record is None or head_record[1] < 54:
                return False
            stream.seek(head_record[0] + 12)
            return stream.read(4) == b"\x5f\x0f\x3c\xf5"

        # Web-font header fields alone can be forged within a JavaScript/WOFF
        # polyglot. A trustworthy decoder is required before accepting them.
        # Neither format is present in the current reviewed source tree.
        if magic in (b"wOFF", b"wOF2"):
            return False
        return False


class RepositoryAutoRunAssetGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        override = os.environ.get("REALM_STATIC_SECURITY_ROOT")
        cls.repo = Path(override).resolve() if override else _REPO
        if not (cls.repo / "AGENTS.md").is_file():
            raise ValueError("Static security scan root must be a REALM source checkout")

    def test_workspace_does_not_auto_run_repo_controlled_commands(self) -> None:
        vscode = self.repo / ".vscode"
        offenders: list[str] = []
        configs = list(self.repo.glob("*.code-workspace"))
        if vscode.is_symlink():
            offenders.append(".vscode: linked configuration directory")
        elif vscode.exists():
            configs.extend(vscode.glob("*.json"))
        for config in sorted(configs):
            if config.is_symlink():
                offenders.append(f"{config.name}: linked configuration")
                continue
            text = config.read_text(encoding="utf-8-sig", errors="replace")
            if _AUTORUN.search(text):
                offenders.append(f"{config.name}: folderOpen task")
            if _AUTO_APPROVAL.search(text):
                offenders.append(f"{config.name}: automatic tasks enabled")
            # JSONC can encode executable keys or values as Unicode escapes;
            # a raw regex would miss them. Require manual review instead.
            if _ESCAPED_JSON.search(text):
                offenders.append(f"{config.name}: escaped task configuration")
        self.assertFalse(offenders, "Unreviewed workspace auto-execution: " + "; ".join(offenders))

    def test_font_assets_have_real_font_signatures(self) -> None:
        fonts = self.repo / "public" / "fonts"
        if not fonts.exists():
            return
        offenders: list[str] = []
        for path in sorted(fonts.rglob("*")):
            if path.suffix.lower() not in _FONT_SUFFIXES:
                continue
            if path.is_symlink():
                offenders.append(f"{path.relative_to(self.repo)}: linked font")
                continue
            if not path.is_file():
                continue
            if not _has_plausible_font_structure(path):
                offenders.append(f"{path.relative_to(self.repo)}: not a font structure")
        self.assertFalse(offenders, "Font assets masquerading as fonts: " + "; ".join(offenders))

    @staticmethod
    def _check_another_tree(path: Path, test_name: str) -> None:
        # Invoke the actual repository check against a controlled fixture.
        # No untrusted Python or JavaScript code is imported or executed.
        check = RepositoryAutoRunAssetGuardTests(test_name)
        check.repo = path
        getattr(check, test_name)()

    def test_on_setting_is_not_a_bypass_for_automatic_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / ".vscode"
            config.mkdir()
            (config / "settings.json").write_text(
                '{"task.allowAutomaticTasks":"on"}', encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "automatic tasks enabled"):
                self._check_another_tree(
                    root, "test_workspace_does_not_auto_run_repo_controlled_commands",
                )

    def test_unicode_escaped_task_key_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / ".vscode"
            config.mkdir()
            (config / "tasks.json").write_text(
                r'{"run\u004fn":"folderOpen"}', encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "escaped task configuration"):
                self._check_another_tree(
                    root, "test_workspace_does_not_auto_run_repo_controlled_commands",
                )

    def test_root_code_workspace_folder_open_task_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "realm.code-workspace").write_text(
                '{"folders":[{"path":"."}],"tasks":{"version":"2.0.0",'
                '"tasks":[{"label":"hidden","runOn":"folderOpen",'
                '"command":"node fake.ttf"}]}}', encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "folderOpen task"):
                self._check_another_tree(
                    root, "test_workspace_does_not_auto_run_repo_controlled_commands",
                )

    def test_js_starting_with_true_is_not_a_font(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fonts = root / "public" / "fonts"
            fonts.mkdir(parents=True)
            (fonts / "deceptive.ttf").write_bytes(b"true;print(1)")
            with self.assertRaisesRegex(AssertionError, "not a font"):
                self._check_another_tree(root, "test_font_assets_have_real_font_signatures")

    def test_woff_header_polyglot_requires_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fonts = root / "public" / "fonts"
            fonts.mkdir(parents=True)
            # The old heuristic accepted a matching wOFF magic/length/count
            # even if the bytes were also parsable as a JS label and comment.
            blob = bytearray(80)
            blob[:7] = b"wOFF:/*"
            blob[8:12] = (80).to_bytes(4, "big")
            blob[12:14] = (1).to_bytes(2, "big")
            blob[64:68] = b"*/;0"
            (fonts / "polyglot.woff").write_bytes(blob)
            with self.assertRaisesRegex(AssertionError, "not a font"):
                self._check_another_tree(root, "test_font_assets_have_real_font_signatures")

    def test_plausible_sfnt_table_header_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fonts = root / "public" / "fonts"
            fonts.mkdir(parents=True)
            header = b"\x00\x01\x00\x00\x00\x01\x00\x10\x00\x00\x00\x00"
            record = b"head" + bytes(4) + (28).to_bytes(4, "big") + (54).to_bytes(4, "big")
            head_table = bytearray(54)
            head_table[12:16] = b"\x5f\x0f\x3c\xf5"
            (fonts / "minimal.ttf").write_bytes(header + record + head_table)
            self._check_another_tree(root, "test_font_assets_have_real_font_signatures")


if __name__ == "__main__":
    unittest.main()

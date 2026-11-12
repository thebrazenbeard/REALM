"""Read-only repository checks for workspace auto-execution and fake fonts.

No tool or payload is executed. The optional test-only root override permits
checking an untrusted checkout from a separate, trusted test file.
"""

from __future__ import annotations

import json
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
_WORKSPACE_FOLDER_PATH = re.compile(r'"path"\s*:\s*"((?:\\.|[^"\\])*)"', re.IGNORECASE)
_WORKSPACE_FOLDER_URI = re.compile(r'"uri"\s*:', re.IGNORECASE)
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
        offenders: list[str] = []
        configs: list[Path] = []
        # Multi-root *.code-workspace files can open nested repository folders,
        # whose own .vscode/tasks.json may auto-run. Scan both representations
        # throughout the checkout, including Windows case variants. pathlib's
        # rglob does not follow directory symlinks; flag linked config folders.
        for path in sorted(self.repo.rglob("*")):
            if ".git" in (part.lower() for part in path.relative_to(self.repo).parts):
                continue
            if path.name.lower() == ".vscode":
                if path.is_symlink():
                    offenders.append(
                        f"{path.relative_to(self.repo).as_posix()}: linked configuration directory"
                    )
                elif path.is_dir():
                    configs.extend(sorted(
                        child for child in path.iterdir()
                        if child.name.lower().endswith(".json")
                    ))
            elif path.name.lower().endswith(".code-workspace"):
                configs.append(path)
        for config in configs:
            label = config.relative_to(self.repo).as_posix()
            if config.is_symlink():
                offenders.append(f"{label}: linked configuration")
                continue
            if not config.is_file():
                offenders.append(f"{label}: not a regular configuration file")
                continue
            # A replacement decoder can hide executable keys in UTF-16 or
            # malformed byte sequences. JSONC config must be valid UTF-8 for
            # this static scan; unknown encodings require manual review.
            try:
                text = config.read_text(encoding="utf-8-sig")
            except (UnicodeError, OSError):
                offenders.append(f"{label}: unsupported configuration encoding")
                continue
            if "\x00" in text:
                offenders.append(f"{label}: unsupported configuration encoding")
                continue
            if _AUTORUN.search(text):
                offenders.append(f"{label}: folderOpen task")
            if _AUTO_APPROVAL.search(text):
                offenders.append(f"{label}: automatic tasks enabled")
            # JSONC can encode executable keys or values as Unicode escapes;
            # a raw regex would miss them. Require manual review instead.
            if _ESCAPED_JSON.search(text):
                offenders.append(f"{label}: escaped task configuration")
            if config.name.lower().endswith(".code-workspace"):
                # A multi-root workspace can reference a sibling outside the
                # inspected checkout or cross a symlink into unscanned tasks.
                # Review those folders instead of following their paths.
                if _WORKSPACE_FOLDER_URI.search(text):
                    offenders.append(f"{label}: unreviewed workspace URI")
                for match in _WORKSPACE_FOLDER_PATH.finditer(text):
                    try:
                        name = json.loads('"' + match.group(1) + '"')
                    except json.JSONDecodeError:
                        offenders.append(f"{label}: unparseable workspace folder")
                        continue
                    normalized = name.replace("\\", "/")
                    parts = normalized.split("/")
                    if (normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized)
                            or ".." in parts or "${" in normalized):
                        offenders.append(f"{label}: external workspace folder")
                        continue
                    # Relative multi-root paths are resolved from the
                    # workspace file's directory, not the repository root.
                    folder = config.parent
                    for part in parts:
                        if not part or part == ".":
                            continue
                        folder = folder / part
                        if folder.is_symlink():
                            offenders.append(f"{label}: linked workspace folder")
                            break
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

    def test_utf16_auto_run_configuration_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            vscode = root / ".vscode"
            vscode.mkdir()
            (vscode / "tasks.json").write_bytes(
                '{"tasks":[{"runOptions":{"runOn":"folderOpen"}}]}'.encode("utf-16"),
            )
            with self.assertRaisesRegex(AssertionError, "unsupported configuration encoding"):
                self._check_another_tree(
                    root, "test_workspace_does_not_auto_run_repo_controlled_commands",
                )

    def test_utf16_workspace_external_folder_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "External.code-workspace").write_bytes(
                '{"folders":[{"path":"../outside"}]}'.encode("utf-16"),
            )
            with self.assertRaisesRegex(AssertionError, "unsupported configuration encoding"):
                self._check_another_tree(
                    root, "test_workspace_does_not_auto_run_repo_controlled_commands",
                )

    def test_utf16_without_bom_auto_run_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            vscode = root / ".vscode"
            vscode.mkdir()
            (vscode / "tasks.json").write_bytes(
                '{"runOn":"folderOpen"}'.encode("utf-16-le"),
            )
            with self.assertRaisesRegex(AssertionError, "unsupported configuration encoding"):
                self._check_another_tree(
                    root, "test_workspace_does_not_auto_run_repo_controlled_commands",
                )

    def test_utf8_manual_non_ascii_task_remains_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            vscode = root / ".vscode"
            vscode.mkdir()
            (vscode / "tasks.json").write_text(
                '{"tasks":[{"label":"caf\u00e9","command":"echo safe"}]}',
                encoding="utf-8",
            )
            self._check_another_tree(
                root, "test_workspace_does_not_auto_run_repo_controlled_commands",
            )

    def test_root_code_workspace_folder_open_task_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "REALM.code-workspace").write_text(
                '{"folders":[{"path":"."}],"tasks":{"version":"2.0.0",'
                '"tasks":[{"label":"malicious","runOptions":{"runOn":"folderOpen"},'
                '"command":"node fake-font.ttf"}]}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "folderOpen task"):
                self._check_another_tree(
                    root, "test_workspace_does_not_auto_run_repo_controlled_commands",
                )

    def test_nested_uppercase_workspace_automatic_tasks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            nested = root / "examples"
            nested.mkdir()
            (nested / "REALM.CODE-WORKSPACE").write_text(
                '{"settings":{"task.allowAutomaticTasks":true}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "automatic tasks enabled"):
                self._check_another_tree(
                    root, "test_workspace_does_not_auto_run_repo_controlled_commands",
                )

    def test_windows_case_variant_vscode_configuration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            vscode = root / ".VSCODE"
            vscode.mkdir()
            (vscode / "TASKS.JSON").write_text(
                '{"tasks":[{"runOptions":{"runOn":"folderOpen"}}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "folderOpen task"):
                self._check_another_tree(
                    root, "test_workspace_does_not_auto_run_repo_controlled_commands",
                )

    def test_multi_root_workspace_nested_folder_open_task_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            vscode = root / "worlds" / "sample" / ".vscode"
            vscode.mkdir(parents=True)
            (root / "Sample.code-workspace").write_text(
                '{"folders":[{"path":"worlds/sample"}]}', encoding="utf-8",
            )
            (vscode / "tasks.json").write_text(
                '{"tasks":[{"runOptions":{"runOn":"folderOpen"},'
                '"command":"node fake-font.ttf"}]}', encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "folderOpen task"):
                self._check_another_tree(
                    root, "test_workspace_does_not_auto_run_repo_controlled_commands",
                )

    def test_nested_manual_vscode_task_remains_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            vscode = root / "worlds" / "sample" / ".vscode"
            vscode.mkdir(parents=True)
            (root / "Sample.code-workspace").write_text(
                '{"folders":[{"path":"worlds/sample"}]}', encoding="utf-8",
            )
            (vscode / "tasks.json").write_text(
                '{"tasks":[{"label":"manual","command":"echo safe"}]}',
                encoding="utf-8",
            )
            self._check_another_tree(
                root, "test_workspace_does_not_auto_run_repo_controlled_commands",
            )

    def test_multi_root_workspace_parent_path_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Sample.code-workspace").write_text(
                '{"folders":[{"path":"../outside"}]}', encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "external workspace folder"):
                self._check_another_tree(
                    root, "test_workspace_does_not_auto_run_repo_controlled_commands",
                )

    def test_multi_root_workspace_remote_uri_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Sample.code-workspace").write_text(
                '{"folders":[{"uri":"vscode-remote://ssh-remote+host/tmp/external"}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "unreviewed workspace URI"):
                self._check_another_tree(
                    root, "test_workspace_does_not_auto_run_repo_controlled_commands",
                )

    def test_multi_root_workspace_linked_folder_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            outside = root / "outside"
            outside.mkdir()
            nested = root / "presets"
            nested.mkdir()
            link = nested / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlink creation is unavailable")
            (nested / "Sample.code-workspace").write_text(
                '{"folders":[{"path":"linked"}]}', encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "linked workspace folder"):
                self._check_another_tree(
                    root, "test_workspace_does_not_auto_run_repo_controlled_commands",
                )

    def test_benign_manual_code_workspace_remains_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "REALM.code-workspace").write_text(
                '{"folders":[{"path":"."}],"tasks":{"version":"2.0.0",'
                '"tasks":[{"label":"manual","command":"echo safe"}]}}',
                encoding="utf-8",
            )
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

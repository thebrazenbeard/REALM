"""Version preflight is a release check, not proof of runtime correctness."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.sqlite_runtime import wal_reset_fix_present


class SQLiteRuntimeTests(unittest.TestCase):
    def test_affected_versions_are_rejected(self):
        for version in ((3, 7, 0), (3, 44, 5), (3, 49, 9), (3, 50, 4), (3, 50, 6), (3, 51, 2)):
            with self.subTest(version=version):
                self.assertFalse(wal_reset_fix_present(version))

    def test_documented_fixed_backports_and_newer_are_accepted(self):
        for version in ((3, 44, 6), (3, 50, 7), (3, 51, 3), (3, 53, 0), (4, 0, 0)):
            with self.subTest(version=version):
                self.assertTrue(wal_reset_fix_present(version))

    def test_unlisted_intermediate_branches_are_not_assumed_fixed(self):
        self.assertFalse(wal_reset_fix_present((3, 45, 2)))
        self.assertFalse(wal_reset_fix_present((3, 50, 0)))


if __name__ == "__main__":
    unittest.main()

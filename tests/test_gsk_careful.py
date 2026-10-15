from __future__ import annotations

import os
import unittest
from pathlib import Path
from veya.gsk.careful import check_careful_command, check_freeze_path

class TestGskCareful(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("VEYA_GSK_CAREFUL", None)
        os.environ.pop("VEYA_GSK_FREEZE_DIR", None)

    def test_careful_disabled(self):
        self.assertIsNone(check_careful_command("rm -rf /"))

    def test_careful_blocked_commands(self):
        os.environ["VEYA_GSK_CAREFUL"] = "1"

        self.assertIsNotNone(check_careful_command("rm -rf /"))
        self.assertIsNotNone(check_careful_command("rm -fr /home"))
        self.assertIsNotNone(check_careful_command("DROP TABLE users;"))
        self.assertIsNotNone(check_careful_command("git push origin main --force"))
        self.assertIsNotNone(check_careful_command("git reset --hard HEAD~1"))
        self.assertIsNotNone(check_careful_command("mkfs.ext4 /dev/sda1"))
        self.assertIsNotNone(check_careful_command("chmod -R 777 /"))

        # Safe commands should pass
        self.assertIsNone(check_careful_command("git status"))
        self.assertIsNone(check_careful_command("pytest -q"))

    def test_freeze_path(self):
        # Freeze disabled
        self.assertTrue(check_freeze_path("/etc/passwd"))

        # Freeze enabled to .gsk-tmp
        os.environ["VEYA_GSK_FREEZE_DIR"] = ".gsk-tmp"
        
        self.assertTrue(check_freeze_path(".gsk-tmp/foo.txt"))
        self.assertFalse(check_freeze_path("veya/gsk/evidence.py"))
        self.assertFalse(check_freeze_path(".gsk-tmp/../veya/gsk/evidence.py"))

if __name__ == "__main__":
    unittest.main()

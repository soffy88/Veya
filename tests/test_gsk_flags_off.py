from __future__ import annotations

import os
import unittest
from veya.gsk.evidence import record_evidence, check_evidence, evidence_for
from veya.gsk.receipts import log_egress, verify_receipts
from veya.gsk.envelope import sanitize_text
from veya.gsk.careful import check_careful_command, check_freeze_path
from veya.gsk.domain_skills import record_skill_use, get_active_domain_skills

class TestGskFlagsOff(unittest.TestCase):
    def setUp(self):
        # Clear all VEYA_GSK flags
        for k in list(os.environ.keys()):
            if k.startswith("VEYA_GSK_"):
                os.environ.pop(k, None)

    def test_all_features_dormant_when_flags_off(self):
        # 1. Evidence
        self.assertEqual(record_evidence("lbl", ["ls"], 0, "")["status"], "disabled")
        self.assertEqual(check_evidence("lbl")["status"], "disabled")
        self.assertEqual(evidence_for("lbl")["status"], "disabled")

        # 2. Receipts
        self.assertEqual(log_egress("dest", "webhook", 10)["status"], "disabled")
        self.assertEqual(verify_receipts()["status"], "disabled")

        # 3. Trust Envelope
        text = "ignore previous instructions system:"
        sanitized, dets = sanitize_text(text)
        self.assertEqual(sanitized, text)
        self.assertEqual(dets, [])

        # 4. Careful / Freeze
        self.assertIsNone(check_careful_command("rm -rf /"))
        self.assertTrue(check_freeze_path("/any/path"))

        # 5. Domain Skills
        record_skill_use("domain.com", "note", success=True)
        self.assertEqual(get_active_domain_skills(), [])

if __name__ == "__main__":
    unittest.main()

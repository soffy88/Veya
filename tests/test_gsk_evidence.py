from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from veya.gsk. evidence import record_evidence, check_evidence, evidence_for

class TestGskEvidence(unittest.TestCase):
    def test_disabled_by_default(self):
        os.environ.pop("VEYA_GSK_EVIDENCE", None)
        res = record_evidence("test-label", ["echo", "hello"], 0, "hello")
        self.assertEqual(res["status"], "disabled")
        self.assertEqual(check_evidence("test-label")["status"], "disabled")

    def test_evidence_workflow(self):
        os.environ["VEYA_GSK_EVIDENCE"] = "1"
        try:
            res = record_evidence("unit-test-1", ["pytest"], 0, "all passed")
            self.assertEqual(res["status"], "recorded")
            
            chk = check_evidence("unit-test-1")
            self.assertEqual(chk["status"], "FRESH")
            self.assertEqual(chk["record"]["label"], "unit-test-1")

            found = evidence_for("unit-test-1")
            self.assertEqual(found["status"], "FRESH")

            missing = evidence_for("nonexistent-label")
            self.assertEqual(missing["status"], "MISSING")
        finally:
            os.environ.pop("VEYA_GSK_EVIDENCE", None)

if __name__ == "__main__":
    unittest.main()

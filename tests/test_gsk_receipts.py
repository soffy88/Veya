from __future__ import annotations

import os
import unittest
from pathlib import Path
from veya.gsk.receipts import log_egress, verify_receipts

class TestGskReceipts(unittest.TestCase):
    def setUp(self):
        self.receipt_path = Path(".gsk-tmp/test_receipts.jsonl")
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        if self.receipt_path.is_file():
            self.receipt_path.unlink()
        os.environ["GSTACK_RECEIPTS_PATH"] = str(self.receipt_path)

    def tearDown(self):
        os.environ.pop("VEYA_GSK_EGRESS_RECEIPTS", None)
        os.environ.pop("GSTACK_RECEIPTS_PATH", None)
        if self.receipt_path.is_file():
            self.receipt_path.unlink()

    def test_disabled_by_default(self):
        res = log_egress("https://example.com/webhook", "webhook", 120)
        self.assertEqual(res["status"], "disabled")
        v = verify_receipts()
        self.assertEqual(v["status"], "disabled")

    def test_egress_chain_verification(self):
        os.environ["VEYA_GSK_EGRESS_RECEIPTS"] = "1"
        
        r1 = log_egress("https://api.telegram.org/bot", "im_send", 45)
        self.assertEqual(r1["status"], "logged")

        r2 = log_egress("https://webhook.site/test", "webhook", 88)
        self.assertEqual(r2["status"], "logged")

        v = verify_receipts()
        self.assertTrue(v["valid"])
        self.assertEqual(v["count"], 2)

        # Test tampering detection
        lines = self.receipt_path.read_text().splitlines()
        import json
        data = json.loads(lines[0])
        data["size"] = 999
        lines[0] = json.dumps(data)
        self.receipt_path.write_text("\n".join(lines) + "\n")

        v_tampered = verify_receipts()
        self.assertFalse(v_tampered["valid"])

if __name__ == "__main__":
    unittest.main()

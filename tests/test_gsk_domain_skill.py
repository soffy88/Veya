from __future__ import annotations

import os
import unittest
from pathlib import Path
from veya.gsk.domain_skills import record_skill_use, get_skill, get_active_domain_skills

class TestGskDomainSkill(unittest.TestCase):
    def setUp(self):
        self.skills_path = Path(".gsk-tmp/test_domain_skills.json")
        self.skills_path.parent.mkdir(parents=True, exist_ok=True)
        if self.skills_path.is_file():
            self.skills_path.unlink()
        os.environ["GSTACK_DOMAIN_SKILLS_PATH"] = str(self.skills_path)

    def tearDown(self):
        os.environ.pop("VEYA_GSK_DOMAIN_SKILLS", None)
        os.environ.pop("GSTACK_DOMAIN_SKILLS_PATH", None)
        if self.skills_path.is_file():
            self.skills_path.unlink()

    def test_disabled_by_default(self):
        record_skill_use("github.com", "Always check PR status before merging.")
        self.assertEqual(get_active_domain_skills(), [])

    def test_domain_skill_quarantine_to_active(self):
        os.environ["VEYA_GSK_DOMAIN_SKILLS"] = "1"
        domain = "github.com"
        note = "Always check PR status before merging."

        # 1. First use -> Quarantine
        res1 = record_skill_use(domain, note, success=True)
        self.assertEqual(res1["skill_status"], "quarantine")
        self.assertEqual(get_active_domain_skills(), [])

        # 2. Second use -> Quarantine
        record_skill_use(domain, note, success=True)
        self.assertEqual(get_active_domain_skills(), [])

        # 3. Third use -> Active!
        res3 = record_skill_use(domain, note, success=True)
        self.assertEqual(res3["skill_status"], "active")
        
        active = get_active_domain_skills()
        self.assertEqual(len(active), 1)
        self.assertIn("github.com", active[0])

    def test_domain_skill_demotion(self):
        os.environ["VEYA_GSK_DOMAIN_SKILLS"] = "1"
        domain = "api.github.com"
        note = "Use v3 endpoints."

        record_skill_use(domain, note, success=False)
        res = record_skill_use(domain, note, success=False)
        self.assertEqual(res["skill_status"], "demoted")
        self.assertEqual(get_active_domain_skills(), [])

if __name__ == "__main__":
    unittest.main()

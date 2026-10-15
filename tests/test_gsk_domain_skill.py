import pytest
from veya.gsk.domain_skills import DomainSkillManager

def test_domain_skills_quarantine_and_activation(tmp_path):
    storage = tmp_path / "domain_skills.json"
    manager = DomainSkillManager(storage_path=storage)
    
    domain = "python_async"
    entry = manager.register_skill(domain, "Always use asyncio.run() carefully.")
    assert entry["status"] == "quarantine"
    
    manager.record_usage(domain, 0, True)
    manager.record_usage(domain, 0, True)
    manager.record_usage(domain, 0, True)
    
    import os
    os.environ["VEYA_GSK_DOMAIN_SKILLS"] = "1"
    
    skills = manager.get_context_skills(domain)
    assert len(skills) == 1
    assert "Always use asyncio.run()" in skills[0]

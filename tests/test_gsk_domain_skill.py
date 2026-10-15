import pytest

from veya.gsk.domain_skills import (
    DomainSkillManager,
    extract_domain,
    inject_domain_skills,
    record_domain_usage,
)
from veya.remote.execution_context import ExecutionCapabilityContext, render_execution_context


def test_domain_skills_quarantine_and_activation(monkeypatch, tmp_path):
    storage = tmp_path / "domain_skills.json"
    manager = DomainSkillManager(storage_path=storage)
    domain = "python_async"
    entry = manager.register_skill(domain, "Always use asyncio.run() carefully.")
    assert entry["status"] == "quarantine"

    manager.record_usage(domain, 0, True)
    manager.record_usage(domain, 0, True)
    manager.record_usage(domain, 0, True)
    monkeypatch.setenv("VEYA_GSK_DOMAIN_SKILLS", "1")

    skills = manager.get_context_skills(domain)
    assert len(skills) == 1
    assert "Always use asyncio.run()" in skills[0]


def test_domain_skill_injects_into_existing_execution_context_only_when_enabled(monkeypatch, tmp_path):
    manager = DomainSkillManager(storage_path=tmp_path / "domain_skills.json")
    domain = "example.com"
    manager.register_skill(domain, "Prefer documented pagination endpoints.")
    for _ in range(3):
        manager.record_usage(domain, 0, True)

    context = ExecutionCapabilityContext(
        mission_id="mission-test",
        execution_id="execution-test",
        worker_type="test",
        workspace=str(tmp_path),
    )
    task = "Summarize the site https://www.example.com/docs?q=1."
    monkeypatch.delenv("VEYA_GSK_DOMAIN_SKILLS", raising=False)
    assert inject_domain_skills(context, task, manager) is None
    assert context.selected_skills == []
    assert "selected skills" not in render_execution_context(context)

    monkeypatch.setenv("VEYA_GSK_DOMAIN_SKILLS", "1")
    assert extract_domain(task) == "example.com"
    assert inject_domain_skills(context, task, manager) == domain
    rendered = render_execution_context(context)
    assert "gsk-domain:example.com:0" in rendered
    assert "Prefer documented pagination endpoints." in rendered


def test_three_successful_domain_tasks_activate_quarantined_note(monkeypatch, tmp_path):
    manager = DomainSkillManager(storage_path=tmp_path / "domain_skills.json")
    manager.register_skill("example.org", "Use the official docs before guessing API behavior.")
    monkeypatch.setenv("VEYA_GSK_DOMAIN_SKILLS", "1")

    context = ExecutionCapabilityContext(
        mission_id="mission-test",
        execution_id="execution-test",
        worker_type="test",
        workspace=str(tmp_path),
    )
    task = "Review https://example.org/reference"
    assert inject_domain_skills(context, task, manager) == "example.org"
    assert context.selected_skills == []
    for _ in range(2):
        record_domain_usage("example.org", True, manager)
    assert manager.data["example.org"][0]["status"] == "quarantine"
    record_domain_usage("example.org", True, manager)
    assert manager.data["example.org"][0]["status"] == "active"

    next_context = ExecutionCapabilityContext(
        mission_id="mission-next",
        execution_id="execution-next",
        worker_type="test",
        workspace=str(tmp_path),
    )
    inject_domain_skills(next_context, task, manager)
    assert len(next_context.selected_skills) == 1
    assert "official docs" in render_execution_context(next_context)

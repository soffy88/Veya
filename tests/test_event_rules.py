"""Tests for event rules loading and validation (fail-closed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.nano_w10.event_hub import parse_rules_file, render_ask


def _skills(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    (skills / "hub-feedback").mkdir(parents=True)
    (skills / "hub-feedback" / "manifest.json").write_text("{}", encoding="utf-8")
    return skills


def _bot_exists(bot_id: str) -> bool:
    return bot_id == "bot-1"


def _rule(**overrides):
    rule = {
        "name": "r1",
        "when": {"source": "virxact-hub", "types": ["feedback.new"]},
        "to": "bot-1",
        "skill": "hub-feedback",
        "ask": "Handle {summary} at {link} ({subject}).",
        "group": "hub-feedback",
        "parallel": 3,
        "tell": "needed",
        "timeout_s": 1800,
    }
    rule.update(overrides)
    return rule


def test_wrapper_and_single_rule_shapes(tmp_path):
    skills = _skills(tmp_path)
    wrapped = parse_rules_file(
        {"version": 1, "rules": [_rule()]}, bot_exists=_bot_exists, skills_dir=skills
    )
    assert len(wrapped) == 1 and wrapped[0]["name"] == "r1"
    single = parse_rules_file(_rule(), bot_exists=_bot_exists, skills_dir=skills)
    assert len(single) == 1
    assert parse_rules_file({"version": 1, "rules": []}, bot_exists=_bot_exists, skills_dir=skills) == []


def test_rejects_unknown_bot_skill_tell_parallel(tmp_path):
    skills = _skills(tmp_path)
    cases = [
        _rule(to="no-such-bot"),
        _rule(skill="no-such-skill"),
        _rule(tell="always"),
        _rule(tell="loud"),
        _rule(parallel=0),
        _rule(parallel="3"),
        _rule(timeout_s=0),
        _rule(ask="Handle {secret} now."),
        _rule(name=""),
        _rule(when={"source": "", "types": ["feedback.new"]}),
        _rule(when={"source": "x", "types": []}),
        _rule(extra_field=1),
    ]
    for bad in cases:
        with pytest.raises(ValueError):
            parse_rules_file(bad, bot_exists=_bot_exists, skills_dir=skills)


def test_rejects_duplicate_names_and_bad_shapes(tmp_path):
    skills = _skills(tmp_path)
    with pytest.raises(ValueError):
        parse_rules_file(
            {"rules": [_rule(), _rule()]}, bot_exists=_bot_exists, skills_dir=skills
        )
    with pytest.raises(ValueError):
        parse_rules_file([_rule()], bot_exists=_bot_exists, skills_dir=skills)
    with pytest.raises(ValueError):
        parse_rules_file(
            {"version": 2, "rules": [_rule()]}, bot_exists=_bot_exists, skills_dir=skills
        )
    with pytest.raises(ValueError):
        parse_rules_file("nope", bot_exists=_bot_exists, skills_dir=skills)


def test_defaults_applied(tmp_path):
    skills = _skills(tmp_path)
    minimal = {
        "name": "r-min",
        "when": {"source": "cron", "types": ["job.done"]},
        "to": "bot-1",
        "ask": "Look at this.",
    }
    (parsed,) = parse_rules_file(minimal, bot_exists=_bot_exists, skills_dir=skills)
    assert parsed["skill"] == ""
    assert parsed["group"] == ""
    assert parsed["parallel"] == 1
    assert parsed["tell"] == "silent"
    assert parsed["timeout_s"] == 1800.0


def test_render_ask_splits_instruction_and_materials():
    envelope = {
        "summary": "SUM",
        "link": "http://x/1",
        "subject": "obj-9",
    }
    text, materials = render_ask("Handle {summary} at {link} ({subject}).", envelope)
    assert "SUM" not in text and "http://x/1" not in text and "obj-9" not in text
    assert "[事件摘要见资料区]" in text
    assert "summary: SUM" in materials and "link: http://x/1" in materials
    assert "subject: obj-9" in materials


def test_render_ask_without_placeholders_still_exposes_values():
    text, materials = render_ask("Look at this.", {"summary": "SUM", "link": "", "subject": "S"})
    assert text == "Look at this."
    assert "summary: SUM" in materials

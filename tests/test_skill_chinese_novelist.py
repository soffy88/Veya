"""Acceptance tests for the vendored Chinese novelist skill package."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "templates" / "skills" / "chinese-novelist"


def test_manifest_is_valid_and_entrypoint_exists() -> None:
    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "chinese-novelist"
    assert manifest["type"] == "python"
    assert manifest["entrypoint"] == "run.py"
    assert (PACKAGE / manifest["entrypoint"]).is_file()
    assert isinstance(manifest["parameters"]["properties"], dict)
    assert manifest["parameters"]["required"] == ["action"]


def test_existing_skill_hub_loads_bundle_from_temporary_skills_dir() -> None:
    with tempfile.TemporaryDirectory(prefix="novel-skill-hub-", dir=ROOT) as tmp:
        skills_dir = Path(tmp) / "skills"
        skills_dir.mkdir()
        shutil.copytree(PACKAGE, skills_dir / "chinese-novelist")
        previous = os.environ.get("VEYA_SKILLS_DIR")
        os.environ["VEYA_SKILLS_DIR"] = str(skills_dir)
        # This checkout's optional platform/3O submodules may be unmounted. Stub
        # only the tool-registry boundary so the real SkillHub discovery, manifest
        # validation and static scan run against the temporary skills directory.
        had_tool_registry = "server.tool_registry" in sys.modules
        had_skill_hub = "server.skill_hub" in sys.modules
        if not had_tool_registry:
            stub = types.ModuleType("server.tool_registry")

            class ToolExecutionError(Exception):
                pass

            async def invoke_callback(fn, kwargs):
                result = fn(**kwargs)
                if hasattr(result, "__await__"):
                    return await result
                return result

            stub.ToolExecutionError = ToolExecutionError
            stub._invoke_callback = invoke_callback
            stub._schema_validator = lambda schema, owner="": (lambda value: None)
            stub._validate_arguments = lambda *args, **kwargs: None
            sys.modules["server.tool_registry"] = stub
        try:
            from server.skill_hub import VeyaSkillHub

            hub = VeyaSkillHub(skills_dir)
            assert hub.has("chinese-novelist")
            assert hub.skill_risk("chinese-novelist")["max_severity"] == "none"
        finally:
            if not had_skill_hub:
                sys.modules.pop("server.skill_hub", None)
            if not had_tool_registry:
                sys.modules.pop("server.tool_registry", None)
            if previous is None:
                os.environ.pop("VEYA_SKILLS_DIR", None)
            else:
                os.environ["VEYA_SKILLS_DIR"] = previous


def test_package_contains_no_sponsorship_or_affiliate_material() -> None:
    forbidden = re.compile(r"infistar|aff=|afdian|爱发电", re.IGNORECASE)
    for path in PACKAGE.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".md", ".py", ".json"}:
            content = path.read_text(encoding="utf-8")
            assert not forbidden.search(content), f"promotional content found in {path.relative_to(PACKAGE)}"


def test_wordcount_script_counts_han_characters_in_chapter_sample() -> None:
    script = PACKAGE / "scripts" / "check_chapter_wordcount.py"
    spec = importlib.util.spec_from_file_location("novel_wordcount_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.count_chinese_words("你好，世界！") == 4

    with tempfile.TemporaryDirectory(prefix="novel-wordcount-", dir=ROOT) as tmp:
        chapter = Path(tmp) / "第01章-测试.md"
        chapter.write_text("# 第01章 测试\n你好，世界！\n", encoding="utf-8")
        result = module.check_chapter(str(chapter), min_words=4)
        assert result["status"] == "pass"
        assert result["word_count"] == 4

def test_all_reference_markdown_is_vendored_with_provenance() -> None:
    flows = sorted((PACKAGE / "references" / "flows").glob("*.md"))
    guides = sorted((PACKAGE / "references" / "guides").glob("*.md"))
    assert len(flows) == 8
    assert len(guides) == 10
    for path in [*flows, *guides, PACKAGE / "SKILL.md"]:
        content = path.read_text(encoding="utf-8")
        assert "https://github.com/PenglongHuang/chinese-novelist-skill" in content
        assert "Copyright (c) 2026 PenglongHuang" in content

def test_preferences_are_scoped_to_project_workspace() -> None:
    for path in [PACKAGE / "SKILL.md", *(PACKAGE / "references" / "flows").glob("*.md")]:
        content = path.read_text(encoding="utf-8")
        assert "~/.veya" not in content
        assert "/home/" not in content
    content = (PACKAGE / "references" / "flows" / "shared-infrastructure.md").read_text(encoding="utf-8")
    assert "chinese-novelist/user-preferences.json" in content


"""Spec 可执行化 + ECC 领域目录门禁 (spec-kit / ECC 3O 内化)。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for lib in ("oprim", "omodul", "oservi", "obase", "oskill"):
    sys.path.insert(0, str(ROOT / "platform" / "3O" / lib))

from oprim._spec_parse import parse_spec, validate_spec  # noqa: E402
from oskill.spec_execute import SpecExecutor, render_preset  # noqa: E402

# =========================================================================
# W1 — 可执行 Spec
# =========================================================================

SAMPLE_SPEC = """## 目标
实现用户登录模块。

## 验收标准
- [ ] 登录接口可用
- [ ] 密码校验正确

## 约束
- 不引入新依赖

## 测试门
- pytest tests/test_auth.py 通过
"""


def test_parse_spec_sections():
    spec = parse_spec(SAMPLE_SPEC)
    assert "登录" in spec["goal"]
    assert len(spec["acceptance"]) == 2
    assert spec["acceptance"][0] == "登录接口可用"   # 列表项剥离
    assert "pytest" in spec["test_gate"]
    assert "新依赖" in spec["constraints"][0]


def test_parse_spec_json():
    spec = parse_spec(json.dumps({"goal": "G", "acceptance": ["a1", "a2"],
                                  "test_gate": "t"}))
    assert spec["goal"] == "G" and len(spec["acceptance"]) == 2


def test_validate_spec_missing_sections():
    bad = parse_spec("## 目标\n只有一个章节\n")
    check = validate_spec(bad)
    assert check["ok"] is False
    assert set(check["missing"]) == {"acceptance", "test_gate"}


def test_render_preset():
    text = render_preset("scaffold", {"project": "myapp"})
    assert "myapp" in text
    assert "验收标准" in text
    with pytest.raises(ValueError):
        render_preset("nonexistent")


def test_spec_execute_full_loop():
    """spec → 拆任务 → 实现 → 验收 (测试门)。"""
    calls: list[str] = []
    executor = SpecExecutor()

    async def implementer(task, idx):
        calls.append(task)
        return {"ok": True, "output": f"实现: {task[:20]}"}

    async def test_runner():
        return {"ok": True, "output": "pytest: 3 passed"}

    report = asyncio.run(executor.execute(
        SAMPLE_SPEC, implementer=implementer, test_runner=test_runner))
    assert report["ok"] is True
    assert report["status"] == "accepted"
    assert report["tasks"] == 2
    assert len(calls) == 2
    assert report["test_gate"]["ok"] is True


def test_spec_execute_invalid_and_failing_gate():
    executor = SpecExecutor()

    async def implementer(task, idx):
        return {"ok": True, "output": "x"}

    async def failing_runner():
        return {"ok": False, "output": "pytest: 1 failed"}

    # 缺章节 → invalid
    r1 = asyncio.run(executor.execute("## 目标\n只有目标", implementer=implementer))
    assert r1["status"] == "invalid_spec"

    # 测试门失败 → needs_work
    r2 = asyncio.run(executor.execute(SAMPLE_SPEC, implementer=implementer,
                                      test_runner=failing_runner))
    assert r2["status"] == "needs_work"
    assert r2["test_gate"]["ok"] is False


def test_spec_api_presets():
    from fastapi.testclient import TestClient

    from server.app import app

    r = TestClient(app).get("/api/v1/spec/presets")
    assert r.status_code == 200
    assert set(r.json()["presets"]) == {"scaffold", "lean", "self-test", "architecture"}


def test_spec_api_execute_stub():
    """API 执行: stub 模式 (无 key) 也返回结构化报告。"""
    from fastapi.testclient import TestClient

    from server.app import app

    r = TestClient(app).post("/api/v1/spec/execute", json={
        "spec": SAMPLE_SPEC, "async_impl": False})
    assert r.status_code == 200
    data = r.json()
    assert data["tasks"] == 2
    assert data["status"] in ("accepted", "needs_work")


# =========================================================================
# W2 — ECC 领域 Agent 目录
# =========================================================================

def test_ecc_agent_skill_loaded():
    """导入的 ECC 领域技能已热载 (线上技能库)。"""
    from server.skill_hub import VeyaSkillHub

    hub = VeyaSkillHub(skills_dir=str(Path.home() / ".veya" / "skills"))
    hub.reload_skills()
    ecc = [s for s in hub._all_skill_names() if s.startswith("ecc_")]
    assert len(ecc) >= 5, f"ECC 领域技能不足: {len(ecc)}"
    # 代表性领域 agent
    assert any("code_review" in s or "architect" in s or "resolver" in s for s in ecc)


def test_ecc_agent_executor_returns_instruction():
    """领域技能 main() 返回 system prompt + 任务指令 (LLM 消费)。"""
    from server.skill_hub import VeyaSkillHub

    hub = VeyaSkillHub(skills_dir=str(Path.home() / ".veya" / "skills"))
    hub.reload_skills()
    name = next(s for s in hub._all_skill_names() if s.startswith("ecc_"))
    executor = hub._executors[name]

    import asyncio

    out = asyncio.run(executor(goal="审查这段 FastAPI 代码"))
    if isinstance(out, str):
        out = json.loads(out) if out.startswith("{") else {"output": out}
    assert isinstance(out, dict)
    assert out.get("ok") is True
    assert out.get("domain_agent") or out.get("instruction")


# =========================================================================
# W3 — 硬规则
# =========================================================================

def test_rules_file_generated():
    rules = Path.home() / ".veya" / "rules.md"
    assert rules.exists()
    text = rules.read_text()
    assert "Must Always" in text and "Must Never" in text
    assert "redact" in text or "密钥" in text
    # 模板同步
    assert (ROOT / "templates" / "rules.md").exists()

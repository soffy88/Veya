"""一期 (Vigla 层) 门禁 — G3 审计 / G4 回滚 / G5 事件管道 / G6 基准评测。

G3/G4 覆盖真实 git 仓库场景 (tmp repo + worktree + 真实 diff)。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for lib in ("oprim", "omodul", "oservi", "obase", "oskill"):
    sys.path.insert(0, str(ROOT / "platform" / "3O" / lib))

from omodul.mission_revert import mission_revert, snapshot_mission_baseline  # noqa: E402
from omodul.mission_supervisor import (  # noqa: E402
    mission_supervisor,
    parse_diff,
)
from oprim._canonical_event_ingest import (  # noqa: E402
    canonical_event_ingest,
    compute_event_fingerprint,
    deserialize_vendor,
)
from oservi.agent_bench_harness import agent_bench_harness  # noqa: E402

# =========================================================================
# G5 — 规范化事件管道 (原语层先测)
# =========================================================================


def test_deserialize_vendor_formats():
    # claude stream-json
    evts = deserialize_vendor(
        b'{"type":"content_block_delta","delta":{"text":"hi"}}\nnot-json-line\n', "claude"
    )
    assert evts[0]["type"] == "content_block_delta"
    assert evts[1]["type"] == "text"  # 非 JSON 行兜底


def test_canonical_event_ingest_fingerprint_and_persist(tmp_path):
    sink_path = tmp_path / "events.jsonl"
    from oprim._audit_emit import JsonlSink

    out = canonical_event_ingest(
        b'{"type":"message_start","content":"start"}\n{"type":"text_delta","delta":"hello"}',
        "codex",
        source="mission/m1",
        sink=JsonlSink(str(sink_path)),
    )
    assert out["count"] == 2
    assert out["persisted"] is True
    assert len(out["fingerprint"]) == 64  # sha256

    # 可回放 + 指纹一致
    from oprim._audit_emit import JsonlSink as J

    events = J(str(sink_path)).read_all()
    assert len(events) == 2
    assert all((e.get("inputs") or {}).get("fingerprint") for e in events)
    # 事件类型映射到白名单
    assert events[0]["event_type"] in ("diagnose", "plan", "decide", "execute", "learn")
    assert events[0]["inputs"]["vendor"] == "codex"


def test_fingerprint_detects_tamper():
    e1 = {"a": 1}
    assert compute_event_fingerprint(e1) != compute_event_fingerprint({**e1, "a": 2})


# =========================================================================
# G3 — Mission Supervisor (真实 git diff)
# =========================================================================


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('v1')\n")
    (repo / ".env").write_text("SECRET=x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _make_diff(repo: Path, changes: dict[str, str]) -> str:
    for fname, content in changes.items():
        (repo / fname).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    r = subprocess.run(
        ["git", "diff", "--cached", "--no-color"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout


def test_mission_supervisor_blocks_secret_leak(git_repo):
    diff = _make_diff(
        git_repo, {"app.py": "print('v2')\nKEY='sk-1234567890abcdef1234567890abcdef'\n"}
    )
    out = mission_supervisor("m1", diff)
    assert out["verdict"] == "block"
    assert any("秘钥" in v for v in out["violations"])
    assert out["files_reviewed"] >= 1


def test_mission_supervisor_blocks_protected_path(git_repo):
    diff = _make_diff(git_repo, {".env": "SECRET=changed\n"})
    out = mission_supervisor("m2", diff)
    assert out["verdict"] == "block"
    assert any("保护文件" in v for v in out["violations"])


def test_mission_supervisor_path_allowlist(git_repo):
    diff = _make_diff(git_repo, {"outside.txt": "x\n"})
    out = mission_supervisor("m3", diff, policy={"path_allowlist": ["src/"]})
    assert out["verdict"] == "request_changes"
    assert any("越界" in v for v in out["violations"])


def test_mission_supervisor_approve_clean(git_repo):
    (git_repo / "src").mkdir(exist_ok=True)
    diff = _make_diff(git_repo, {"src/feature.py": "def f(): return 1\n"})
    out = mission_supervisor("m4", diff, policy={"path_allowlist": ["src/"]})
    assert out["verdict"] == "approve"
    assert out["violations"] == []


def test_parse_diff_structured():
    diff = (
        "diff --git a/a.py b/a.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/a.py\n@@ -0,0 +1 @@\n+print(1)\n"
    )
    entries = parse_diff(diff)
    assert entries[0].path == "a.py"
    assert entries[0].status == "added"
    assert "+print(1)" in entries[0].content


# =========================================================================
# G4 — Mission Revert (真实 git worktree 回滚 + 隔离)
# =========================================================================


def test_mission_revert_restores_baseline_and_quarantines(git_repo):
    # worktree 模拟 worker 工作区
    wt_path = git_repo / "wt1"
    subprocess.run(["git", "worktree", "add", "-b", "w1", str(wt_path)], cwd=git_repo, check=True)
    (wt_path / "broken.py").write_text("print('broken')\n")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt_path, capture_output=True, text=True, check=True
    ).stdout.strip()

    out = mission_revert(
        "m1",
        [{"worker_id": "w1", "worktree": str(wt_path)}],
        base_commits={str(wt_path): base},
        quarantine_dir=str(git_repo / "quarantine"),
    )
    assert out["reverted"] is True
    # 基线恢复: broken.py 消失
    assert not (wt_path / "broken.py").exists()
    # 坏改动隔离 (patch 存在)
    assert len(out["quarantined_changes"]) == 1
    patch = Path(out["quarantined_changes"][0])
    assert "broken" in patch.read_text()
    assert out["audit_id"]

    subprocess.run(["git", "worktree", "remove", "--force", str(wt_path)], cwd=git_repo, check=True)


def test_mission_revert_missing_baseline_is_structured(git_repo):
    out = mission_revert("m2", [{"worker_id": "w1", "worktree": str(git_repo)}])
    assert out["reverted"] is False
    assert "无基线" in out["restored"][0]["error"]


def test_snapshot_mission_baseline(git_repo):
    states = snapshot_mission_baseline("m3", [{"worker_id": "w0", "worktree": str(git_repo)}])
    assert states[0].base_commit
    assert states[0].worker_id == "w0"


# =========================================================================
# G6 — 确定性基准评测
# =========================================================================


def test_bench_harness_mock_deterministic():
    tasks = [{"repo": "r1", "prompt": "add feature", "gold_patch": "def f(): return 1", "id": "t1"}]
    r1 = agent_bench_harness(tasks, ["claude", "codex"])
    r2 = agent_bench_harness(tasks, ["claude", "codex"])
    # 确定性: 两次运行完全一致
    assert r1 == r2
    assert r1["summary"]["vendors"] == ["claude", "codex"]
    for v in ("claude", "codex"):
        br = r1["per_vendor"][v]
        assert "completion_rate" in br and "pass_rate" in br
        assert "token_usage" in br and "cost" in br


def test_bench_harness_custom_executor():
    calls = []

    def executor(vendor, prompt, ctx):
        calls.append(vendor)
        return {"output": ctx.get("gold_patch"), "token_usage": {"prompt_tokens": 10}}

    tasks = [{"repo": "r", "prompt": "p", "gold_patch": "GOLD"}]
    out = agent_bench_harness(tasks, ["pi"], executor=executor)
    assert calls == ["pi"]
    assert out["per_vendor"]["pi"]["pass_rate"] == 1.0


# =========================================================================
# 账本
# =========================================================================


def test_replica_ledger_phase_one_registered():
    from server.operator_ledger import REPLICA_LEDGER, replica_ledger_summary

    assert set(REPLICA_LEDGER) == {
        "G3_mission_supervisor",
        "G4_mission_revert",
        "G5_canonical_event_ingest",
        "G6_agent_bench_harness",
        "G1_artifact_preview",
        "G2_agent_team_monitor",
        "G7_skill_crystallize",
        "G8_trigger_register",
    }
    summary = replica_ledger_summary()
    g3 = next(s for s in summary if s["name"] == "G3_mission_supervisor")
    assert g3["status"] == "registered"
    g1 = next(s for s in summary if s["name"] == "G1_artifact_preview")
    assert g1["status"] == "registered"  # 二期已实施

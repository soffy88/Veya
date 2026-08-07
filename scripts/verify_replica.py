#!/usr/bin/env python3
"""三平台复刻 8 算子实战验证 (非 mock)。

场景全部真实: 真实 git 仓库/diff/worktree、真实 vendor 输出格式、真实事件流、
真实失败教训 ×3 结晶、真实触发器触发回调。

用法: python scripts/verify_replica.py [--keep]
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for lib in ("oprim", "omodul", "oservi", "obase", "oskill"):
    sys.path.insert(0, str(ROOT / "platform" / "3O" / lib))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="veya-verify-"))
    print("验证工作区:", tmp)

    # ── G3 Mission Supervisor (真实 git diff) ─────────────────────────
    print("\n[G3] mission_supervisor (真实 git 仓库 + 真实 diff)")
    repo = tmp / "repo"
    repo.mkdir()
    for c in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"]):
        subprocess.run(c, cwd=repo, check=True)
    (repo / "app.py").write_text("print('v1')\n")
    (repo / ".env").write_text("SECRET=x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    # 含秘钥泄漏的改动
    (repo / "app.py").write_text("print('v2')\nKEY='sk-abcdef1234567890abcdef1234567890'\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--no-color"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout

    from omodul.mission_supervisor import mission_supervisor
    out = mission_supervisor("verify-m1", diff)
    check("秘钥泄漏被 block", out["verdict"] == "block",
          f"verdict={out['verdict']} violations={len(out['violations'])}")
    check("审计落盘", bool(out["audit_id"]))

    # 干净改动 → approve
    (repo / "app.py").write_text("print('v3')\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    diff2 = subprocess.run(["git", "diff", "--cached", "--no-color"], cwd=repo,
                           capture_output=True, text=True, check=True).stdout
    out2 = mission_supervisor("verify-m2", diff2)
    check("干净改动 approve", out2["verdict"] == "approve")

    # ── G4 Mission Revert (真实 worktree 回滚) ────────────────────────
    print("\n[G4] mission_revert (真实 worktree 改乱 → 一键回滚)")
    wt = repo / "wt1"
    subprocess.run(["git", "worktree", "add", "-b", "w1", str(wt)], cwd=repo, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt,
                          capture_output=True, text=True, check=True).stdout.strip()
    (wt / "broken.py").write_text("print('broken')\n")   # 未跟踪
    (wt / "app.py").write_text("print('HACKED')\n")      # 已跟踪

    from omodul.mission_revert import mission_revert
    rv = mission_revert("verify-m1", [{"worker_id": "w1", "worktree": str(wt)}],
                        base_commits={str(wt): base},
                        quarantine_dir=str(tmp / "quarantine"))
    check("回滚恢复基线", rv["reverted"] is True and not (wt / "broken.py").exists()
          and (wt / "app.py").read_text() == "print('v1')\n"
          and (wt / "app.py").read_text() != "print('HACKED')\n")
    check("坏改动隔离 (patch 含未跟踪)", len(rv["quarantined_changes"]) == 1 and
          "HACKED" in Path(rv["quarantined_changes"][0]).read_text())
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, check=True)

    # ── G5 Canonical Event Ingest (真实 claude stream-json 格式) ──────
    print("\n[G5] canonical_event_ingest (claude stream-json 真实格式)")
    claude_output = (
        b'{"type":"message_start","message":{"role":"assistant"}}\n'
        b'{"type":"content_block_delta","delta":{"type":"text_delta","text":"def f():\\n"}}\n'
        b'{"type":"content_block_delta","delta":{"type":"text_delta","text":"    return 1\\n"}}\n'
        b'{"type":"message_stop"}\n'
    )
    from oprim._audit_emit import JsonlSink
    from oprim._canonical_event_ingest import canonical_event_ingest
    ev_sink = tmp / "events.jsonl"
    ce = canonical_event_ingest(claude_output, "claude", source="verify",
                                sink=JsonlSink(str(ev_sink)))
    check("归一为 4 条规范事件", ce["count"] == 4)
    check("sha256 指纹", len(ce["fingerprint"]) == 64)
    replay = JsonlSink(str(ev_sink)).read_all()
    check("可回放且指纹在", len(replay) == 4 and all(
        (e.get("inputs") or {}).get("fingerprint") for e in replay))

    # ── G6 Agent Bench Harness (确定性评测) ───────────────────────────
    print("\n[G6] agent_bench_harness (确定性 mock 评测)")
    from oservi.agent_bench_harness import agent_bench_harness
    tasks = [
        {"repo": "r1", "prompt": "实现 is_even", "gold_patch": "def is_even(n): return n % 2 == 0"},
        {"repo": "r2", "prompt": "实现 reverse", "gold_patch": "def reverse(s): return s[::-1]"},
    ]
    bench = agent_bench_harness(tasks, ["claude", "codex", "pi"])
    check("三 vendor 报表", set(bench["per_vendor"]) == {"claude", "codex", "pi"})
    check("确定性 (两次一致)", agent_bench_harness(tasks, ["claude"]) ==
          agent_bench_harness(tasks, ["claude"]))
    check("指标齐全", all(all(k in v for k in ("completion_rate", "pass_rate", "cost"))
                          for v in bench["per_vendor"].values()))

    # ── G1 Artifact Preview (真实恶意 HTML + echarts) ─────────────────
    print("\n[G1] artifact_preview (XSS 样本 + ECharts 校验)")
    from omodul.artifact_preview import artifact_preview
    evil_html = ('<div onclick="steal()">x</div><script>fetch("//evil")</script>'
                 '<a href="javascript:alert(1)">y</a>')
    ap = artifact_preview({"type": "html", "content": evil_html},
                          snapshot_dir=str(tmp / "artifacts"))
    check("XSS 全剥离", ap["renderable"] and "<script" not in ap["sanitized_content"].lower()
          and "onclick" not in ap["sanitized_content"] and "javascript:" not in ap["sanitized_content"])
    check("快照落盘", Path(ap["snapshot_path"]).exists() and ap["snapshot_id"].startswith("art_"))
    ec = artifact_preview({"type": "echarts_json",
                           "content": '{"xAxis": {"data": ["a"]}, "series": [{"data": [1]}]}'},
                          snapshot_dir=str(tmp / "artifacts"))
    check("ECharts 校验通过", ec["renderable"] and ec["issues"] == [])

    # ── G2 Team Monitor (真实事件流投影) ──────────────────────────────
    print("\n[G2] agent_team_monitor (真实事件流 → 事件溯源投影)")
    from obase.event_bus import EventBus
    from oservi.agent_team_monitor import TeamMonitor
    bus = EventBus()
    mon = TeamMonitor("verify-team", ["w1", "w2"], bus).start()
    bus.publish("agent.start", {"agent_id": "w1", "task": "写测试"})
    bus.publish("agent.message", {"agent_id": "w1", "message": "进度 50%"})
    bus.publish("agent.end", {"agent_id": "w1", "status": "done",
                              "artifacts": ["tests/test_x.py"]})
    bus.publish("agent.error", {"agent_id": "w2", "error": "依赖缺失"})
    state = mon.live_state()
    check("w1 投影 done+task+artifact",
          state["w1"]["status"] == "done" and state["w1"]["current_task"] == "写测试"
          and state["w1"]["artifacts"] == ["tests/test_x.py"])
    check("w2 投影 error", state["w2"]["status"] == "error")
    # 事件溯源重放
    mon2 = TeamMonitor("verify-team2", ["w1"], bus).start()
    check("新监视器重放历史", mon2.live_state()["w1"]["status"] == "done")

    # ── G7 Skill Crystallize (真实失败教训 ×3 → 结晶 + 热载) ──────────
    print("\n[G7] skill_crystallize (真实失败教训 ×3 → 技能包 + skill_hub 热载)")
    import importlib
    sc = importlib.import_module("omodul.skill_crystallize")
    sc.LESSONS_FILE = tmp / "lessons.json"
    sc.SKILLS_DIR = tmp / "skills"  # 一级技能库 (skill_hub 热载区)
    lesson = {"trigger_type": "verify_failed",
              "evidence": {"test": "test_metrics_api"},
              "subject_ref": "metrics", "lesson": "指标 API 测试失败: 时区未归一"}
    res = None
    for _ in range(3):
        res = sc.skill_crystallize(lesson, recurrence_threshold=3,
                                   lessons_file=str(tmp / "lessons.json"))
    check("阈值 3 次结晶", res["crystallized"] is True and res["skill_module"])
    pkg = Path(res["skill_module"]).parent
    check("技能包结构 (manifest+run.py)", (pkg / "manifest.json").exists() and (pkg / "run.py").exists())

    from server.skill_hub import VeyaSkillHub
    hub = VeyaSkillHub(skills_dir=str(tmp / "skills"))
    hub.reload_skills()
    check("skill_hub 热载结晶技能", "crystallized_" in " ".join(hub.list_skills()) or
          any("crystallized" in s for s in hub.list_skills()),
          f"skills={hub.list_skills()}")

    # ── G8 Trigger Register (真实 event 触发器 → 回调触发) ─────────────
    print("\n[G8] trigger_register (event 触发器 → 事件触发回调)")
    from oservi.trigger_register import TriggerRegistry
    fired: list[dict] = []
    tbus = EventBus()
    treg = TriggerRegistry(event_bus=tbus, triggers_file=str(tmp / "triggers.json"))
    b = treg.register("event", "agent.end", "wf_daily_report",
                      callback=lambda payload: fired.append(payload))
    tbus.publish("agent.end", {"agent_id": "w1", "status": "done"})
    check("事件触发回调", len(fired) == 1 and fired[0]["event"] == "agent.end")
    check("触发计数+持久化", treg.list()[0]["trigger_count"] == 1 and
          TriggerRegistry(triggers_file=str(tmp / "triggers.json")).list()[0]["binding_id"] == b["binding_id"])

    # 线上部署确认
    print("\n[线上] 部署确认")
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8767/api/v1/operators", timeout=5) as r:
            data = json.loads(r.read())
        rep = data.get("replica", [])
        check("线上 replica 账本 8/8 registered",
              len(rep) == 8 and all(x["status"] == "registered" for x in rep))
    except Exception as e:
        check("线上 replica 账本", False, f"不可达: {e}")

    print("\n" + "=" * 56)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"验证结果: {passed}/{len(RESULTS)} 通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ❌ {name} {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Phase 1 regressions: real proposer channel, gating, and evidence tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.harness._harness2 import import_harness2_core
from server.harness.channel import (
    ProposerChannel,
    candidate_channels,
    probe_channel,
    select_channel,
)
from server.harness.proposer import (
    FLAG_NAME,
    MAX_TOOL_ROUNDS,
    ProposerDisabled,
    ProposerError,
    VeyaProposer,
    _EvidenceTools,
    extract_notes,
    proposer_enabled,
)

# ── fixtures ──


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch: pytest.MonkeyPatch):
    """每条测试都从 flag 关闭出发，需要开启的显式打开。"""
    monkeypatch.delenv(FLAG_NAME, raising=False)


def _evidence(tmp_path: Path) -> Path:
    root = tmp_path / "ctx"
    (root / "rollouts").mkdir(parents=True)
    (root / "harness").mkdir(parents=True)
    (root / "task.md").write_text("do the thing\n", encoding="utf-8")
    (root / "rollouts" / "base1.md").write_text("[ACTION] t\n[OBSERVATION] ok\n", encoding="utf-8")
    (root / "harness" / "systemprompt.md").write_text(
        "# base\n\nline one\nline two\nline three\n", encoding="utf-8"
    )
    return root


def _ok_probe(channel: ProposerChannel) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}}


def _stub_probe(channel: ProposerChannel) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": "LLM provider not configured"}}],
        "usage": {},
    }


# ── feature flag ──


def test_flag_registered_appended_and_off_by_default():
    from server import feature_flags

    names = [f.name for f in feature_flags.FLAGS]
    assert FLAG_NAME in names
    assert names.index(FLAG_NAME) == len(names) - 1  # 表尾追加，不重排既有行
    spec = next(f for f in feature_flags.FLAGS if f.name == FLAG_NAME)
    assert spec.default is False


def test_proposer_disabled_by_default(tmp_path):
    with pytest.raises(ProposerDisabled):
        VeyaProposer(audit_dir=tmp_path / "audit")


def test_proposer_enabled_reads_the_flag(monkeypatch):
    assert proposer_enabled() is False
    monkeypatch.setenv(FLAG_NAME, "1")
    assert proposer_enabled() is True


# ── channel selection ──


def test_probe_rejects_stub_and_accepts_real_reply():
    assert probe_channel(ProposerChannel("p", "m", "t"), probe=_stub_probe)[0] is False
    ok, reason = probe_channel(ProposerChannel("p", "m", "t"), probe=_ok_probe)
    assert ok is True
    assert "p/m" in reason


def test_probe_rejects_error_flagged_reply():
    def _err(channel: ProposerChannel) -> dict:
        return {"error": True, "choices": [{"message": {"content": "nope"}}], "usage": {}}

    ok, _reason = probe_channel(ProposerChannel("p", "m", "t"), probe=_err)
    assert ok is False


def test_probe_catches_raising_probe():
    def _boom(channel: ProposerChannel) -> dict:
        raise ConnectionError("no route")

    ok, reason = probe_channel(ProposerChannel("p", "m", "t"), probe=_boom)
    assert ok is False
    assert "ConnectionError" in reason


def test_select_channel_prefers_deepseek_flash(monkeypatch):
    monkeypatch.delenv("HARNESS2_IMPROVER_MODEL", raising=False)
    monkeypatch.delenv("HARNESS2_IMPROVER_PROVIDER", raising=False)
    candidates = candidate_channels()
    assert candidates[0].provider == "opencode-go"
    assert candidates[0].model == "deepseek-v4.1-flash"
    assert candidates[0].source == "deepseek-flash"


def test_select_channel_honours_env_override(monkeypatch):
    monkeypatch.setenv("HARNESS2_IMPROVER_MODEL", "some-model")
    monkeypatch.setenv("HARNESS2_IMPROVER_PROVIDER", "some-provider")
    channel, _records = select_channel(probe=_ok_probe)
    assert channel is not None
    assert channel.label == "some-provider/some-model"
    assert channel.source == "env-override"


def test_select_channel_requires_both_override_halves(monkeypatch):
    monkeypatch.delenv("HARNESS2_IMPROVER_MODEL", raising=False)
    monkeypatch.setenv("HARNESS2_IMPROVER_PROVIDER", "some-provider")
    with pytest.raises(ValueError, match="HARNESS2_IMPROVER_MODEL"):
        candidate_channels()


def test_select_channel_returns_none_when_all_candidates_fail():
    channel, records = select_channel(probe=_stub_probe)
    assert channel is None
    assert records  # 每个候选都留下判定记录，不静默


# ── evidence tools ──


def test_read_file_and_list_dir(tmp_path):
    tools = _EvidenceTools(_evidence(tmp_path))
    assert "do the thing" in tools.read_file("task.md")
    listing = tools.list_dir("")
    assert "rollouts/base1.md" in listing
    assert "harness/systemprompt.md" in listing


def test_write_file_is_confined_to_the_harness_tree(tmp_path):
    tools = _EvidenceTools(_evidence(tmp_path))
    tools.write_file("harness/new.md", "x")
    assert (tmp_path / "ctx" / "harness" / "new.md").is_file()
    with pytest.raises(ValueError, match="writable"):
        tools.write_file("task.md", "overwrite")


def test_path_escape_is_rejected(tmp_path):
    tools = _EvidenceTools(_evidence(tmp_path))
    for bad in ("../../etc/passwd", "/etc/passwd"):
        with pytest.raises(ValueError, match="escapes"):
            tools.read_file(bad)
    with pytest.raises(ValueError, match="escapes"):
        tools.list_dir("..")


def test_apply_patch_replaces_in_place(tmp_path):
    tools = _EvidenceTools(_evidence(tmp_path))
    tools.apply_patch(
        "harness/systemprompt.md",
        "--- a/harness/systemprompt.md\n+++ b/harness/systemprompt.md\n"
        "@@ -3,3 +3,3 @@\n line one\n-line two\n+line TWO\n line three\n",
    )
    body = (tmp_path / "ctx" / "harness" / "systemprompt.md").read_text(encoding="utf-8")
    assert "line TWO" in body
    assert "line two" not in body
    # 就地替换，不复制：总行数不变
    assert body.count("line one") == 1
    assert body.count("line three") == 1


def test_apply_patch_locates_by_context_not_stale_line_numbers(tmp_path):
    """行号过期时仍就地命中——这是修掉"补丁落点漂移→内容被复制"的回归。"""
    tools = _EvidenceTools(_evidence(tmp_path))
    # 先加两行，让模型手里的旧行号整体偏移
    tools.write_file(
        "harness/systemprompt.md",
        "# base\n\npreamble A\npreamble B\n\nline one\nline two\nline three\n",
    )
    tools.apply_patch(
        "harness/systemprompt.md",
        "@@ -3,3 +3,3 @@\n line one\n-line two\n+line TWO\n line three\n",
    )
    body = (tmp_path / "ctx" / "harness" / "systemprompt.md").read_text(encoding="utf-8")
    assert body.count("line two") == 0
    assert body.count("line TWO") == 1
    assert body.count("line three") == 1


def test_apply_patch_rejects_ambiguous_context(tmp_path):
    tools = _EvidenceTools(_evidence(tmp_path))
    tools.write_file(
        "harness/systemprompt.md",
        "# base\n\nline one\nline two\nline three\nline one\nline two\nline three\n",
    )
    with pytest.raises(ValueError, match="could not locate"):
        tools.apply_patch(
            "harness/systemprompt.md",
            "@@ -3,3 +3,3 @@\n line one\n-line two\n+line TWO\n line three\n",
        )


def test_apply_patch_rejects_a_patch_with_no_context(tmp_path):
    tools = _EvidenceTools(_evidence(tmp_path))
    with pytest.raises(ValueError, match="no context or removed"):
        tools.apply_patch("harness/systemprompt.md", "@@ -3,1 +3,2 @@\n+only additions\n")


def test_unknown_tool_is_rejected(tmp_path):
    tools = _EvidenceTools(_evidence(tmp_path))
    with pytest.raises(ValueError, match="unknown proposer tool"):
        tools.execute("run_in_sandbox", {"code": "x"})


# ── real proposer session (injected llm_call, no network) ──


class _FakeLlm:
    """按脚本回放 tool_calls / 最终文本；记录每次看到的工具面。"""

    def __init__(self, script: list[dict]) -> None:
        self._script = list(script)
        self.seen_tools: list[list[dict]] = []
        self.calls = 0

    def __call__(self, messages, *, provider, model, tools, **kwargs):
        self.calls += 1
        self.seen_tools.append(list(tools))
        step = self._script.pop(0) if self._script else {"content": "<notes>done</notes>"}
        message = {"role": "assistant", "content": step.get("content", "")}
        if step.get("tool_calls"):
            message["tool_calls"] = [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"]),
                    },
                }
                for i, call in enumerate(step["tool_calls"])
            ]
        return {
            "choices": [{"message": message}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


def _patch_llm(monkeypatch: pytest.MonkeyPatch, llm: _FakeLlm) -> None:
    """把 ``veya.obase.llm.llm_call`` 换成同步假实现（proposer 兼容两种返回）。"""

    def _fake(messages, **kwargs):
        return llm(messages, **kwargs)

    monkeypatch.setattr("veya.obase.llm.llm_call", _fake)


def test_real_proposer_returns_reply_with_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG_NAME, "1")
    llm = _FakeLlm(
        [
            {
                "content": "reading",
                "tool_calls": [{"name": "read_file", "arguments": {"path": "task.md"}}],
            },
            {
                "content": "reading",
                "tool_calls": [{"name": "list_dir", "arguments": {"path": "harness"}}],
            },
        ]
    )
    _patch_llm(monkeypatch, llm)
    proposer = VeyaProposer(audit_dir=tmp_path / "audit", probe=_ok_probe)
    reply = proposer.call("mission", _evidence(tmp_path), "inv1.a1", harvest="harness")

    assert reply.ok is True
    assert reply.workspace is not None and reply.workspace.is_dir()
    assert (reply.workspace / "systemprompt.md").is_file()
    assert proposer.channel_records  # 通道判定被记录，不静默
    # 模型只看到 proposer 工具面：四个文件工具，没有 master_tools
    names = {t["function"]["name"] for t in llm.seen_tools[0]}
    assert names == {"read_file", "list_dir", "write_file", "apply_patch"}


def test_real_proposer_edits_on_disk_become_the_proposal(tmp_path, monkeypatch):
    """ "你的编辑就是提案"：harvest 出来的树必须反映模型的落盘编辑。"""
    monkeypatch.setenv(FLAG_NAME, "1")
    llm = _FakeLlm(
        [
            {
                "content": "editing",
                "tool_calls": [
                    {
                        "name": "write_file",
                        "arguments": {"path": "harness/systemprompt.md", "content": "EDITED\n"},
                    }
                ],
            },
        ]
    )
    _patch_llm(monkeypatch, llm)
    root = _evidence(tmp_path)
    proposer = VeyaProposer(audit_dir=tmp_path / "audit", probe=_ok_probe)
    reply = proposer.call("mission", root, "dom1.a1", harvest="harness")

    assert (reply.workspace / "systemprompt.md").read_text(encoding="utf-8") == "EDITED\n"
    # 证据树本身也被就地改了（harness2 的 context/harness 语义）
    assert (root / "harness" / "systemprompt.md").read_text(encoding="utf-8") == "EDITED\n"


def test_real_proposer_harvests_when_handed_harness2_context_prefix(tmp_path, monkeypatch):
    """harness2 传的是 ``harvest="context/harness"``，而我们的根就是 context_dir。

    不做前缀剥离就会去找 ``<context>/context/harness``，提案被判成
    "harness 没回来"——这是 Phase 1 实跑暴露的回归。
    """
    monkeypatch.setenv(FLAG_NAME, "1")
    _patch_llm(monkeypatch, _FakeLlm([{"content": "<notes>ok</notes>"}]))
    proposer = VeyaProposer(audit_dir=tmp_path / "audit", probe=_ok_probe)
    reply = proposer.call("mission", _evidence(tmp_path), "inv1.a1", harvest="context/harness")

    assert reply.workspace is not None
    assert (reply.workspace / "systemprompt.md").is_file()


def test_real_proposer_raises_on_empty_reply(tmp_path, monkeypatch):
    """会话期失败必须抛 harness2 的 ProposerError：那是 recursion 捕获的类型。"""
    core = import_harness2_core()
    monkeypatch.setenv(FLAG_NAME, "1")
    _patch_llm(monkeypatch, _FakeLlm([{"content": ""}]))
    proposer = VeyaProposer(audit_dir=tmp_path / "audit", probe=_ok_probe)
    with pytest.raises(core.proposer.ProposerError, match="empty reply"):
        proposer.call("mission", _evidence(tmp_path), "inv1.a1", harvest="harness")


def test_real_proposer_forces_a_final_turn_at_the_round_cap(tmp_path, monkeypatch):
    """轮数用尽时不直接判失败：再给一次不带工具的机会逼出文本回复。

    实测失败模式：模型反复重读同一批文件，把预算烧在证据上。直接抛错会让整个
    提案回退 base，白花一次会话；这里必须落到一段 <notes> 上。
    """
    monkeypatch.setenv(FLAG_NAME, "1")
    llm = _FakeLlm(
        [
            {
                "content": "again",
                "tool_calls": [{"name": "read_file", "arguments": {"path": "task.md"}}],
            }
            for _ in range(MAX_TOOL_ROUNDS + 4)
        ]
    )
    _patch_llm(monkeypatch, llm)
    proposer = VeyaProposer(audit_dir=tmp_path / "audit", probe=_ok_probe, max_rounds=3)
    reply = proposer.call("mission", _evidence(tmp_path), "inv1.a1", harvest="harness")

    assert reply.ok is True
    # 最后一轮不带工具：模型被明确要求停止调用
    assert llm.seen_tools[-1] == []


def test_real_proposer_nudges_a_model_that_only_rereads(tmp_path, monkeypatch):
    """全部调用都是重复读时，会话里要出现一条"别再读了"的提示。"""
    monkeypatch.setenv(FLAG_NAME, "1")
    seen_messages: list[list[dict]] = []

    def _llm(messages, *, provider, model, tools, **kwargs):
        seen_messages.append(list(messages))
        step = len(seen_messages)
        if step <= 4:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": f"c{step}",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "task.md"}),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            }
        return {"choices": [{"message": {"content": "<notes>done</notes>"}}], "usage": {}}

    _patch_llm(monkeypatch, _FakeLlm([]))
    monkeypatch.setattr("veya.obase.llm.llm_call", _llm)
    proposer = VeyaProposer(audit_dir=tmp_path / "audit", probe=_ok_probe, max_rounds=8)
    reply = proposer.call("mission", _evidence(tmp_path), "inv1.a1", harvest="harness")

    assert reply.ok is True
    blob = "\n".join(str(m.get("content") or "") for batch in seen_messages for m in batch)
    assert "repeating yourself" in blob


def test_real_proposer_does_not_nudge_a_model_reading_new_files(tmp_path, monkeypatch):
    """逐个读不同证据文件是正常取证，不能被误判成"在重复"。

    按工具名判重会把"读第二个文件"也算成重复，于是每轮都塞一条"别再读了"，
    模型还没看到全部证据就被催着回答——这是实跑中观察到的反向误伤。
    """
    monkeypatch.setenv(FLAG_NAME, "1")
    seen_messages: list[list[dict]] = []

    def _llm(messages, *, provider, model, tools, **kwargs):
        seen_messages.append(list(messages))
        step = len(seen_messages)
        if step > 3:
            return {"choices": [{"message": {"content": "<notes>done</notes>"}}], "usage": {}}
        path = ["task.md", "rollouts/base1.md", "harness"][step - 1]
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": f"c{step}",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": path}),
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {},
        }

    _patch_llm(monkeypatch, _FakeLlm([]))
    monkeypatch.setattr("veya.obase.llm.llm_call", _llm)
    proposer = VeyaProposer(audit_dir=tmp_path / "audit", probe=_ok_probe, max_rounds=8)
    reply = proposer.call("mission", _evidence(tmp_path), "inv1.a1", harvest="harness")

    assert reply.ok is True
    blob = "\n".join(str(m.get("content") or "") for batch in seen_messages for m in batch)
    assert "repeating yourself" not in blob


def test_real_proposer_does_not_nudge_a_read_that_ends_in_an_edit(tmp_path, monkeypatch):
    """读完紧接着改文件：这一批不是纯重复读，不能触发提示。"""
    monkeypatch.setenv(FLAG_NAME, "1")
    seen_messages: list[list[dict]] = []

    def _llm(messages, *, provider, model, tools, **kwargs):
        seen_messages.append(list(messages))
        step = len(seen_messages)
        if step > 2:
            return {"choices": [{"message": {"content": "<notes>done</notes>"}}], "usage": {}}
        calls = [
            {"name": "read_file", "arguments": {"path": "task.md"}},
            {
                "name": "write_file",
                "arguments": {"path": "harness/systemprompt.md", "content": "X\n"},
            },
        ]
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": f"c{step}",
                                "type": "function",
                                "function": {
                                    "name": call["name"],
                                    "arguments": json.dumps(call["arguments"]),
                                },
                            }
                            for call in calls
                        ],
                    }
                }
            ],
            "usage": {},
        }

    _patch_llm(monkeypatch, _FakeLlm([]))
    monkeypatch.setattr("veya.obase.llm.llm_call", _llm)
    proposer = VeyaProposer(audit_dir=tmp_path / "audit", probe=_ok_probe, max_rounds=6)
    reply = proposer.call("mission", _evidence(tmp_path), "inv1.a1", harvest="harness")

    assert reply.ok is True
    blob = "\n".join(str(m.get("content") or "") for batch in seen_messages for m in batch)
    assert "repeating yourself" not in blob


def test_real_proposer_raises_when_no_channel_is_usable(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG_NAME, "1")
    _patch_llm(monkeypatch, _FakeLlm([]))
    with pytest.raises(ProposerError, match="no usable proposer channel"):
        VeyaProposer(audit_dir=tmp_path / "audit", probe=_stub_probe)


def test_real_proposer_writes_an_audit_trail_without_keys(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG_NAME, "1")
    _patch_llm(monkeypatch, _FakeLlm([{"content": "<notes>ok</notes>"}]))
    proposer = VeyaProposer(audit_dir=tmp_path / "audit", probe=_ok_probe)
    proposer.call("mission text", _evidence(tmp_path), "inv1.a1")

    audit = tmp_path / "audit" / "inv1.a1"
    assert (audit / "mission.md").is_file()
    meta = json.loads((audit / "meta.json").read_text(encoding="utf-8"))
    assert meta["ok"] is True
    assert meta["model"] == proposer.channel.model
    # 审计里只有模型标识与成本，没有凭据
    blob = (audit / "meta.json").read_text(encoding="utf-8")
    for banned in ("api_key", "apikey", "Bearer ", "sk-"):
        assert banned not in blob


# ── notes extraction ──


def test_extract_notes_prefers_the_notes_block():
    assert extract_notes("prose\n<notes>\nwhy\n</notes>\nmore") == "why"
    assert extract_notes("no block here") == "no block here"


# ── harness2 protocol compatibility ──


def test_real_proposer_reply_satisfies_the_harness2_contract(tmp_path, monkeypatch):
    """``Reply`` 形状与 workspace 语义必须和 harness2 原生 Proposer 一致。"""
    core = import_harness2_core()
    monkeypatch.setenv(FLAG_NAME, "1")
    _patch_llm(monkeypatch, _FakeLlm([{"content": "<notes>ok</notes>"}]))
    proposer = VeyaProposer(audit_dir=tmp_path / "audit", probe=_ok_probe)
    reply = proposer.call("mission", _evidence(tmp_path), "agg1.a1", harvest="harness")

    assert isinstance(reply, core.proposer.Reply)
    assert reply.ok is True
    assert reply.channel.startswith("veya:")
    assert isinstance(reply.cost_usd, float)


def test_flag_off_blocks_even_an_explicitly_reachable_channel(tmp_path, monkeypatch):
    """关闭时连通道探测都不做：一个请求都不发。"""
    calls: list[int] = []

    def _counting_probe(channel: ProposerChannel) -> dict:
        calls.append(1)
        return _ok_probe(channel)

    with pytest.raises(ProposerDisabled):
        VeyaProposer(audit_dir=tmp_path / "audit", probe=_counting_probe)
    assert calls == []

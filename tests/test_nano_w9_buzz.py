"""NANO-W9 tests: branch rooms, routine triggers, ACP members, audit chain,
search-with-receipts, the agent-native CLI template, and the 3O gate contract.

Every dependency that would otherwise touch ``~/.veya`` or a live service is
injected.  The few places that must use real code (the ACP stdio transport, the
event store, the routine dispatch) are exercised against throwaway processes and
temp directories only.

Run with:  venv/bin/python -m pytest -q tests/test_nano_w9_buzz.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.nano_w9.acp_members import (
    ACP_MEMBER_KIND,
    AcpMembers,
    acp_runner_id,
    detect_acp_candidates,
    is_acp_member,
)
from server.nano_w9.audit_chain import (
    AUDITED_TOPICS,
    CHAINED_TOPIC,
    audit_envelopes,
    audit_records,
    chained_topic,
    stamp_event,
    verify_audit_chain,
)
from server.nano_w9.branch_rooms import BranchRooms, is_branch_room, list_branch_rooms
from server.nano_w9.cli_gen import PHASE_FUNCTIONS, run_cli_gen
from server.nano_w9.flags import ALL_FLAGS, FeatureDisabled, feature_flags, require_flag
from server.nano_w9.routine_triggers import (
    ApprovalBridge,
    RoutineSignalRouter,
    extract_triggers,
    verify_webhook_signature,
)
from server.nano_w9.search_receipts import ReceiptSearch
from server.nano_w9.trajectory import Trajectory
from server.routes.nano_w9 import router as nano_w9_router
from veya.obase.acp_stdio import StdioJsonRpcError, StdioJsonRpcTransport
from veya.omodul.agent_native_scaffold import (
    AGENT_NATIVE_PHASES,
    CliGenAnalyzeConfig,
    CliGenAnalyzeInput,
    cli_gen_analyze,
    cli_gen_docs,
    phase_spec,
)
from veya.omodul.branch_room_sync import (
    BranchRoomOpenConfig,
    BranchRoomOpenInput,
    branch_room_name,
    branch_room_open,
    branch_room_publish,
    compute_fingerprint_for,
)
from veya.oprim.audit_chain import GENESIS_HASH, chain_head, compute_event_hash, verify_chain_links
from veya.oprim.receipt_query import RECEIPT_SOURCES, normalize_query, normalize_receipt
from veya.oskill.pure.receipt_rank_merge import (
    build_answer,
    build_citation,
    merge_receipts,
    rank_receipts,
    score_receipt,
)
from veya.oskill.pure.trajectory_assemble import (
    assemble_trajectory,
    build_preview_bundle,
    replay_states,
)
from veya.oskill.pure.trigger_match import (
    match_message_trigger,
    match_reaction_trigger,
    match_webhook_trigger,
    signal_matches_triggers,
)

# ==========================================================================
# fixtures
# ==========================================================================


@pytest.fixture
def w9_env(tmp_path, monkeypatch):
    """Point every store W9 touches at ``tmp_path`` and clear all flags."""
    from server import events as events_module

    for flag in ALL_FLAGS:
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("VEYA_EVENT_STORE_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots"))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bots" / "registry.json"))
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routines.json"))

    # ``server.events.event_store`` is a module-level singleton bound at import
    # time, so setting the env var alone would still read the real ~/.veya log.
    monkeypatch.setattr(
        events_module, "event_store", events_module.EventStore(tmp_path / "events.jsonl")
    )
    return tmp_path


@pytest.fixture
def enable(monkeypatch):
    """Turn NANO-W9 flags on for the duration of one test."""

    def _enable(*flags: str) -> None:
        for flag in flags:
            monkeypatch.setenv(flag, "1")

    return _enable


@pytest.fixture
def client(w9_env):
    """A FastAPI app carrying only the NANO-W9 router."""
    app = FastAPI()
    app.include_router(nano_w9_router)
    return TestClient(app)


class _EmptyRegistry:
    """A CLI-agent registry that reports nothing (keeps W1 detection out of unit tests)."""

    def list_all(self) -> list:
        return []

    def list(self) -> list:
        return []


class FakeGroup:
    """Minimal stand-in for a P2 ``GroupChat``."""

    def __init__(self, gid: str, name: str, bot_ids: list[str]) -> None:
        self.id = gid
        self.name = name
        self.bot_ids = tuple(bot_ids)


class RecordingGroups:
    """In-memory group store implementing the bits the omodul injects."""

    def __init__(self) -> None:
        self.groups: dict[str, FakeGroup] = {}
        self.messages: dict[str, list[dict]] = {}
        self._n = 0

    def create_group(self, name: str, bot_ids: list[str]) -> str:
        self._n += 1
        gid = f"grp_{self._n}"
        self.groups[gid] = FakeGroup(gid, name, list(bot_ids) or ["branch-bot"])
        self.messages[gid] = []
        return gid

    def list_groups(self) -> list[FakeGroup]:
        return list(self.groups.values())

    def post(self, group_id: str, role: str, text: str, bot_id: str) -> dict | None:
        if group_id not in self.groups:
            return None
        record = {"id": f"msg_{len(self.messages[group_id]) + 1}", "role": role, "text": text}
        self.messages[group_id].append(record)
        return record


# ==========================================================================
# element 1 — branch rooms
# ==========================================================================


class TestBranchRooms:
    def test_room_name_convention(self):
        assert branch_room_name("feat/nano-w9") == "branch:feat/nano-w9"
        assert branch_room_name("  ") == "branch:unknown"
        assert is_branch_room("branch:x") and not is_branch_room("general")
        assert list_branch_rooms(
            [FakeGroup("g1", "branch:a", ["b"]), FakeGroup("g2", "c", ["b"])]
        ) == [{"id": "g1", "name": "branch:a", "bot_ids": ["b"]}]

    async def test_open_creates_then_reuses(self, w9_env, enable):
        enable("VEYA_BRANCH_ROOMS")
        groups = RecordingGroups()
        facade = BranchRooms(
            create_group=groups.create_group,
            list_groups=groups.list_groups,
            post_message=groups.post,
        )
        first = await facade.open_room("feat/x", bot_ids=["b1"])
        assert first["status"] == "completed"
        assert first["created"] is True
        assert first["group_id"] == "grp_1"

        second = await facade.open_room("feat/x", bot_ids=["b1"])
        assert second["created"] is False
        assert second["group_id"] == "grp_1"
        assert len(groups.groups) == 1

    async def test_open_rejects_empty_branch(self, w9_env, enable):
        enable("VEYA_BRANCH_ROOMS")
        facade = BranchRooms(
            create_group=lambda *a: "g", list_groups=lambda: [], post_message=lambda *a: None
        )
        result = await facade.open_room("   ")
        assert result["status"] == "failed"
        assert result["error"]["code"] == "INVALID_INPUT"

    async def test_publish_posts_every_section(self, w9_env, enable):
        enable("VEYA_BRANCH_ROOMS")
        groups = RecordingGroups()
        facade = BranchRooms(
            create_group=groups.create_group,
            list_groups=groups.list_groups,
            post_message=groups.post,
            fetch_checks=lambda b, p: ["lint", "unit"],
            fetch_commits=lambda p, b, n: [{"sha": "abcdef1234", "subject": "feat: w9"}],
            fetch_annotations=lambda pr, g: [
                {"id": "an1", "file": "a.py", "line": 3, "body": "rename this"}
            ],
            llm_caller=lambda prompt: {"output": "AI: looks fine", "cost_usd": 0.02},
        )
        room = await facade.open_room("feat/x")
        result = await facade.publish(
            branch="feat/x",
            group_id=room["group_id"],
            repo_path=str(w9_env),
            goal_id="g1",
            merge_decision="squash",
        )
        assert result["status"] == "completed"
        assert set(result["posted"]) == {
            "commits",
            "ci",
            "annotations",
            "ai_review",
            "merge_decision",
        }
        assert result["skipped"] == []
        assert result["cost_usd"] == pytest.approx(0.02)
        texts = [m["text"] for m in groups.messages[room["group_id"]]]
        assert any("abcdef12" in t for t in texts)
        assert any("rename this" in t for t in texts)
        assert any("Merge decision: squash" in t for t in texts)
        assert result["citations"], "every posted section must cite a source"

    async def test_publish_skips_missing_sources_without_failing(self, w9_env, enable):
        enable("VEYA_BRANCH_ROOMS")
        groups = RecordingGroups()
        facade = BranchRooms(
            create_group=groups.create_group,
            list_groups=groups.list_groups,
            post_message=groups.post,
        )
        room = await facade.open_room("feat/y")
        result = await facade.publish(branch="feat/y", group_id=room["group_id"])
        assert result["status"] == "completed"
        assert set(result["skipped"]) == {
            "commits",
            "ci",
            "annotations",
            "ai_review",
            "merge_decision",
        }
        assert result["posted"] == []

    def test_fingerprint_is_stable_and_scoping(self):
        cfg = BranchRoomOpenConfig()
        first = compute_fingerprint_for(cfg, BranchRoomOpenInput(branch="feat/x", repo_path="/r"))
        again = compute_fingerprint_for(cfg, BranchRoomOpenInput(branch="feat/x", repo_path="/r"))
        other = compute_fingerprint_for(cfg, BranchRoomOpenInput(branch="feat/y", repo_path="/r"))
        assert first and first == again
        assert first != other


# ==========================================================================
# element 2 — event-triggered routines
# ==========================================================================


class _Routine:
    def __init__(self, rid: str, triggers: list[dict], enabled: bool = True) -> None:
        self.routine_id = rid
        self.triggers = triggers
        self.enabled = enabled
        self.bot_id = "default"


class TestRoutineTriggers:
    def test_extract_triggers_accepts_every_shape(self):
        assert (
            extract_triggers(_Routine("r", [{"kind": "reaction", "reaction": "x"}]))[0]["kind"]
            == "reaction"
        )
        assert (
            extract_triggers({"triggers": [{"type": "webhook", "event": "push"}]})[0]["kind"]
            == "webhook"
        )
        assert extract_triggers({"triggers": [{"kind": "nonsense"}]}) == []
        assert extract_triggers({"nope": 1}) == []

    def test_pure_matchers(self):
        assert match_message_trigger(
            {"kind": "message", "text": "hey @ci"}, {"kind": "message", "mentions": ["@ci"]}
        )
        assert not match_message_trigger(
            {"kind": "message", "text": "hey"}, {"kind": "message", "mentions": ["@ci"]}
        )
        assert match_message_trigger(
            {"kind": "message", "text": "release v2"},
            {"kind": "message", "regex": r"^release v\d+$"},
        )
        # An invalid regex never matches and never raises.
        assert not match_message_trigger(
            {"kind": "message", "text": "x"}, {"kind": "message", "regex": "([a-"}
        )
        # A trigger with no condition must never match-all.
        assert not match_message_trigger({"kind": "message", "text": "x"}, {"kind": "message"})

        assert match_reaction_trigger(
            {"kind": "reaction", "reaction": "👍", "message_author": "bot"},
            {"kind": "reaction", "reaction": "👍", "on_message_author": "bot"},
        )
        assert not match_reaction_trigger(
            {"kind": "reaction", "reaction": "👍", "message_author": "human"},
            {"kind": "reaction", "reaction": "👍", "on_message_author": "bot"},
        )

        # An unverified webhook is fail-closed.
        assert match_webhook_trigger(
            {"kind": "webhook", "event": "push", "verified": True},
            {"kind": "webhook", "event": "push"},
        )
        assert not match_webhook_trigger(
            {"kind": "webhook", "event": "push", "verified": False},
            {"kind": "webhook", "event": "push"},
        )

        assert signal_matches_triggers(
            {"kind": "reaction", "reaction": "👍"},
            [{"kind": "message", "mentions": ["@x"]}, {"kind": "reaction", "reaction": "👍"}],
        ) == [1]
        assert signal_matches_triggers({"kind": "unknown"}, [{"kind": "reaction"}]) == []

    def test_webhook_signature_matches_obase(self):
        import obase.webhook as obase_webhook

        body = b'{"ref":"refs/tags/v1"}'
        secret = "s" * 32
        good = obase_webhook.sign_payload(payload=body, secret=secret)
        assert verify_webhook_signature(body, secret=secret, signature=good)
        assert not verify_webhook_signature(body, secret=secret, signature="0" * 64)
        assert not verify_webhook_signature(body, secret="short", signature=good)
        assert not verify_webhook_signature(body, secret=secret, signature=good, algo="md5")

    async def test_router_dispatches_through_injected_dispatch(self, w9_env, enable):
        enable("VEYA_ROUTINE_TRIGGERS")
        if True:
            seen: list[dict] = []
            router = RoutineSignalRouter(
                lookup=lambda: [_Routine("r1", [{"kind": "message", "regex": "^ship"}])],
                dispatch=lambda **kw: seen.append(kw) or {"handoffs": [1]},
            )
            result = await router.handle({"kind": "message", "text": "ship it"})
            assert result["status"] == "ok"
            assert result["dispatched"][0]["routine_id"] == "r1"
            assert seen and seen[0]["topic"] == "event.arrive"

    async def test_router_reaction_approval_goes_through_the_bridge(self, w9_env, enable):
        enable("VEYA_ROUTINE_TRIGGERS")
        if True:
            calls: list[tuple] = []

            async def approval(action, arguments):
                calls.append((action, arguments))
                return "granted"

            router = RoutineSignalRouter(
                lookup=lambda: [
                    _Routine("r1", [{"kind": "reaction", "reaction": "👍", "action": "approve"}])
                ],
                dispatch=lambda **kw: pytest.fail("approval trigger must not dispatch"),
                approval=ApprovalBridge(request_approval=approval),
            )
            result = await router.handle({"kind": "reaction", "reaction": "👍"})
            assert result["status"] == "ok"
            assert result["approvals"][0]["verdict"] == "granted"
            assert result["dispatched"] == []
            assert calls[0][0] == "routine.approve"

    async def test_router_no_match_is_not_an_error(self, w9_env, enable):
        enable("VEYA_ROUTINE_TRIGGERS")
        if True:
            router = RoutineSignalRouter(lookup=lambda: [_Routine("r1", [{"kind": "message"}])])
            assert (await router.handle({"kind": "message", "text": "x"}))["status"] == "no_match"

    async def test_router_is_flag_gated(self, w9_env):
        router = RoutineSignalRouter(lookup=lambda: [])
        with pytest.raises(FeatureDisabled) as exc:
            await router.handle({"kind": "message", "text": "x"})
        assert exc.value.flag == "VEYA_ROUTINE_TRIGGERS"


# ==========================================================================
# element 3 — ACP members (+ the stdio transport single-source guard)
# ==========================================================================

_FAKE_ACP = """import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        continue
    if method == "session/new":
        out = {"sessionId": "s-1"}
    elif method == "task/start":
        print(json.dumps({"jsonrpc": "2.0", "method": "task/event",
                          "params": {"event": {"type": "text", "content": "acp says hi"}}}),
              flush=True)
        print(json.dumps({"jsonrpc": "2.0", "method": "task/event",
                          "params": {"event": {"type": "task/end"}}}), flush=True)
        out = {"taskId": msg["params"]["taskId"]}
    elif method == "session/init":
        out = {"ok": True}
    else:
        out = {}
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": out}), flush=True)
"""


class TestAcpMembers:
    def test_runner_id_and_detection(self):
        assert acp_runner_id("goose") == f"{ACP_MEMBER_KIND}:goose"
        assert is_acp_member({"runner_id": "acp:b1"})
        assert not is_acp_member({"runner_id": "local"})
        found = detect_acp_candidates(
            backends=[{"name": "goose", "kind": "acp", "command": ["goose"]}],
            cli_registry=_EmptyRegistry(),
        )
        assert [c["backend_id"] for c in found] == ["goose"]
        assert found[0]["source"] == "backend_store"
        assert detect_acp_candidates(backends=[], cli_registry=_EmptyRegistry()) == []

    def test_detection_tolerates_a_module(self):
        assert isinstance(detect_acp_candidates(backends=None, cli_registry=None), list)

    def test_register_and_list(self, w9_env, enable):
        from server.goal_run.bot_identity import BotRegistry

        enable("VEYA_ACP_MEMBERS")
        registry = BotRegistry()
        facade = AcpMembers(bot_registry=registry, backends=[], cli_registry=_EmptyRegistry())
        assert facade.register_member("goose")["error"]["code"] == "INVALID_INPUT"
        assert facade.register_member("goose", owner_id="u1")["status"] == "ok"
        assert [m["bot_id"] for m in facade.members()] == ["acp:goose"]
        assert (
            facade.register_member("goose", owner_id="u1")["error"]["code"] == "ALREADY_REGISTERED"
        )

    def test_flag_gating(self, w9_env):
        from server.goal_run.bot_identity import BotRegistry

        facade = AcpMembers(bot_registry=BotRegistry(), cli_registry=_EmptyRegistry())
        with pytest.raises(FeatureDisabled):
            facade.members()
        with pytest.raises(FeatureDisabled):
            facade.candidates()

    async def test_turn_is_forwarded_through_goal_run(self, w9_env, enable):
        from server.goal_run.bot_identity import BotRegistry

        enable("VEYA_ACP_MEMBERS")
        seen: list[tuple] = []

        async def goal_runner(root, objective, wait=False):
            seen.append((root, objective, wait))
            return {"goal_id": "g-1"}

        facade = AcpMembers(
            bot_registry=BotRegistry(), goal_runner=goal_runner, project_root=str(w9_env)
        )
        member = acp_runner_id("goose")
        bad = await facade.forward_turn(member_id="local", prompt="x")
        assert bad["error"]["code"] == "NOT_ACP_MEMBER"
        empty = await facade.forward_turn(member_id=member, prompt="  ")
        assert empty["error"]["code"] == "INVALID_INPUT"

        good = await facade.forward_turn(member_id=member, prompt="review the diff")
        assert good["status"] == "ok"
        assert good["goal_id"] == "g-1"
        assert seen and "review the diff" in seen[0][1]


class TestAcpStdioSingleSource:
    """§1.4 guard: the stdio JSON-RPC transport has exactly one implementation."""

    def test_acp_backend_delegates_to_the_obase_transport(self):
        from server.acp_client import ACPBackend

        backend = ACPBackend(["does-not-matter"])
        assert isinstance(backend.transport, StdioJsonRpcTransport)
        assert isinstance(backend._transport, StdioJsonRpcTransport)
        # The ACP layer must not carry its own process/IO machinery any more.
        source = Path(ACPBackend.__module__.replace(".", "/") + ".py").read_text(encoding="utf-8")
        assert "asyncio.create_subprocess_exec" not in source
        assert "from veya.obase.acp_stdio import" in source

    async def test_transport_round_trip_against_a_fake_agent(self, tmp_path):
        agent = tmp_path / "fake_acp.py"
        agent.write_text(_FAKE_ACP, encoding="utf-8")
        notes: list[dict] = []
        transport = StdioJsonRpcTransport(
            [sys.executable, str(agent)], on_notification=notes.append, name="fake"
        )
        try:
            result = await transport.request("session/new", {"metadata": {}})
            assert result["sessionId"] == "s-1"
            assert transport.running
            started = await transport.request(
                "task/start", {"sessionId": "s-1", "prompt": "hi", "taskId": "t-1"}
            )
            assert started["taskId"] == "t-1"
        finally:
            await transport.close()
        assert notes, "server notifications must reach the injected callback"

    async def test_transport_surfaces_timeouts_and_exit(self, tmp_path):
        agent = tmp_path / "silent.py"
        agent.write_text("import sys\nsys.stdin.readline()\n", encoding="utf-8")
        transport = StdioJsonRpcTransport([sys.executable, str(agent)], timeout_s=0.4)
        try:
            with pytest.raises(StdioJsonRpcError):
                await transport.request("anything", {})
        finally:
            await transport.close()

        missing = StdioJsonRpcTransport(["definitely-not-a-real-binary-xyz"])
        with pytest.raises(StdioJsonRpcError):
            await missing.open()


# ==========================================================================
# element 4 — hash-chained audit
# ==========================================================================


class TestAuditChain:
    def test_chain_rule_is_deterministic_and_linked(self):
        def record(topic: str, index: int) -> dict:
            return {
                "source_topic": topic,
                "actor": "system",
                "payload": {"n": index},
                "chain_index": index,
            }

        first = compute_event_hash(record("a", 0))
        second = compute_event_hash(record("b", 1), prev_hash=first)
        assert len(first) == 64 and first != second
        records = [
            {**record("a", 0), "prev_hash": GENESIS_HASH, "hash": first},
            {**record("b", 1), "prev_hash": first, "hash": second},
        ]
        assert verify_chain_links(records)[0] is True
        assert chain_head(records) == second
        assert chain_head([]) == GENESIS_HASH

    def test_verification_detects_tampering(self):
        records = [
            {
                "source_topic": "a",
                "chain_index": 0,
                "prev_hash": GENESIS_HASH,
                "hash": compute_event_hash({"source_topic": "a", "chain_index": 0}),
            },
        ]
        records[0]["source_topic"] = "tampered"
        ok, reason, index = verify_chain_links(records)
        assert ok is False and index == 0 and "derived" in reason

    def test_chained_topic_mapping(self):
        assert chained_topic("git.event") == CHAINED_TOPIC
        assert chained_topic("chat.token") == "chat.token"
        assert "group.message" in AUDITED_TOPICS

    def test_stamp_and_verify_round_trip(self, w9_env):
        for index in range(3):
            stamp_event("git.event", {"n": index})
        result = verify_audit_chain()
        assert result["status"] == "ok"
        assert result["length"] == 3
        assert [r["chain_index"] for r in audit_records()] == [0, 1, 2]
        assert len(audit_envelopes()) == 3

    def test_tampering_with_a_stored_record_is_detected(self, w9_env):
        stamp_event("git.event", {"n": 0})
        stamp_event("git.event", {"n": 1})
        path = Path(w9_env / "events.jsonl")
        lines = path.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[0])
        tampered["payload"]["audit"]["payload"]["n"] = 99
        lines[0] = json.dumps(tampered, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = verify_audit_chain()
        assert result["status"] == "broken"
        assert result["ok"] is False
        assert result["broken_index"] == 0

    def test_unaudited_topics_pass_through_unchained(self, w9_env):
        envelope = stamp_event("chat.token", {"n": 1})
        assert "hash" not in envelope
        assert verify_audit_chain()["status"] == "empty"

    def test_verify_on_empty_store(self, w9_env):
        result = verify_audit_chain()
        assert result == {
            "status": "empty",
            "ok": True,
            "reason": "no chained records",
            "broken_index": -1,
            "length": 0,
            "head": GENESIS_HASH,
        }


# ==========================================================================
# element 5 — search with receipts
# ==========================================================================


class TestSearchReceipts:
    def test_receipt_shape_and_uri(self):
        assert RECEIPT_SOURCES == ("session", "goal_run", "group_message", "approval", "annotation")
        receipt = normalize_receipt("session", {"sid": "s1", "snippet": "hi"}, ts=5.0)
        assert set(receipt) == {"receipt_id", "source", "ref", "title", "text", "uri", "ts"}
        assert receipt["uri"] == "/sessions/s1"
        assert receipt["receipt_id"] == "session:s1"

    def test_query_normalisation_is_bounded(self):
        assert normalize_query("Fix The Flaky Test")["terms"] == ["fix", "the", "flaky", "test"]
        assert normalize_query("x", limit=9999)["limit"] == 100
        assert normalize_query("x", limit=-5)["limit"] == 1
        assert normalize_query("", limit="nope")["terms"] == []

    def test_ranking_is_deterministic_and_deduped(self):
        receipts = [
            {
                "receipt_id": "a",
                "source": "session",
                "ref": "s1",
                "title": "fix login",
                "text": "login bug",
            },
            {
                "receipt_id": "a",
                "source": "approval",
                "ref": "s1",
                "title": "fix login",
                "text": "login bug",
            },
            {
                "receipt_id": "b",
                "source": "group_message",
                "ref": "g1",
                "title": "chat",
                "text": "nothing",
            },
        ]
        merged = merge_receipts(receipts, terms=["login"])
        assert len(merged) == 1
        assert merged[0]["source"] == "approval"  # approvals outrank chat hearsay
        assert merged[0]["also_seen_in"] == ["session"]
        assert merged[0]["citation"]["uri"] == "/governance/approvals/s1"
        # Same input, same output.
        assert rank_receipts(receipts, terms=["login"]) == rank_receipts(receipts, terms=["login"])

    def test_citation_label_always_names_a_source(self):
        label = build_citation({"source": "session", "ref": "s1", "title": "t"})["label"]
        assert label == "[session] s1 — t"
        assert build_citation(None)["label"] == ""

    def test_answer_never_cites_nothing(self):
        empty = build_answer([], query="q")
        assert empty["cited"] is False
        cited = build_answer(
            [
                {
                    "source": "session",
                    "ref": "s1",
                    "title": "t",
                    "text": "x",
                    "citation": build_citation({"source": "session", "ref": "s1", "title": "t"}),
                }
            ],
            query="q",
        )
        assert cited["cited"] is True
        assert "[session]" in cited["answer"]

    def test_scoring_recency(self):
        fresh = {"source": "session", "title": "x", "text": "x", "ts": 1000}
        old = {"source": "session", "title": "x", "text": "x", "ts": 0}
        assert score_receipt(fresh, terms=["x"], now=1010) > score_receipt(
            old, terms=["x"], now=1010
        )

    def test_multi_source_answer_with_isolation(self):
        def sessions(query, *, limit, since, ref):
            return [{"ref": "s1", "title": "flaky login", "text": "login test flakes"}]

        def approvals(query, *, limit, since, ref):
            return [{"ref": "r1", "title": "login deploy", "text": "approved"}]

        def boom(query, *, limit, since, ref):
            raise RuntimeError("store down")

        search = ReceiptSearch(
            sources={"session": sessions, "approval": approvals, "annotation": boom},
            ref="anon-1234",
        )
        result = search.answer("login", limit=5)
        assert result["status"] == "partial"
        assert result["cited"] is True
        assert {c["source"] for c in result["receipts"]} == {"session", "approval"}
        assert result["errors"][0]["source"] == "annotation"
        assert all(c["citation"]["label"].startswith("[") for c in result["receipts"])

    def test_no_match_is_explicit(self):
        search = ReceiptSearch(sources={})
        result = search.answer("nothing at all")
        assert result["cited"] is False
        assert result["receipts"] == []
        assert "No prior record" in result["answer"]


# ==========================================================================
# addendum E — preview + trajectory
# ==========================================================================


class TestTrajectory:
    def test_pure_assembly_orders_by_step_then_ts(self):
        entries = assemble_trajectory(
            [{"step": 2, "command": "b"}, {"step": 1, "command": "a"}, {"step": 1, "tool": "c"}]
        )
        assert [e["command"] for e in entries] == ["a", "c", "b"]
        assert assemble_trajectory([{"step": 1}]) == []
        assert assemble_trajectory(["nonsense"]) == []

    def test_preview_bundle_embeds_its_trajectory_entry(self):
        bundle = build_preview_bundle(step=1, command="pnpm build", artifact_ref="a.js")
        assert bundle["trajectory"]["command"] == "pnpm build"
        assert bundle["kind"] == "file"

    def test_replay_states_accumulate_artifacts(self):
        states = replay_states(
            [
                {"step": 1, "command": "build", "artifact_ref": "a.js"},
                {"step": 2, "command": "test", "artifact_ref": "log.txt"},
                {"step": 3, "command": "package", "artifact_ref": "a.js"},
            ]
        )
        assert states[0]["artifacts_so_far"] == ["a.js"]
        assert states[2]["artifacts_so_far"] == ["a.js", "log.txt"]
        assert states[0]["status"] == "completed"

    def test_record_lands_on_the_existing_event_log(self, w9_env):
        from server.events import event_store

        trajectory = Trajectory()
        assert (
            trajectory.record(step=1, command="pnpm build", artifact_ref="a.js", goal_id="g1")[
                "status"
            ]
            == "ok"
        )
        stored = event_store.read_all(topics={"trajectory.recorded"})
        assert stored and stored[0]["payload"]["command"] == "pnpm build"

        replay = trajectory.replay("g1")
        assert replay["step_count"] == 1
        assert replay["artifacts"] == ["a.js"]

    def test_writer_failure_is_reported_not_raised(self):
        def boom(*args, **kwargs):
            raise RuntimeError("store down")

        result = Trajectory(append_event=boom).record(step=1, command="x")
        assert result["status"] == "error"
        assert result["error"]["code"] == "RuntimeError"

    def test_replay_of_an_unknown_goal_is_empty_not_an_error(self, w9_env):
        assert Trajectory().replay("nope")["step_count"] == 0


# ==========================================================================
# addendum F — agent-native CLI generation template
# ==========================================================================


class TestAgentNativeTemplate:
    def test_seven_phases_are_declared_and_bound(self):
        assert AGENT_NATIVE_PHASES == (
            "analyze",
            "design",
            "implement",
            "test_plan",
            "tests",
            "docs",
            "package",
        )
        assert set(PHASE_FUNCTIONS) == set(AGENT_NATIVE_PHASES)
        for phase in AGENT_NATIVE_PHASES:
            spec = phase_spec(phase)
            assert spec["phase"] == phase
            assert spec["title"] and spec["produces"] and spec["acceptance"]
            assert spec["experimental"] is True
        assert phase_spec("nope")["acceptance"]

    def test_default_model_is_the_strongest_canonical_proxy(self):
        from server.nano_w9.cli_gen import default_model

        assert default_model() == "veya1.2"

    async def test_flag_off_returns_disabled_without_running(self, w9_env):
        calls: list[str] = []
        result = await run_cli_gen(
            repo_ref="/tmp/x", executor=lambda ctx: calls.append(ctx["phase"])
        )
        assert result["status"] == "disabled"
        assert result["error"]["code"] == "FEATURE_DISABLED"
        assert calls == []
        assert result["experimental"] is True

    async def test_seven_phases_run_in_order_and_chain_artifacts(self, w9_env, enable):
        enable("VEYA_CLI_GEN")
        seen: list[str] = []
        carried: list[dict] = []

        def executor(context):
            seen.append(context["phase"])
            carried.append(dict(context["carried"]))
            return {"produced": context["phase"], "cost_usd": 0.01}

        result = await run_cli_gen(repo_ref="/tmp/repo", executor=executor)
        assert result["status"] == "completed"
        assert seen == list(AGENT_NATIVE_PHASES)
        assert result["phases"] == list(AGENT_NATIVE_PHASES)
        assert result["experimental"] is True
        assert result["cost_usd"] == pytest.approx(0.07)
        # Phase 2 sees phase 1's artifact; later phases see earlier ones.
        assert carried[0] == {}
        assert carried[1].get("produced") == "analyze"
        assert carried[6].get("produced") == "docs"  # package sees everything before it

    async def test_a_failing_phase_is_reported_not_raised(self, w9_env, enable):
        enable("VEYA_CLI_GEN")

        def executor(context):
            if context["phase"] == "tests":
                raise RuntimeError("model unavailable")
            return {"produced": context["phase"]}

        result = await run_cli_gen(repo_ref="/tmp/repo", executor=executor)
        assert result["status"] == "partial"
        assert result["error"]["code"] == "RuntimeError"
        assert len(result["results"]) == 7

    async def test_missing_repo_ref_fails_the_phase(self, w9_env, enable):
        enable("VEYA_CLI_GEN")
        result = await run_cli_gen(repo_ref="", executor=lambda ctx: {})
        assert result["status"] in {"partial", "failed"}
        assert any((r.get("error") or {}).get("code") == "INVALID_INPUT" for r in result["results"])

    async def test_each_phase_omodul_never_raises(self, w9_env):

        cfg = CliGenAnalyzeConfig(executor=None)
        result = await cli_gen_analyze(cfg, CliGenAnalyzeInput(repo_ref="/tmp/x"), None)
        assert result["status"] == "completed"
        assert result["artifacts"]
        docs = await cli_gen_docs(cfg, CliGenAnalyzeInput(repo_ref="/tmp/x"), None)
        assert docs["phase"] == "docs"

    async def test_a_raising_executor_is_caught_by_the_phase(self, w9_env):

        def boom(context):
            raise ValueError("nope")

        result = await cli_gen_docs(
            CliGenAnalyzeConfig(executor=boom), CliGenAnalyzeInput(repo_ref="/tmp/x"), None
        )
        assert result["status"] == "failed"
        assert result["error"]["code"] == "ValueError"

    async def test_on_step_callback_failure_does_not_break_the_flow(self, w9_env, enable):
        enable("VEYA_CLI_GEN")

        def bad_step(_payload):
            raise RuntimeError("callback exploded")

        result = await run_cli_gen(
            repo_ref="/tmp/x", executor=lambda ctx: {"ok": True}, on_step=bad_step
        )
        assert result["status"] == "completed"


# ==========================================================================
# flags + HTTP surface
# ==========================================================================


class TestFlagsAndRoutes:
    def test_every_flag_defaults_off(self, w9_env):
        assert feature_flags() == ALL_FLAGS
        assert not any(ALL_FLAGS.values())
        with pytest.raises(FeatureDisabled):
            require_flag("VEYA_BRANCH_ROOMS")

    def test_flag_parsing(self, monkeypatch):
        from server.nano_w9.flags import flag_enabled

        for value, expected in [
            ("1", True),
            ("on", True),
            ("true", True),
            ("0", False),
            ("off", False),
            ("", False),
            ("no", False),
        ]:
            monkeypatch.setenv("VEYA_BRANCH_ROOMS", value)
            assert flag_enabled("VEYA_BRANCH_ROOMS") is expected, value
        monkeypatch.delenv("VEYA_BRANCH_ROOMS")
        assert flag_enabled("VEYA_BRANCH_ROOMS") is False
        assert flag_enabled("VEYA_BRANCH_ROOMS", default=True) is True

    def test_feature_disabled_carries_the_flag(self):
        assert FeatureDisabled("VEYA_CLI_GEN").as_error() == {
            "code": "FEATURE_DISABLED",
            "flag": "VEYA_CLI_GEN",
        }

    def test_flags_endpoint_is_always_open(self, client):
        response = client.get("/api/v1/nano-w9/flags")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert set(response.json()["flags"]) == set(ALL_FLAGS)

    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("post", "/api/v1/nano-w9/branch-rooms/open", {"branch": "feat/x"}),
            (
                "post",
                "/api/v1/nano-w9/branch-rooms/publish",
                {"branch": "feat/x", "group_id": "g1"},
            ),
            ("get", "/api/v1/nano-w9/branch-rooms", None),
            ("post", "/api/v1/nano-w9/signals", {"kind": "message", "text": "x"}),
            ("get", "/api/v1/nano-w9/acp/members", None),
            ("post", "/api/v1/nano-w9/acp/members", {"backend_id": "goose", "owner_id": "u"}),
            ("post", "/api/v1/nano-w9/acp/turn", {"member_id": "acp:g", "prompt": "x"}),
            ("get", "/api/v1/nano-w9/audit/verify", None),
            ("post", "/api/v1/nano-w9/cli-gen", {"repo_ref": "/tmp/x"}),
        ],
    )
    def test_gated_routes_404_when_the_flag_is_off(self, client, method, path, payload):
        response = (
            getattr(client, method)(path, json=payload)
            if payload
            else getattr(client, method)(path)
        )
        assert response.status_code == 404, path
        assert response.json()["detail"]["code"] == "FEATURE_DISABLED"

    def test_audit_verify_route_when_enabled(self, client, w9_env, enable):
        from server.nano_w9.audit_chain import stamp_event

        enable("VEYA_AUDIT_CHAIN")
        stamp_event("git.event", {"n": 1})
        response = client.get("/api/v1/nano-w9/audit/verify")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["length"] == 1

    def test_branch_room_routes_when_enabled(self, client, w9_env, enable):
        enable("VEYA_BRANCH_ROOMS")
        opened = client.post("/api/v1/nano-w9/branch-rooms/open", json={"branch": "feat/w9"})
        assert opened.status_code == 200, opened.text
        group_id = opened.json()["group_id"]
        published = client.post(
            "/api/v1/nano-w9/branch-rooms/publish",
            json={"branch": "feat/w9", "group_id": group_id, "merge_decision": "squash"},
        )
        assert published.status_code == 200, published.text
        assert "merge_decision" in published.json()["posted"]
        listed = client.get("/api/v1/nano-w9/branch-rooms")
        assert listed.json()["rooms"][0]["name"] == "branch:feat/w9"

    def test_search_and_trajectory_are_ungated(self, client, w9_env):
        search = client.post("/api/v1/nano-w9/search", json={"query": "anything"})
        assert search.status_code == 200
        assert search.json()["cited"] is False
        replay = client.get("/api/v1/nano-w9/trajectory/unknown-goal")
        assert replay.status_code == 200
        assert replay.json()["step_count"] == 0

    def test_trajectory_record_route(self, client, w9_env):
        response = client.post(
            "/api/v1/nano-w9/trajectory",
            json={"step": 1, "command": "pnpm build", "artifact_ref": "a.js", "goal_id": "g1"},
        )
        assert response.status_code == 200
        replay = client.get("/api/v1/nano-w9/trajectory/g1")
        assert replay.json()["artifacts"] == ["a.js"]

    def test_cli_gen_phases_route(self, client, w9_env):
        response = client.get("/api/v1/nano-w9/cli-gen/phases")
        assert response.status_code == 200
        assert len(response.json()["phases"]) == 7
        assert response.json()["experimental"] is True

    def test_search_pseudonymizes_the_caller(self, w9_env):
        from server.routes.nano_w9 import _subject_ref

        first = _subject_ref({"user_id": "alice"})
        again = _subject_ref({"user_id": "alice"})
        other = _subject_ref({"user_id": "bob"})
        assert first == again
        assert first != other
        assert first != "alice"
        assert len(first) == 24
        assert _subject_ref(None) == "anonymous"


# ==========================================================================
# 3O contract — the layers this work must not violate
# ==========================================================================


class TestThreeOLayerContract:
    def test_oskill_pure_elements_stay_pure(self):
        """A 3O-PURE module may not import IO or non-deterministic sources."""
        import ast

        banned = {"os", "subprocess", "time", "random", "secrets", "uuid", "socket", "httpx"}
        for module in ("trigger_match", "receipt_rank_merge", "trajectory_assemble"):
            path = Path("veya/oskill/pure") / f"{module}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            assert ast.get_docstring(tree).startswith("3O-PURE — ")
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    imported.add(node.module.split(".")[0])
            assert not (imported & banned), f"{module} imports {imported & banned}"
            assert "__all__" in path.read_text(encoding="utf-8")

    def test_oprim_exports_the_chain_atoms(self):
        import veya.oprim as oprim

        for name in (
            "compute_event_hash",
            "chain_head",
            "verify_chain_links",
            "normalize_receipt",
            "normalize_query",
            "receipt_uri",
        ):
            assert hasattr(oprim, name), name

    def test_branch_room_omodul_declares_its_pillars(self):
        from veya.omodul.branch_room_sync import BranchRoomOpenConfig, BranchRoomPublishConfig

        assert {"fingerprint", "decision_trail"} <= set(
            BranchRoomOpenConfig._enabled_pillars  # type: ignore[attr-defined]
        )
        assert {"fingerprint", "decision_trail", "cost"} <= set(
            BranchRoomPublishConfig._enabled_pillars  # type: ignore[attr-defined]
        )

    def test_omodul_signature_is_the_standard_three(self):
        import inspect

        for fn in (branch_room_open, branch_room_publish):
            positional = [
                p
                for p in inspect.signature(fn).parameters.values()
                if p.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            ]
            assert [p.name for p in positional] == ["config", "input_data", "output_dir"], fn

    def test_omodul_never_imports_a_sibling_omodul(self):
        import ast

        path = Path("veya/omodul/branch_room_sync.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        siblings = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("veya.omodul.")
        }
        assert siblings == set(), f"omodul must not import a sibling: {siblings}"

    def test_three_o_layers_do_not_import_the_business_root(self):
        import ast

        for path in sorted(Path("veya").glob("*/*.py")):
            layer = path.parent.name
            if layer not in {"oprim", "oskill", "omodul", "obase", "oservi"}:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    assert root not in {"server", "agents", "cli", "tools"}, (
                        f"{path}: {node.module}"
                    )

    def test_no_new_model_callable_tool_was_registered(self):
        """HARD RULES: any new model-callable tool must be declared in the report.

        Scans the registry *source* rather than importing it: importing
        ``server.tool_registry`` pulls in the whole tool surface (and, in an
        environment where the 3O submodules are unmounted, fails for reasons
        unrelated to this change).
        """
        source = Path("server/tool_registry.py").read_text(encoding="utf-8")
        assert "nano_w9" not in source, "W9 must not add a model-callable tool"
        assert "branch_room" not in source
        assert "audit_chain" not in source


def test_w9_routes_reject_paths_outside_allowed_roots(tmp_path, monkeypatch):
    """Client-supplied repo/output paths must stay under the configured roots."""
    import pytest
    from fastapi import HTTPException

    from server.routes import nano_w9 as routes

    monkeypatch.setenv("VEYA_ALLOWED_REPO_ROOTS", str(tmp_path))
    inside = tmp_path / "repo"
    inside.mkdir()
    assert routes._safe_path(str(inside), "repo_path") == str(inside)
    assert routes._safe_path("", "repo_path") == ""
    with pytest.raises(HTTPException) as exc:
        routes._safe_path("/etc", "repo_path")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        routes._safe_path(str(tmp_path / ".." / "elsewhere"), "output_dir")


def test_w9_router_requires_caller():
    """Every W9 route carries the auth dependency."""
    from server import auth as auth_mod
    from server.routes import nano_w9 as routes

    deps = [d.dependency for d in routes.router.dependencies]
    assert auth_mod.get_current_user in deps

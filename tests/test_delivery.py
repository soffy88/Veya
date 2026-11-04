"""Tests for the delivery pipeline: config, order, stages, card, reaper."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from server.events import EventStore
from server.goal_run.delivery import (
    load_delivery_config,
    on_delivery_approved,
    parse_order,
    reap_delivery_verifications,
    run_delivery_order,
)
from server.nano_w10.event_hub import EventHub, EventSeenStore, parse_rules_file

REPO = "acme/widgets"


# ── fixtures ───────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "tester")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture()
def head_sha(repo: Path) -> str:
    _git(repo, "checkout", "-b", "feature")
    sha = _commit(repo, "feature.py", "y = 2\n")
    _git(repo, "checkout", "main")
    return sha


@pytest.fixture()
def events(tmp_path: Path) -> EventStore:
    return EventStore(path=tmp_path / "events.jsonl")


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "delivery.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "enabled": True,
                "repo": REPO,
                "checks": [["true"]],
                "check_timeout_s": 60,
                "review_rounds_max": 3,
            }
        ),
        encoding="utf-8",
    )
    return path


class FakeApprovals:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.pending_rows: list[dict] = []
        self.store = self

    def list_approvals(self, **_kwargs):
        return list(self.pending_rows)

    async def request(self, *, tool, args, user_id, session_id="", actor="", reason="", delegate_policy=""):
        self.requests.append({"tool": tool, "args": args, "user_id": user_id, "reason": reason})
        return {"status": "completed", "approval_id": "appr-1"}


def _clean_llm(calls: dict):
    async def _call(*args, **kwargs):
        calls["n"] = calls.get("n", 0) + 1
        return {"choices": [{"message": {"content": json.dumps({"findings": []})}}]}

    return _call


def _envelope(head: str, mode: str = "deliver", summary: str = "add feature") -> dict:
    return {
        "type": f"github.pr.labeled.{mode}",
        "source": "github",
        "subject": f"{REPO}#7@{head}",
        "summary": summary,
    }


def _topics(store: EventStore) -> list[str]:
    return [e["topic"] for e in store.read_all()]


async def _run_order(
    envelope: dict,
    *,
    events: EventStore,
    config_file: Path,
    repo: Path,
    approvals: FakeApprovals | None = None,
    llm=None,
    flag: bool = True,
    record=None,
):
    emitted: list[tuple[str, dict]] = []

    def _record(topic: str, payload: dict) -> None:
        emitted.append((topic, payload))
        events.append({"topic": topic, "payload": payload, "task_id": payload.get("subject", "")})

    result = await run_delivery_order(
        envelope,
        {"name": "deliver", "to": "pipeline:delivery"},
        user={"user_id": "owner-1"},
        events=events,
        config_path=config_file,
        repo_path=repo,
        approval_service=approvals,
        llm_call_fn=llm,
        flag_enabled_fn=lambda: flag,
        record_event=record if record is not None else _record,
        incident_fn=lambda _subject, _payload: None,
    )
    return result, emitted


# ── config + order parsing ─────────────────────────────────────────────────


def test_config_missing_file(tmp_path: Path):
    config, reason, sha = load_delivery_config(tmp_path / "nope.json")
    assert config is None and reason == "no config file" and sha is None


def test_config_disabled_and_bad_shapes(tmp_path: Path):
    def _write(data) -> Path:
        path = tmp_path / "d.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    config, reason, _ = load_delivery_config(_write({"version": 1, "enabled": False, "repo": REPO, "checks": [["true"]]}))
    assert config is None and "disabled" in reason
    config, reason, _ = load_delivery_config(_write("{not json"))
    assert config is None and "JSON" in reason
    config, reason, _ = load_delivery_config(_write({"version": 2, "enabled": True, "repo": REPO, "checks": [["true"]]}))
    assert config is None and "version" in reason
    config, reason, _ = load_delivery_config(
        _write({"version": 1, "enabled": True, "repo": REPO, "checks": [["true"]], "surprise": 1})
    )
    assert config is None and "unknown keys" in reason
    config, reason, _ = load_delivery_config(
        _write({"version": 1, "enabled": True, "repo": REPO, "checks": [["rm", "-rf", "/"]]})
    )
    assert config is None and "checks invalid" in reason


def test_config_good_loads_with_sha(config_file: Path):
    config, reason, sha = load_delivery_config(config_file)
    assert config is not None and reason == "" and len(sha or "") == 64
    assert config["repo"] == REPO


def test_parse_order_variants(head_sha: str):
    mode, repo, pr, head = parse_order(_envelope(head_sha))
    assert (mode, repo, pr, head) == ("deliver", REPO, 7, head_sha)
    mode, _, _, _ = parse_order(_envelope(head_sha, mode="deliver-review"))
    assert mode == "deliver-review"
    with pytest.raises(Exception, match="not a delivery label"):
        parse_order({"type": "github.push", "subject": f"{REPO}#7@{head_sha}"})
    with pytest.raises(Exception, match="subject must be"):
        parse_order({"type": "github.pr.labeled.deliver", "subject": "nope"})


# ── happy path + modes ─────────────────────────────────────────────────────


async def test_happy_path_creates_card(events, config_file, repo, head_sha):
    approvals = FakeApprovals()
    calls: dict = {}
    result, emitted = await _run_order(
        _envelope(head_sha), events=events, config_file=config_file, repo=repo,
        approvals=approvals, llm=_clean_llm(calls),
    )
    assert result["status"] == "awaiting_approval", result
    assert result["approval_id"] == "appr-1"
    assert len(approvals.requests) == 1
    card = approvals.requests[0]
    assert card["tool"] == "delivery.merge" and card["user_id"] == "owner-1"
    assert card["args"]["subject"] == f"delivery:{REPO}#7@{head_sha}"
    assert calls["n"] == 2  # one review round, two axes
    topics = _topics(events)
    assert "review.completed" in topics and "checks.completed" in topics
    assert "delivery.card_created" in [t for t, _ in emitted]
    # No leftover worktree: only the main checkout remains.
    worktrees = _git(repo, "worktree", "list", "--porcelain")
    assert worktrees.count("worktree ") == 1


async def test_deliver_review_reports_without_card(events, config_file, repo, head_sha):
    approvals = FakeApprovals()
    result, _ = await _run_order(
        _envelope(head_sha, mode="deliver-review"), events=events, config_file=config_file,
        repo=repo, approvals=approvals, llm=_clean_llm({}),
    )
    assert result["status"] == "reported" and result["mode"] == "deliver-review"
    assert approvals.requests == []


async def test_flag_off_is_report_only(events, config_file, repo, head_sha):
    approvals = FakeApprovals()
    result, emitted = await _run_order(
        _envelope(head_sha), events=events, config_file=config_file, repo=repo,
        approvals=approvals, llm=_clean_llm({}), flag=False,
    )
    assert result["status"] == "reported" and result["report_only"] is True
    assert approvals.requests == []
    assert any(t == "delivery.reported" for t, _ in emitted)


async def test_second_run_reuses_review_round(events, config_file, repo, head_sha):
    approvals = FakeApprovals()
    calls: dict = {}
    llm = _clean_llm(calls)
    await _run_order(_envelope(head_sha), events=events, config_file=config_file, repo=repo, approvals=approvals, llm=llm)
    result, _ = await _run_order(
        _envelope(head_sha), events=events, config_file=config_file, repo=repo,
        approvals=approvals, llm=llm,
    )
    assert result["status"] == "awaiting_approval"
    assert calls["n"] == 2  # unchanged diff: no second model round


async def test_pending_card_is_not_duplicated(events, config_file, repo, head_sha):
    approvals = FakeApprovals()
    approvals.pending_rows.append(
        {"request_id": "appr-0", "tool_args": {"subject": f"delivery:{REPO}#7@{head_sha}", "diff_hash": "x"}}
    )
    # The pending row must carry the CURRENT diff hash to dedupe: run
    # once to learn it, then seed it and run again.
    calls: dict = {}
    first, _ = await _run_order(
        _envelope(head_sha), events=events, config_file=config_file, repo=repo,
        approvals=FakeApprovals(), llm=_clean_llm(calls),
    )
    approvals.pending_rows[0]["tool_args"]["diff_hash"] = first["diff_hash"]
    approvals.requests.clear()
    result, emitted = await _run_order(
        _envelope(head_sha), events=events, config_file=config_file, repo=repo,
        approvals=approvals, llm=_clean_llm({}),
    )
    assert result["status"] == "awaiting_approval"
    assert approvals.requests == []
    assert any(t == "delivery.card_exists" for t, _ in emitted)


# ── refusals ───────────────────────────────────────────────────────────────


async def test_refuses_when_head_absent(events, config_file, repo):
    result, emitted = await _run_order(
        _envelope("0" * 40), events=events, config_file=config_file, repo=repo,
        approvals=FakeApprovals(), llm=_clean_llm({}),
    )
    assert result["status"] == "refused" and "not present locally" in result["reason"]
    assert emitted[0][0] == "delivery.refused"


async def test_refuses_repo_mismatch(events, config_file, repo, head_sha):
    result, _ = await _run_order(
        _envelope(head_sha).copy() | {"subject": f"other/other#7@{head_sha}"},
        events=events, config_file=config_file, repo=repo,
        approvals=FakeApprovals(), llm=_clean_llm({}),
    )
    assert result["status"] == "refused" and "does not match" in result["reason"]


async def test_refuses_merge_conflict(events, config_file, repo):
    # Feature branch and main both add the same file differently.
    _git(repo, "checkout", "-b", "feature")
    feature = _commit(repo, "shared.py", "v = 1\n")
    _git(repo, "checkout", "main")
    (repo / "shared.py").write_text("v = 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "main moves")
    result, _ = await _run_order(
        _envelope(feature), events=events, config_file=config_file, repo=repo,
        approvals=FakeApprovals(), llm=_clean_llm({}),
    )
    assert result["status"] == "refused" and "does not merge cleanly" in result["reason"]


async def test_refuses_blocker_review(events, config_file, repo, head_sha):
    async def _blocker(*args, **kwargs):
        return {"choices": [{"message": {"content": json.dumps({"findings": [{"detail": "hardcoded secret", "severity": "blocker"}]})}}]}

    result, _ = await _run_order(
        _envelope(head_sha), events=events, config_file=config_file, repo=repo,
        approvals=FakeApprovals(), llm=_blocker,
    )
    assert result["status"] == "refused" and "review did not pass" in result["reason"]
    assert "checks.completed" not in _topics(events)  # checks never ran


async def test_refuses_failing_checks(events, tmp_path, repo, head_sha):
    bad_config = tmp_path / "bad.json"
    bad_config.write_text(
        json.dumps({"version": 1, "enabled": True, "repo": REPO, "checks": [["false"]]}),
        encoding="utf-8",
    )
    result, _ = await _run_order(
        _envelope(head_sha), events=events, config_file=bad_config, repo=repo,
        approvals=FakeApprovals(), llm=_clean_llm({}),
    )
    assert result["status"] == "refused" and "checks did not pass" in result["reason"]


async def test_refuses_without_user_principal(events, config_file, repo, head_sha):
    result = await run_delivery_order(
        _envelope(head_sha),
        {"name": "x", "to": "pipeline:delivery"},
        user={},
        events=events,
        config_path=config_file,
        repo_path=repo,
        record_event=lambda t, p: None,
    )
    assert result["status"] == "refused" and "no user principal" in result["reason"]


# ── approval hook + reaper ─────────────────────────────────────────────────


def test_on_delivery_approved_records_and_cards(tmp_path: Path):
    emitted: list[tuple[str, dict]] = []

    class _Attention:
        def __init__(self):
            self.items = []

        def create(self, **kwargs):
            self.items.append(kwargs)
            return {"id": "att-1"}

    attention = _Attention()
    record = {
        "tool": "delivery.merge",
        "request_id": "appr-1",
        "tool_args": {"subject": f"delivery:{REPO}#7@{'a' * 40}", "head": "a" * 40, "repo": REPO, "pr": 7},
    }
    out = on_delivery_approved(
        record, decision="allow",
        emit=lambda t, p: emitted.append((t, p)), attention_store=attention,
    )
    assert out["status"] == "approved"
    assert emitted[0][0] == "delivery.approved"
    assert len(attention.items) == 1
    assert attention.items[0]["kind"] == "escalation" and attention.items[0]["tell"] == "needed"
    assert "a" * 40 in attention.items[0]["question"]
    assert "no push" in attention.items[0]["question"]

    out = on_delivery_approved(
        record, decision="deny",
        emit=lambda t, p: emitted.append((t, p)), attention_store=attention,
    )
    assert out["status"] == "denied"
    assert emitted[-1][0] == "delivery.denied"
    assert len(attention.items) == 1  # no second card

    out = on_delivery_approved(
        {"tool": "bash", "request_id": "x", "tool_args": {}},
        decision="allow",
        emit=lambda t, p: emitted.append((t, p)), attention_store=attention,
    )
    assert out["status"] == "ignored"


async def test_verify_reaper_marks_deployed_once(events):
    head = "b" * 40
    subject = f"delivery:{REPO}#7@{head}"
    events.append({"topic": "delivery.approved", "payload": {"subject": subject, "head": head}})
    emitted: list[tuple[str, dict]] = []
    count = await reap_delivery_verifications(
        events=events,
        commit_reader=lambda: {"commit": head, "source": "commit-file"},
        emit=lambda t, p: emitted.append((t, p)),
    )
    assert count == 1 and emitted[0][0] == "delivery.verified"
    # Second run: already verified (event present in the store now).
    events.append({"topic": "delivery.verified", "payload": {"subject": subject, "head": head}})
    emitted.clear()
    count = await reap_delivery_verifications(
        events=events,
        commit_reader=lambda: {"commit": head, "source": "commit-file"},
        emit=lambda t, p: emitted.append((t, p)),
    )
    assert count == 0 and emitted == []


async def test_verify_reaper_silent_when_not_deployed(events):
    events.append(
        {"topic": "delivery.approved", "payload": {"subject": f"delivery:{REPO}#7@{'c' * 40}", "head": "c" * 40}}
    )
    emitted: list[tuple[str, dict]] = []
    count = await reap_delivery_verifications(
        events=events,
        commit_reader=lambda: {"commit": "d" * 40, "source": "commit-file"},
        emit=lambda t, p: emitted.append((t, p)),
    )
    assert count == 0 and emitted == []


# ── hub wiring: pipeline targets ───────────────────────────────────────────


def test_hub_loader_accepts_pipeline_target(tmp_path: Path):
    skills = tmp_path / "skills"
    skills.mkdir()
    rules = parse_rules_file(
        {"version": 1, "rules": [{"name": "deliver", "when": {"source": "github", "types": ["github.pr.labeled.deliver"]}, "to": "pipeline:delivery"}]},
        skills_dir=skills,
        bot_exists=lambda _bot: False,
    )
    assert len(rules) == 1 and rules[0]["to"] == "pipeline:delivery" and rules[0]["ask"] == ""
    with pytest.raises(ValueError, match="unknown pipeline"):
        parse_rules_file(
            {"version": 1, "rules": [{"name": "bad", "when": {"source": "github", "types": ["x"]}, "to": "pipeline:unknown"}]},
            skills_dir=skills,
            bot_exists=lambda _bot: False,
        )


async def test_hub_dispatches_to_pipeline(tmp_path: Path):
    seen: list[dict] = []

    async def _pipeline(envelope, rule, *, user, **_kw):
        seen.append({"envelope": envelope, "user": user})
        return {"status": "ok"}

    skills = tmp_path / "skills"
    skills.mkdir()
    rules_path = tmp_path / "event_rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [
                    {
                        "name": "deliver",
                        "when": {"source": "github", "types": ["github.pr.labeled.deliver"]},
                        "to": "pipeline:delivery",
                        "tell": "needed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    recorded: list[tuple[str, dict]] = []
    hub = EventHub(
        rules_path=str(rules_path),
        seen_store=EventSeenStore(str(tmp_path / "event_hub.db")),
        record_event=lambda t, p: recorded.append((t, p)),
        bot_exists=lambda _bid: False,
        skills_dir=skills,
        enabled_fn=lambda: True,
        pipelines={"pipeline:delivery": _pipeline},
    )
    out = await hub.ingest(
        {"source": "github", "type": "github.pr.labeled.deliver", "subject": "s", "summary": "x"},
        user={"user_id": "u1"},
    )
    assert out["status"] == "accepted"
    assert seen and seen[0]["user"] == {"user_id": "u1"}
    # Pipeline dispatch owns its notifications: no generic tell wake.
    assert all(t != "event.wake" for t, _ in recorded)

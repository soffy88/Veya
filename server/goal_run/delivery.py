"""Delivery pipeline (Autoloop graft item 3, adapted).

A GitHub ``deliver`` / ``deliver-review`` label event becomes a gated
delivery order: read-only review (3 rounds max) -> clean-env checks ->
merge gate -> human approval card.  The pipeline never merges, never
pushes, never deploys, and never restarts the server: an approved order
becomes an operator card with the exact manual steps; a startup reaper
later probes the D4 version endpoint and records ``delivery.verified``
when the deployed commit equals the order's head.

Boundaries, all fail-closed:

* Config comes ONLY from the deploy-time copy at
  ``$VEYA_DELIVERY_CONFIG`` (default ``~/.veya/delivery.json``), which the
  deploy procedure refreshes from ``ops/delivery.json`` on main.  The
  pipeline never reads the repo tree for its config, never fetches, and
  logs the config sha on every order.  Missing/disabled config refuses
  every order; a shipped ``ops/delivery.json`` with ``enabled: false``
  documents the schema and keeps the pipeline inert.
* The target repo is fixed by the config; the order's repo must match
  it.  Nothing is enabled for any repo until Soffy names it and flips
  ``enabled``.  The merge itself is local-only (no push) and stays
  manual: the server has no repo checkout, and auto-merge in-container
  is disabled by design.
* The envelope is pointer-only: type + subject + summary.  The subject
  carries ``owner/repo#PR@HEAD_SHA``; a head SHA that is not locally
  present is refused, never fetched.  The spec-axis review is therefore
  shallow (no PR body) and the standards axis carries the weight; that
  limit is stated, not hidden.
* ``VEYA_CI_DISCIPLINE`` off means report-only: review + checks still
  run and are recorded, but no approval card is ever created.
* Runner targets are not dispatchable (EH-3 loader rule); delivery
  orders arrive via the ``pipeline:delivery`` target, never via a bot
  turn, so no model sits in the routing loop.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from server.goal_run.merge_gate import merge_gate_passed
from server.goal_run.review_gate import MAX_REVIEW_ROUNDS, diff_hash_of, run_gated_review
from server.goal_run.sandbox_checks import record_checks, run_checks, validate_commands
from server.version_info import read_deployed_commit

logger = logging.getLogger("veya.delivery")

PIPELINE_TARGETS = {"pipeline:delivery"}

ORDER_TYPES = {
    "github.pr.labeled.deliver": "deliver",
    "github.pr.labeled.deliver-review": "deliver-review",
}

_SUBJECT_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)@([0-9a-f]{40})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_CONFIG_KEYS = {"version", "enabled", "repo", "checks", "check_timeout_s", "review_rounds_max"}

_APPROVE_DECISIONS = {"allow", "once", "session", "always"}

_DELIVERY_STANDARDS = """Delivery review standards:
1. No secrets, tokens, or credentials in the diff.
2. No destructive commands (rm -rf, DROP, mass delete) outside migrations.
3. No network calls to new external hosts without a stated reason.
4. Public behavior changes are named in the summary or the diff.
5. No unrelated drive-by changes bundled into the delivery."""


class DeliveryRefused(Exception):
    """A delivery order was refused; the reason is the message."""


# ── config (only from the deploy-time copy) ───────────────────────────────


def default_config_path() -> Path:
    override = os.environ.get("VEYA_DELIVERY_CONFIG")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~/.veya/delivery.json"))


def load_delivery_config(path: str | Path | None = None) -> tuple[dict[str, Any] | None, str, str | None]:
    """Return ``(config, reason, config_sha)``; config None means refuse.

    ``reason`` is "" on success and names the refusal otherwise.  The
    sha is over the raw file bytes, for per-order audit.
    """

    cfg_path = Path(path) if path is not None else default_config_path()
    try:
        raw = cfg_path.read_bytes()
    except OSError:
        return None, "no config file", None
    config_sha = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "config is not valid JSON", config_sha
    if not isinstance(data, dict):
        return None, "config must be a JSON object", config_sha
    unknown = set(data) - _CONFIG_KEYS
    if unknown:
        return None, f"config has unknown keys: {sorted(unknown)}", config_sha
    if data.get("version") != 1:
        return None, "config version must be 1", config_sha
    if not isinstance(data.get("enabled"), bool):
        return None, "config enabled must be a boolean", config_sha
    if not data["enabled"]:
        return None, "delivery disabled by config", config_sha
    repo = data.get("repo")
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        return None, "config repo must be owner/name", config_sha
    checks = data.get("checks")
    if not isinstance(checks, list) or not checks:
        return None, "config checks must be a non-empty list", config_sha
    try:
        validate_commands(checks)
    except ValueError as exc:
        return None, f"config checks invalid: {exc}", config_sha
    timeout_s = data.get("check_timeout_s", 600)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s <= 0:
        return None, "config check_timeout_s must be a positive integer", config_sha
    rounds = data.get("review_rounds_max", MAX_REVIEW_ROUNDS)
    if isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds <= MAX_REVIEW_ROUNDS:
        return None, f"config review_rounds_max must be 1..{MAX_REVIEW_ROUNDS}", config_sha
    return data, "", config_sha


# ── order parsing ──────────────────────────────────────────────────────────


def parse_order(envelope: dict[str, Any]) -> tuple[str, str, int, str]:
    """Return ``(mode, repo, pr_number, head_sha)`` or raise DeliveryRefused."""

    env_type = str(envelope.get("type") or "")
    mode = ORDER_TYPES.get(env_type)
    if mode is None:
        raise DeliveryRefused(f"not a delivery label event: {env_type!r}")
    subject = str(envelope.get("subject") or "")
    match = _SUBJECT_RE.match(subject)
    if match is None:
        raise DeliveryRefused(f"subject must be owner/repo#PR@HEAD_SHA, got {subject!r}")
    repo, pr, head = match.group(1), int(match.group(2)), match.group(3)
    return mode, repo, pr, head


# ── git helpers (local only: never fetch, never push) ──────────────────────


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }


def _git(
    repo: Path,
    *args: str,
    timeout: int = 60,
    runner: Any = None,
) -> subprocess.CompletedProcess[str]:
    if runner is not None:
        return runner(["git", *args], cwd=str(repo), timeout=timeout, env=_git_env())
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        timeout=timeout,
        env=_git_env(),
        capture_output=True,
        text=True,
    )


def _git_text(result: Any) -> str:
    stdout = getattr(result, "stdout", "") or ""
    return stdout if isinstance(stdout, str) else stdout.decode("utf-8", "replace")


def git_is_repo(repo: Path, runner: Any = None) -> bool:
    try:
        return _git(repo, "rev-parse", "--git-dir", runner=runner).returncode == 0
    except Exception:
        return False


def git_ref_exists(repo: Path, ref: str, runner: Any = None) -> bool:
    try:
        return _git(repo, "rev-parse", "--verify", "--quiet", ref, runner=runner).returncode == 0
    except Exception:
        return False


def git_head_is_commit(repo: Path, sha: str, runner: Any = None) -> bool:
    try:
        result = _git(repo, "cat-file", "-t", sha, runner=runner)
        return result.returncode == 0 and _git_text(result).strip() == "commit"
    except Exception:
        return False


def git_diff(repo: Path, base: str, head: str, runner: Any = None) -> str:
    result = _git(repo, "diff", "--no-color", f"{base}...{head}", timeout=120, runner=runner)
    if result.returncode != 0:
        raise DeliveryRefused(f"git diff failed: {_git_text(result).strip()[:200]}")
    return _git_text(result)


def git_mergeable(
    repo: Path,
    base: str,
    head: str,
    *,
    runner: Any = None,
    use_merge_tree: bool = True,
) -> tuple[bool, str]:
    """True when ``head`` merges cleanly onto ``base``.

    Primary: ``git merge-tree --write-tree`` (writes tree objects only,
    changes no ref).  Fallback: a detached worktree at ``base`` plus
    ``git merge --no-commit`` (aborted afterwards).  Either way the
    repo is left exactly as found.
    """

    if use_merge_tree:
        result = _git(repo, "merge-tree", "--write-tree", base, head, timeout=120, runner=runner)
        if result.returncode == 0:
            return True, ""
        if result.returncode == 1:
            detail = _git_text(result).strip().splitlines()
            return False, "; ".join(detail[:3])[:300]
        raise DeliveryRefused(f"git merge-tree failed: {_git_text(result).strip()[:200]}")
    worktree = Path(tempfile.mkdtemp(prefix="delivery-merge-"))
    worktree.rmdir()
    try:
        add = _git(repo, "worktree", "add", "--detach", str(worktree), base, timeout=120, runner=runner)
        if add.returncode != 0:
            raise DeliveryRefused(f"git worktree add failed: {_git_text(add).strip()[:200]}")
        merged = _git(worktree, "merge", "--no-commit", "--no-ff", head, timeout=120, runner=runner)
        _git(worktree, "merge", "--abort", timeout=60, runner=runner)
        if merged.returncode == 0:
            return True, ""
        return False, _git_text(merged).strip().splitlines()[0][:300] if _git_text(merged).strip() else "merge conflict"
    finally:
        _git(repo, "worktree", "remove", "--force", str(worktree), timeout=60, runner=runner)
        _git(repo, "worktree", "prune", timeout=60, runner=runner)


def _worktree_add(repo: Path, head: str, runner: Any = None) -> Path:
    worktree = Path(tempfile.mkdtemp(prefix="delivery-wt-"))
    worktree.rmdir()
    result = _git(repo, "worktree", "add", "--detach", str(worktree), head, timeout=120, runner=runner)
    if result.returncode != 0:
        raise DeliveryRefused(f"git worktree add failed: {_git_text(result).strip()[:200]}")
    return worktree


def _worktree_remove(repo: Path, worktree: Path, runner: Any = None) -> None:
    _git(repo, "worktree", "remove", "--force", str(worktree), timeout=60, runner=runner)
    _git(repo, "worktree", "prune", timeout=60, runner=runner)


# ── the pipeline ───────────────────────────────────────────────────────────


def _subject(repo: str, pr: int, head: str) -> str:
    return f"delivery:{repo}#{pr}@{head}"


async def run_delivery_order(
    envelope: dict[str, Any],
    rule: dict[str, Any],
    *,
    user: dict[str, Any],
    events: Any = None,
    config_path: str | Path | None = None,
    repo_path: str | Path | None = None,
    approval_service: Any = None,
    llm_call_fn: Any = None,
    executor: Any = None,
    git_runner: Any = None,
    flag_enabled_fn: Any = None,
    record_event: Any = None,
    incident_fn: Any = None,
) -> dict[str, Any]:
    """Run one delivery order end to end.  Never raises for a refusal.

    ``events`` is the read side (EventStore-like, ``read_all``); the
    write side is ``record_event(topic, payload)`` and defaults to the
    canonical event writer.  ``user`` must carry ``user_id``: the
    approval card is scoped to that principal, never invented.
    """

    from server.events import append_canonical_event

    def emit(topic: str, payload: dict[str, Any]) -> None:
        if record_event is not None:
            record_event(topic, payload)
        else:
            append_canonical_event(topic, payload, actor="delivery")

    def refuse(reason: str, **extra: Any) -> dict[str, Any]:
        emit("delivery.refused", {"reason": reason, **extra})
        return {"status": "refused", "reason": reason, **extra}

    user_id = str(user.get("user_id") or "")
    if not user_id:
        return refuse("no user principal for the approval card")

    if events is None:
        from server.events import EventStore

        events = EventStore()

    try:
        mode, repo, pr, head = parse_order(envelope)
    except DeliveryRefused as exc:
        return refuse(str(exc))
    subject = _subject(repo, pr, head)
    summary = str(envelope.get("summary") or "")[:200]

    config, reason, config_sha = load_delivery_config(config_path)
    if config is None:
        return refuse(reason, subject=subject, config_sha=config_sha)
    if repo != config["repo"]:
        return refuse(
            f"order repo {repo} does not match configured repo {config['repo']}",
            subject=subject,
            config_sha=config_sha,
        )

    repo_dir = Path(repo_path) if repo_path is not None else Path(os.environ.get("VEYA_DELIVERY_REPO_PATH") or "")
    if not str(repo_dir):
        return refuse("repo path not configured (VEYA_DELIVERY_REPO_PATH)", subject=subject, config_sha=config_sha)
    if not git_is_repo(repo_dir, runner=git_runner):
        return refuse(f"repo path is not a git repo: {repo_dir}", subject=subject, config_sha=config_sha)
    if not git_ref_exists(repo_dir, "main", runner=git_runner):
        return refuse("base branch main not present locally", subject=subject, config_sha=config_sha)
    if not git_head_is_commit(repo_dir, head, runner=git_runner):
        return refuse(f"head sha not present locally (never fetched): {head}", subject=subject, config_sha=config_sha)

    try:
        diff = git_diff(repo_dir, "main", head, runner=git_runner)
    except DeliveryRefused as exc:
        return refuse(str(exc), subject=subject, config_sha=config_sha)
    if not diff.strip():
        return refuse("empty diff: nothing to deliver", subject=subject, config_sha=config_sha)
    digest = diff_hash_of(diff)

    # Stage 1: read-only review (rounds capped; reuses a prior round for
    # the same diff instead of spending another model call).
    instruction = (
        f"Delivery review for {repo}#{pr} at {head[:12]}. "
        f"Label summary: {summary}. "
        "The spec axis can only check gross mismatch: the envelope is "
        "pointer-only and carries no PR body."
    )
    review = await run_gated_review(
        diff_text=diff,
        task_instruction=instruction,
        acceptance=["No blocker findings", "Diff matches the labeled change"],
        standards_doc=_DELIVERY_STANDARDS,
        subject_id=subject,
        events=events,
        llm_call_fn=llm_call_fn,
        max_rounds=int(config.get("review_rounds_max", MAX_REVIEW_ROUNDS)),
        incident_fn=incident_fn,
    )
    if not review["passed"]:
        return refuse(
            f"review did not pass: {review['reason']}",
            subject=subject,
            diff_hash=digest,
            config_sha=config_sha,
        )

    # Stage 2: clean-env checks on a detached worktree at the head.
    worktree = _worktree_add(repo_dir, head, runner=git_runner)
    try:
        checks = await run_checks(
            tree=worktree,
            commands=list(config["checks"]),
            timeout_s=int(config.get("check_timeout_s", 600)),
            executor=executor,
        )
    finally:
        _worktree_remove(repo_dir, worktree, runner=git_runner)
    record_checks(events, subject, digest, checks)
    if not checks.passed:
        return refuse(
            f"checks did not pass: {checks.reason}",
            subject=subject,
            diff_hash=digest,
            config_sha=config_sha,
        )

    # Stage 3: mergeability (no ref is touched).
    mergeable, merge_detail = git_mergeable(repo_dir, "main", head, runner=git_runner)
    emit(
        "delivery.mergeable",
        {"subject": subject, "diff_hash": digest, "mergeable": mergeable, "detail": merge_detail},
    )
    if not mergeable:
        return refuse(
            f"head does not merge cleanly onto main: {merge_detail}",
            subject=subject,
            diff_hash=digest,
            config_sha=config_sha,
        )

    # Stage 4: merge gate (latest review + checks records, same hash).
    gate_ok, gate_reason = merge_gate_passed(subject, events=events)
    if not gate_ok:
        return refuse(
            f"merge gate refused: {gate_reason}",
            subject=subject,
            diff_hash=digest,
            config_sha=config_sha,
        )

    base_result: dict[str, Any] = {
        "subject": subject,
        "diff_hash": digest,
        "config_sha": config_sha,
        "review": {"round": review["round"], "capped": review["capped"]},
        "checks": {"commands": [c.argv for c in checks.commands]},
        "mergeable": mergeable,
    }
    if mode == "deliver-review":
        emit("delivery.reported", {**base_result, "mode": mode})
        return {"status": "reported", "mode": mode, **base_result}

    discipline_on = True
    if flag_enabled_fn is not None:
        discipline_on = bool(flag_enabled_fn())
    else:
        try:
            from server.feature_flags import enabled as _flag_enabled

            discipline_on = bool(_flag_enabled("VEYA_CI_DISCIPLINE"))
        except Exception:
            discipline_on = False
    if not discipline_on:
        emit("delivery.reported", {**base_result, "mode": mode, "report_only": True})
        return {"status": "reported", "mode": mode, "report_only": True, **base_result}

    # Human gate: one approval card.  A pending card for the same
    # subject + diff is reused, never duplicated.
    if approval_service is not None:
        try:
            pending = approval_service.store.list_approvals(status="pending", tool="delivery.merge")
        except Exception:
            pending = []
        for row in pending:
            args = row.get("tool_args") or {}
            if isinstance(args, dict) and args.get("subject") == subject and args.get("diff_hash") == digest:
                emit("delivery.card_exists", {**base_result, "approval_id": row.get("request_id")})
                return {"status": "awaiting_approval", **base_result}
    card_args = {
        "subject": subject,
        "repo": repo,
        "pr": pr,
        "head": head,
        "diff_hash": digest,
        "config_sha": config_sha,
        "mode": mode,
        "summary": summary,
        "mergeable": mergeable,
    }
    if approval_service is None:
        from server.approvals import get_approval_service

        approval_service = get_approval_service()
    created = await approval_service.request(
        tool="delivery.merge",
        args=card_args,
        user_id=user_id,
        session_id="",
        actor="delivery",
        reason=f"Delivery order {subject}: review + checks passed; approve to merge (local only, no push).",
    )
    approval_id = str(created.get("approval_id") or created.get("request_id") or "")
    emit("delivery.card_created", {**base_result, "approval_id": approval_id})
    return {"status": "awaiting_approval", "approval_id": approval_id, **base_result}


# ── hub dispatch entry point ───────────────────────────────────────────────


async def delivery_dispatch(
    envelope: dict[str, Any],
    rule: dict[str, Any],
    *,
    user: dict[str, Any],
    events: Any = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Hub ``pipeline:delivery`` dispatch.  ``user`` is required."""

    if events is None:
        from server.events import EventStore

        events = EventStore()
    return await run_delivery_order(
        envelope,
        rule,
        user=user,
        events=events,
        config_path=None,
        repo_path=None,
    )


# ── approval-resolve hook (called from ApprovalService.resolve) ────────────


def on_delivery_approved(
    record: dict[str, Any],
    *,
    decision: str = "",
    emit: Any = None,
    attention_store: Any = None,
) -> dict[str, Any]:
    """Record the approval and leave the operator card.

    Runs synchronously inside the resolve funnel; it must never raise.
    A deny records ``delivery.denied`` and creates no card.  Non-delivery
    approvals are ignored.  ``decision`` is passed explicitly: the
    record fetched before resolve carries no decision.  ``emit`` and
    ``attention_store`` are injectable so tests never touch the
    production event log or attention file.
    """

    from server.events import append_canonical_event

    def _emit(topic: str, payload: dict[str, Any]) -> None:
        if emit is not None:
            emit(topic, payload)
        else:
            append_canonical_event(topic, payload, actor="delivery")

    if str(record.get("tool") or "") != "delivery.merge":
        return {"status": "ignored"}
    tool_args = record.get("tool_args") or {}
    subject = str(tool_args.get("subject") or "")
    decision = decision or str(record.get("decision") or "")
    if decision not in _APPROVE_DECISIONS:
        _emit(
            "delivery.denied",
            {"subject": subject, "decision": decision, "approval_id": record.get("request_id")},
        )
        return {"status": "denied", "subject": subject}
    _emit(
        "delivery.approved",
        {
            "subject": subject,
            "head": str(tool_args.get("head") or ""),
            "diff_hash": str(tool_args.get("diff_hash") or ""),
            "config_sha": str(tool_args.get("config_sha") or ""),
            "approval_id": record.get("request_id"),
        },
    )
    head = str(tool_args.get("head") or "")
    repo = str(tool_args.get("repo") or "")
    pr = tool_args.get("pr")
    try:
        from server.bot_team.attention import AttentionStore

        store = attention_store if attention_store is not None else AttentionStore()
        store.create(
            question=(
                f"Delivery approved: {subject}. Operator steps (manual, local only, no push): "
                f"1) merge {head} into local main in the {repo} checkout and confirm the tree is clean; "
                f"2) deploy via the established docker cp + restart procedure; "
                f"3) check GET /api/v1/version reports commit {head}. "
                "The startup reaper records delivery.verified when the deployed commit matches."
            ),
            kind="escalation",
            category="product-direction",
            tell="needed",
            context=f"subject={subject} head={head} pr={pr}",
        )
    except Exception:
        logger.exception("delivery operator card failed")
    return {"status": "approved", "subject": subject}


# ── startup verify reaper ──────────────────────────────────────────────────


async def reap_delivery_verifications(
    *,
    events: Any = None,
    commit_reader: Any = None,
    emit: Any = None,
) -> int:
    """Record ``delivery.verified`` for approved orders now deployed.

    Silent when the deployed commit does not match: not-yet-deployed
    is not an incident.  Never raises.
    """

    verified = 0
    try:
        if events is None:
            from server.events import EventStore

            events = EventStore()
        approved = events.read_all(topics={"delivery.approved"})
        already = {
            str((e.get("payload") or {}).get("subject") or "")
            for e in events.read_all(topics={"delivery.verified"})
        }
        info = (commit_reader or read_deployed_commit)()
        deployed = info.get("commit") if isinstance(info, dict) else None
        if not deployed:
            return 0
        from server.events import append_canonical_event

        def _emit(topic: str, payload: dict[str, Any]) -> None:
            if emit is not None:
                emit(topic, payload)
            else:
                append_canonical_event(topic, payload, actor="delivery")

        for event in approved:
            payload = event.get("payload") or {}
            subject = str(payload.get("subject") or "")
            head = str(payload.get("head") or "")
            if not subject or not head or subject in already:
                continue
            if deployed == head:
                _emit(
                    "delivery.verified",
                    {"subject": subject, "head": head, "commit": deployed},
                )
                verified += 1
    except Exception:
        logger.exception("delivery verify reap failed")
    return verified

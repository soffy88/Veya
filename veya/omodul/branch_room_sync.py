"""veya/omodul/branch_room_sync — omodul_branch_room_sync（分支即房间）。

把一条 feature 分支上发生的工程事实，投影成一个 P2 群房间（P2 group
``branch:<name>``）里的对话流：

    commits / patches · CI 检查结果（复用 W3 gh 轮询）· AI 首轮评审摘要
    · 行标注（W3 annotations）· 合并决策

**归属（3O PARADIGM §6）**:
* 内部组合 ≥2 个 oskill/oprim —— `veya.oskill.pure.receipt_rank_merge`
  （评审摘要排序 / 引用）与 `veya.oprim.receipt_query`（事实规范化）；
* 标准签名 ``(config, input_data, output_dir) -> dict``，必返 status/error；
* 失败不 raise（除 ``CancelledError``）；``_enabled_pillars`` 显式声明；
* **不裸调 sibling omodul** —— 多阶段组合由 layer4 经
  `oservi.sequential_composer` 编排（`server/nano_w9/branch_rooms.py`）。

**注入而非自建**（§1.2 依赖倒置）：群存储、gh 轮询、标注读取、LLM 全部由
layer4 以 callable 注入。本模块不 import `server.*`（那会把业务根拉进 3O 层，
触发 `check_no_reverse_dep` R2），因此它对"房间存在哪"完全无感。

支柱:
    fingerprint    —— 同分支重复同步可去重
    decision_trail —— 谁在什么时候把什么事实贴进了哪个房间（审计）
    cost           —— 调用注入 LLM 时累计
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from veya.oskill.pure.receipt_rank_merge import build_citation, rank_receipts

__all__ = [
    "ROOM_NAME_PREFIX",
    "BranchRoomOpenConfig",
    "BranchRoomOpenInput",
    "BranchRoomPublishConfig",
    "BranchRoomPublishInput",
    "branch_room_name",
    "branch_room_open",
    "branch_room_publish",
    "compute_fingerprint_for",
    "summarize_review_findings",
]

#: 房间名约定：一个分支对应一个群，名字以此前缀开头（P2 GroupChat 没有 kind
#: 列，前缀是既有 schema 下的最小可逆约定，见 docs/NANO_W9_DESIGN.md §3.1）。
ROOM_NAME_PREFIX = "branch:"

_MAX_MESSAGE_CHARS = 4000


# --------------------------------------------------------------------------
# config / input（§5.2 / §5.3）
# --------------------------------------------------------------------------


@dataclass
class BranchRoomOpenConfig:
    """Open 阶段配置。"""

    repo_path: str = ""
    branch: str = ""
    #: 注入的群创建器 (name, bot_ids) -> group_id
    create_group: Callable[..., Any] | None = None
    #: 注入的房间/群查找器 () -> list[dict]
    list_groups: Callable[[], list[dict[str, Any]]] | None = None
    enabled_pillars: set[str] = field(default_factory=lambda: {"fingerprint", "decision_trail"})

    _omodul_name: str = "branch_room_open"
    _omodul_version: str = "1.0.0"
    _fingerprint_fields: set[str] = field(default_factory=lambda: {"branch", "repo_path"})
    _enabled_pillars: set[str] = field(default_factory=lambda: {"fingerprint", "decision_trail"})


@dataclass
class BranchRoomOpenInput:
    """Open 阶段输入。"""

    branch: str
    bot_ids: list[str] = field(default_factory=list)
    repo_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BranchRoomPublishConfig:
    """Publish 阶段配置。"""

    #: 注入的群消息写入器 (group_id, role, text, bot_id) -> record
    post_message: Callable[..., Any] | None = None
    #: 注入的 gh 检查轮询 (branch, repo_path) -> list[str]
    fetch_checks: Callable[[str, str], list[str]] | None = None
    #: 注入的提交列举 (repo_path, branch, limit) -> list[dict]
    fetch_commits: Callable[[str, str, int], list[dict[str, Any]]] | None = None
    #: 注入的行标注读取 (project_root, goal_id) -> list[dict]
    fetch_annotations: Callable[[str, str], list[dict[str, Any]]] | None = None
    #: 注入的 AI 首轮评审 (prompt) -> dict (含 output / cost_usd / usage)
    llm_caller: Callable[..., Any] | None = None
    enabled_pillars: set[str] = field(
        default_factory=lambda: {"fingerprint", "decision_trail", "cost"}
    )

    _omodul_name: str = "branch_room_publish"
    _omodul_version: str = "1.0.0"
    _fingerprint_fields: set[str] = field(default_factory=lambda: {"branch", "group_id"})
    _enabled_pillars: set[str] = field(
        default_factory=lambda: {"fingerprint", "decision_trail", "cost"}
    )


@dataclass
class BranchRoomPublishInput:
    """Publish 阶段输入。"""

    branch: str
    group_id: str
    bot_id: str = "branch-bot"
    repo_path: str = ""
    goal_id: str = ""
    project_root: str = "."
    merge_decision: str = ""
    commit_limit: int = 20


# --------------------------------------------------------------------------
# 纯算法（可被 oskill 层复用；此处仅做本模块内的确定性投影）
# --------------------------------------------------------------------------


def branch_room_name(branch: str) -> str:
    """分支 → 房间名（确定性、无 IO）。

    Args:
        branch: 分支名，如 ``feat/nano-w9``。

    Returns:
        ``branch:<name>``；空分支返回 ``branch:unknown``（永不为空串，避免
        生成一个无法辨识的房间）。

    Example:
        >>> branch_room_name("feat/nano-w9")
        'branch:feat/nano-w9'
    """
    name = branch.strip() if isinstance(branch, str) else ""
    if not name:
        return f"{ROOM_NAME_PREFIX}unknown"
    return f"{ROOM_NAME_PREFIX}{name[:110]}"


def summarize_review_findings(
    findings: list[dict[str, Any]], *, terms: list[str] | None = None
) -> str:
    """把评审发现压成一条房间消息。

    排序交给 :func:`veya.oskill.pure.receipt_rank_merge.rank_receipts`（单一
    实现），本函数只负责把排序后的前几条渲染成文本。

    Args:
        findings: ``[{"title", "body", "file", "line", "uri"}]``。
        terms: 可选的关注词，参与排序。

    Returns:
        一行一条的摘要文本；无发现时返回固定文案（绝不空串）。

    Example:
        >>> summarize_review_findings([])
        'No review findings recorded for this branch.'
    """
    if not findings:
        return "No review findings recorded for this branch."
    receipts = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            continue
        title = str(finding.get("title") or finding.get("file") or f"finding-{index}")
        where = ""
        if finding.get("file"):
            where = f" ({finding['file']}:{finding.get('line', 0)})"
        receipts.append(
            {
                "receipt_id": f"finding:{index}",
                "source": "annotation",
                "ref": str(finding.get("id") or index),
                "title": f"{title}{where}",
                "text": str(finding.get("body") or ""),
                "uri": str(finding.get("uri") or ""),
                "ts": float(finding.get("ts") or 0.0),
            }
        )
    ranked = rank_receipts(receipts, terms=list(terms or []), limit=10)
    lines = [f"- {r['citation']['label']}: {r['text'][:200]}".rstrip(": ") for r in ranked]
    return "\n".join(lines) if lines else "No review findings recorded for this branch."


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def with_suppress(callback: Callable[[dict[str, Any]], None], payload: dict[str, Any]) -> None:
    """调用 on_step；回调自身抛错不得影响 omodul 结果。"""
    try:
        callback(payload)
    except Exception:
        return


def _call(
    injected: Callable[..., Any] | None, *args: Any, default: Any = None, **kwargs: Any
) -> Any:
    """调用注入的 layer4 callable；未注入或抛错都返回 default（永不 raise）。"""
    if injected is None:
        return default
    try:
        return injected(*args, **kwargs)
    except Exception:
        return default


async def _acall(
    injected: Callable[..., Any] | None, *args: Any, default: Any = None, **kwargs: Any
) -> Any:
    """同上，但支持返回 awaitable 的注入点。"""
    if injected is None:
        return default
    try:
        result = injected(*args, **kwargs)
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            result = await result
        return result
    except Exception:
        return default


def _fingerprint(fields: dict[str, Any]) -> str:
    from veya.oprim.audit_chain import GENESIS_HASH, compute_event_hash

    return compute_event_hash(fields, prev_hash=GENESIS_HASH)[:24]


def compute_fingerprint_for(config: Any, input_data: Any) -> str:
    """暴露给服务层做去重判断（§5.5 MUST）。

    Args:
        config: omodul 配置（读 ``_fingerprint_fields``）。
        input_data: omodul 输入。

    Returns:
        24 字符稳定指纹；字段缺失时返回空串而不是抛错。
    """
    fields = set(getattr(config, "_fingerprint_fields", set()) or set())
    if not fields or input_data is None:
        return ""
    if hasattr(input_data, "model_dump"):
        data = input_data.model_dump()
    elif hasattr(input_data, "__dict__"):
        data = dict(input_data.__dict__)
    elif isinstance(input_data, dict):
        data = dict(input_data)
    else:
        return ""
    return _fingerprint({k: data.get(k) for k in sorted(fields)})


#: decision_trail / fingerprint 禁含的真实身份字段（§5.5.1）。
_PII_KEYS: frozenset[str] = frozenset(
    {"user_id", "student_id", "account_id", "email", "phone", "name", "username", "device_id"}
)


def _strip_pii(value: Any) -> Any:
    """Recursively drop identity keys (mirrors obase Trail._public_event)."""
    if isinstance(value, dict):
        return {k: _strip_pii(v) for k, v in value.items() if str(k).lower() not in _PII_KEYS}
    if isinstance(value, list):
        return [_strip_pii(v) for v in value]
    return value


def _redact(value: Any) -> Any:
    """decision_trail 禁真实 PII（§5.5.1）。

    Identity **keys** are dropped first (obase's ``redact_value`` masks values,
    not key names, so it cannot satisfy §5.5.1 on its own), then the result goes
    through ``obase.redact_value`` so residual secret-shaped values are still
    handled by the single canonical implementation.
    """
    import importlib

    stripped = _strip_pii(value)
    try:
        from veya.platform import load

        fn = getattr(load("obase"), "redact_value", None)
        if fn is not None:
            return fn(stripped)
    except Exception:
        pass
    try:
        module = importlib.import_module("obase.pseudo_anonymizer")
        fn = getattr(module, "redact_value", None)
        if fn is not None:
            return fn(stripped)
    except Exception:
        pass
    return stripped


def _write_trail(
    output_dir: Path | None, name: str, trail: list[dict[str, Any]], *, enabled: bool = True
) -> str | None:
    if output_dir is None or not enabled:
        return None
    try:
        import json

        redact_value = _redact
        path = Path(output_dir) / f"{name}_trail.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(redact_value(trail), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return str(path)
    except Exception:
        return None


# --------------------------------------------------------------------------
# omodul 1 — open
# --------------------------------------------------------------------------


async def branch_room_open(
    config: BranchRoomOpenConfig,
    input_data: BranchRoomOpenInput,
    output_dir: Path | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """为一条分支打开（或复用）P2 群房间。

    Args:
        config: 注入的群创建/查找 callable + 支柱开关。
        input_data: 分支名与期望成员。
        output_dir: 可选；写 decision trail。
        on_step: 同步进度回调。

    Returns:
        ``{"status", "error", "group_id", "room_name", "created",
        "fingerprint", "decision_trail", "cost_usd"}``。失败不 raise。

    Example:
        >>> import asyncio
        >>> # asyncio.run(branch_room_open(cfg, BranchRoomOpenInput(branch="feat/x",
        >>> #     bot_ids=["b1"]), None))
    """
    trail: list[dict[str, Any]] = []

    def record(event: str, **payload: Any) -> None:
        step = {"step_no": len(trail) + 1, "event": event, **payload}
        trail.append(step)
        if on_step is not None:
            with_suppress(on_step, dict(step))

    try:
        branch = (input_data.branch or "").strip()
        if not branch:
            return {
                "status": "failed",
                "error": {"code": "INVALID_INPUT", "reason": "branch is required"},
                "group_id": "",
                "room_name": "",
                "created": False,
                "fingerprint": "",
                "decision_trail": trail,
                "cost_usd": 0.0,
            }
        room_name = branch_room_name(branch)
        fingerprint = compute_fingerprint_for(config, input_data)
        record("open.requested", room_name=room_name, branch=branch)

        existing = _call(config.list_groups, default=[]) or []
        match = next(
            (g for g in existing if isinstance(g, dict) and str(g.get("name") or "") == room_name),
            None,
        )
        if match is not None:
            group_id = str(match.get("id") or "")
            record("open.reused", group_id=group_id)
            return {
                "status": "completed",
                "error": None,
                "group_id": group_id,
                "room_name": room_name,
                "created": False,
                "fingerprint": fingerprint,
                "decision_trail": trail,
                "cost_usd": 0.0,
                "trail_path": _write_trail(output_dir, "branch_room_open", trail),
            }

        group_id = str(
            await _acall(
                config.create_group,
                room_name,
                list(input_data.bot_ids or []),
                default="",
            )
            or ""
        )
        if not group_id:
            record("open.failed", reason="create_group returned no id")
            return {
                "status": "failed",
                "error": {"code": "GROUP_CREATE_FAILED", "reason": "no group id returned"},
                "group_id": "",
                "room_name": room_name,
                "created": False,
                "fingerprint": fingerprint,
                "decision_trail": trail,
                "cost_usd": 0.0,
                "trail_path": _write_trail(output_dir, "branch_room_open", trail),
            }
        record("open.created", group_id=group_id, members=list(input_data.bot_ids or []))
        return {
            "status": "completed",
            "error": None,
            "group_id": group_id,
            "room_name": room_name,
            "created": True,
            "fingerprint": fingerprint,
            "decision_trail": trail,
            "cost_usd": 0.0,
            "trail_path": _write_trail(output_dir, "branch_room_open", trail),
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # §5.4 不 raise
        return {
            "status": "failed",
            "error": {"code": type(exc).__name__, "reason": str(exc)},
            "group_id": "",
            "room_name": "",
            "created": False,
            "fingerprint": "",
            "decision_trail": trail,
            "cost_usd": 0.0,
        }


# --------------------------------------------------------------------------
# omodul 2 — publish
# --------------------------------------------------------------------------


async def branch_room_publish(
    config: BranchRoomPublishConfig,
    input_data: BranchRoomPublishInput,
    output_dir: Path | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """把分支事实流贴进房间。

    依次发布：提交摘要 → CI 检查 → 行标注 → AI 首轮评审 → 合并决策。
    任一来源未注入即记 ``skipped``，绝不因此失败。

    Args:
        config: 注入的写入/查询/LLM callable + 支柱开关。
        input_data: 分支、房间 id、GoalRun 上下文。
        output_dir: 可选；写 decision trail。
        on_step: 同步进度回调。

    Returns:
        ``{"status", "error", "posted", "skipped", "fingerprint",
        "decision_trail", "cost_usd", "citations"}``。

    Example:
        >>> import asyncio
        >>> # asyncio.run(branch_room_publish(cfg, BranchRoomPublishInput(
        >>> #     branch="feat/x", group_id="grp_1")))
    """
    trail: list[dict[str, Any]] = []
    posted: list[str] = []
    skipped: list[str] = []
    citations: list[dict[str, Any]] = []
    cost_usd = 0.0

    def record(event: str, **payload: Any) -> None:
        step = {"step_no": len(trail) + 1, "event": event, **payload}
        trail.append(step)
        if on_step is not None:
            with_suppress(on_step, dict(step))

    async def post(label: str, text: str) -> None:
        body = text.strip()
        if not body:
            skipped.append(label)
            record("publish.skipped", section=label, reason="empty body")
            return
        record_id = await _acall(
            config.post_message,
            input_data.group_id,
            "bot",
            body[:_MAX_MESSAGE_CHARS],
            input_data.bot_id,
            default=None,
        )
        if record_id is None:
            skipped.append(label)
            record("publish.failed", section=label, reason="post_message returned nothing")
        else:
            posted.append(label)
            citations.append(build_citation({"source": "group_message", "ref": str(record_id)}))
            record("publish.posted", section=label, message_id=str(record_id))

    try:
        fingerprint = compute_fingerprint_for(config, input_data)
        record(
            "publish.started",
            branch=input_data.branch,
            group_id=input_data.group_id,
            goal_id=input_data.goal_id,
        )

        commits = (
            _call(
                config.fetch_commits,
                input_data.repo_path,
                input_data.branch,
                input_data.commit_limit,
                default=[],
            )
            or []
        )
        if commits:
            lines = [
                f"- {c.get('sha', '?')[:8]} {c.get('subject', '')}".rstrip()
                for c in commits
                if isinstance(c, dict)
            ]
            await post("commits", "\n".join(lines))
        else:
            skipped.append("commits")

        checks = _call(config.fetch_checks, input_data.branch, input_data.repo_path, default=[])
        if isinstance(checks, list) and checks:
            await post("ci", "Failing checks:\n" + "\n".join(f"- {name}" for name in checks))
        elif isinstance(checks, list):
            await post("ci", "CI: all checks green.")
        else:
            skipped.append("ci")

        annotations = (
            _call(config.fetch_annotations, input_data.project_root, input_data.goal_id, default=[])
            or []
        )
        if annotations:
            await post("annotations", summarize_review_findings(annotations))
            for annotation in annotations:
                if isinstance(annotation, dict):
                    citations.append(
                        build_citation(
                            {
                                "source": "annotation",
                                "ref": str(annotation.get("id") or ""),
                                "title": str(annotation.get("file") or ""),
                                "uri": str(annotation.get("uri") or ""),
                            }
                        )
                    )
        else:
            skipped.append("annotations")

        if config.llm_caller is not None:
            prompt = (
                f"First-pass AI review summary for branch {input_data.branch!r} in "
                f"{input_data.repo_path!r}. Checks: {checks!r}. Annotations: {len(annotations)}."
            )
            review = await _acall(config.llm_caller, prompt, default=None)
            if isinstance(review, dict):
                with contextlib.suppress(TypeError, ValueError):
                    cost_usd += float(review.get("cost_usd") or 0.0)
                await post("ai_review", str(review.get("output") or ""))
            else:
                skipped.append("ai_review")
                record("publish.skipped", section="ai_review", reason="llm_caller returned nothing")
        else:
            skipped.append("ai_review")

        if input_data.merge_decision:
            await post("merge_decision", f"Merge decision: {input_data.merge_decision}")
        else:
            skipped.append("merge_decision")

        return {
            "status": "completed",
            "error": None,
            "posted": posted,
            "skipped": skipped,
            "fingerprint": fingerprint,
            "decision_trail": trail,
            "cost_usd": cost_usd,
            "citations": citations,
            "trail_path": _write_trail(output_dir, "branch_room_publish", trail),
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # §5.4 不 raise
        return {
            "status": "failed",
            "error": {"code": type(exc).__name__, "reason": str(exc)},
            "posted": posted,
            "skipped": skipped,
            "fingerprint": "",
            "decision_trail": trail,
            "cost_usd": cost_usd,
            "citations": citations,
        }

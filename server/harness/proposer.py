"""VeyaProposer: 走 Veya 现有 provider 配置的真实 harness2 proposer。

Phase 0 的 ``VeyaDemoProposer`` 不发模型调用，只验证流程。Phase 1 换成真模型：
同一个 ``.call(mission, context_dir, label, harvest=...)`` 协议，但把"起
opencode/claude CLI 子进程"换成一次 ``veya.obase.llm.llm_call``，并给模型一组
**只作用于 evidence 树**的文件工具。

三条硬边界：

1. **工具面隔离**。proposer 的工具只有 read_file / list_dir / write_file /
   apply_patch，全部以 ``context_dir`` 为根做路径夹持。它看不见 master_tools、
   不能触网、不能执行命令。
2. **默认关闭**。``VEYA_HARNESS2_REAL_PROPOSER`` 关闭时构造即抛
   ``ProposerDisabled``，一个 LLM 请求都不发。
3. **编辑即提案**。模型对 ``context/harness/`` 的落盘改动经 ``harvest`` 拷出后
   成为 ``Reply.workspace``，harness2 的 ``recursion._run_stage`` 会把它读成树。
   我们只提供工具与路径，不代替模型生成提案内容。
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from server.harness._harness2 import import_harness2_core

#: flag 名（与 server/feature_flags.py 表尾条目一致）。
FLAG_NAME = "VEYA_HARNESS2_REAL_PROPOSER"

#: proposer 单次会话的最大工具轮数。harness2 的 OpenCode channel 默认 120 步，
#: 这里收紧到 24：证据树是几十个 markdown 文件，超过 24 步说明模型在原地打转。
#: 实测 DeepSeek Flash 做一次"读证据 → 改文件 → 复核"约 6–8 轮，12 太紧。
MAX_TOOL_ROUNDS = 24

#: 模型回复里允许的 <notes> 块。recursion 用正则提取它，缺省时退回整段文本。
_NOTES = re.compile(r"<notes>(.*?)</notes>", re.S)


class ProposerDisabled(RuntimeError):
    """feature flag 关闭： proposer 会话被明确拒绝，而不是静默退化。"""


class ProposerError(RuntimeError):
    """proposer 通道在构造期就不可用（无可用模型 / 无凭据）。

    会话期的失败抛 harness2 自己的 ``core.proposer.ProposerError``——那是
    ``recursion._run_stage`` 捕获的类型，抛别的会让"提案失败→回退 base"的
    降级路径失效。
    """


def proposer_enabled() -> bool:
    """读 feature flag；flag 表缺失该条目时按关闭处理（绝不默认放行）。"""
    try:
        from server.feature_flags import enabled

        return bool(enabled(FLAG_NAME))
    except Exception:
        return False


# ── proposer 工具面（只作用于 evidence 树） ─────────────────────


def _tool_schemas() -> list[dict]:
    def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    return [
        _fn(
            "read_file",
            "Read a UTF-8 text file under context/. Returns the file body.",
            {"path": {"type": "string", "description": "Path relative to context/."}},
            ["path"],
        ),
        _fn(
            "list_dir",
            "List a directory under context/, one relative path per line.",
            {"path": {"type": "string", "description": "Directory relative to context/."}},
            ["path"],
        ),
        _fn(
            "write_file",
            "Create or replace a file under context/harness/. Use this to edit the harness.",
            {
                "path": {
                    "type": "string",
                    "description": "Path relative to context/, e.g. harness/systemprompt.md.",
                },
                "content": {"type": "string", "description": "Full new file body."},
            },
            ["path", "content"],
        ),
        _fn(
            "apply_patch",
            "Apply a unified diff to a file under context/harness/.",
            {
                "path": {"type": "string", "description": "Path relative to context/."},
                "patch": {"type": "string", "description": "Unified diff text."},
            },
            ["path", "patch"],
        ),
    ]


class _EvidenceTools:
    """路径夹持在 ``root`` 内的只读/限写文件工具。

    写只允许落在 ``root/harness`` 下：那是 harness2 的可编辑树，也正是
    "你的编辑就是提案"发生的地方。其余路径只读。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.writable = self.root / "harness"
        self.calls: list[tuple[str, str]] = []
        #: 每次调用的 (工具名, 规范化参数)，用于精确识别"同一调用重复"。
        self.signatures: list[tuple[str, str]] = []

    # ── 路径解析 ──
    def _resolve(self, rel: str) -> Path:
        candidate = (self.root / str(rel).strip()).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"path escapes the evidence tree: {rel!r}")
        return candidate

    def _resolve_writable(self, rel: str) -> Path:
        candidate = self._resolve(rel)
        if candidate != self.writable and self.writable not in candidate.parents:
            raise ValueError(f"only context/harness/ is writable, got {rel!r}")
        return candidate

    # ── 工具实现 ──
    def read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ValueError(f"no such file: {path!r}")
        return target.read_text(encoding="utf-8", errors="replace")

    def list_dir(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_dir():
            raise ValueError(f"no such directory: {path!r}")
        lines: list[str] = []
        for item in sorted(target.rglob("*")):
            if "__pycache__" in item.parts:
                continue
            suffix = "/" if item.is_dir() else ""
            lines.append(item.relative_to(self.root).as_posix() + suffix)
        return "\n".join(lines) or "(empty)"

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve_writable(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        return f"wrote {target.relative_to(self.root).as_posix()}"

    def apply_patch(self, path: str, patch: str) -> str:
        """最小 unified-diff 应用，按**上下文行**定位而不是信 hunk 行号。

        模型给的行号经常相对于它上一次读到的文件版本，而中间已经有过别的编辑，
        照行号落点会**复制**而不是就地替换。所以每个 hunk 都用它的
        context/removed 行去原文里找唯一匹配；找不到或有多处匹配时明确报错，
        绝不猜一个位置落下去。

        只支持 harness2 证据树规模的小补丁。
        """
        target = self._resolve_writable(path)
        if not target.is_file():
            raise ValueError(f"no such file to patch: {path!r}")
        original = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        out: list[str] = []
        cursor = 0
        applied = 0
        for hunk in _split_hunks(str(patch)):
            body = _hunk_body(hunk)
            anchors = [line for line in body if line.startswith((" ", "-")) and line[1:].strip()]
            if not anchors:
                raise ValueError(f"hunk has no context or removed lines to locate: {hunk[0]!r}")
            index = _locate(original, body, cursor)
            if index is None:
                raise ValueError(
                    f"could not locate the hunk context in {path!r}; the file changed "
                    "since it was last read. Re-read it and send a fresh patch."
                )
            if index < cursor:
                raise ValueError("patch hunks overlap or are out of order")
            out.extend(original[cursor:index])
            # cursor 必须推进到 hunk 落点：否则 context/removed 行会从更早的
            # 位置消费原文，补丁把未消费的尾部又抄一遍（内容被复制）。
            cursor = index
            for line in body:
                if line.startswith("-"):
                    # removed 行只从原文消费，不写进结果
                    cursor += 1
                elif line.startswith("+"):
                    out.append(_ensure_eol(line[1:]))
                elif line.startswith(" "):
                    # context 行原文照抄（不 strip，保留缩进）
                    out.append(line[1:])
                    cursor += 1
                # 其余行（补丁尾部的说明文字等）忽略
            applied += 1
        out.extend(original[cursor:])
        target.write_text("".join(out), encoding="utf-8")
        return f"patched {target.relative_to(self.root).as_posix()} ({applied} hunk(s))"

    def execute(self, name: str, args: dict[str, Any]) -> str:
        handler = {
            "read_file": self.read_file,
            "list_dir": self.list_dir,
            "write_file": self.write_file,
            "apply_patch": self.apply_patch,
        }.get(name)
        if handler is None:
            raise ValueError(f"unknown proposer tool {name!r}")
        self.calls.append((name, json.dumps(args, sort_keys=True)[:200]))
        self.signatures.append(_call_signature(name, args))
        return str(handler(**args))


def _split_hunks(patch: str) -> list[list[str]]:
    hunks: list[list[str]] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("@@"):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
    if not hunks:
        raise ValueError("patch contains no @@ hunk")
    return hunks


def _hunk_body(hunk: list[str]) -> list[str]:
    """一个 hunk 的 body 行（去掉 ``@@`` 头与 ``---``/``+++`` 文件头）。"""
    return [line for line in hunk[1:] if line.strip() and not line.startswith(("---", "+++"))]


def _ensure_eol(line: str) -> str:
    return line if line.endswith("\n") else line + "\n"


def _locate(original: list[str], body: list[str], cursor: int) -> int | None:
    """在 ``original[cursor:]`` 里找 ``body`` 的 context/removed 行的唯一落点。

    hunk 行号只是提示，真正的锚是上下文内容：模型手里的行号常常过期。返回
    ``None`` 表示没找到或有多个候选——调用方必须报错而不是猜。
    """
    # 只拿 context(' ') 与 removed('-') 行做锚；'+' 行是要写入的内容，不是原文。
    needle = [
        _ensure_eol(line[1:]) for line in body if line.startswith((" ", "-")) and line[1:].strip()
    ]
    if not needle:
        return None
    width = len(needle)
    if len(original) - cursor < width:
        return None
    hits = [
        index
        for index in range(cursor, len(original) - width + 1)
        if original[index : index + width] == needle
    ]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        # 行尾差异（CRLF / 缺末尾换行）不该让一个本来唯一的补丁失败。
        loose = [
            index
            for index in range(cursor, len(original) - width + 1)
            if [ln.rstrip("\r\n") for ln in original[index : index + width]]
            == [ln.rstrip("\r\n") for ln in needle]
        ]
        return loose[0] if len(loose) == 1 else None
    return None


# ── proposer ───────────────────────────────────────────────────


class VeyaProposer:
    """harness2 ``Proposer`` 的 Veya 实现：一次 LLM 会话 + 证据树文件工具。"""

    def __init__(
        self,
        spec: Any = None,
        *,
        provider: str = "",
        model: str = "",
        audit_dir: Path | str | None = None,
        max_rounds: int = MAX_TOOL_ROUNDS,
        probe: object | None = None,
    ) -> None:
        if not proposer_enabled():
            raise ProposerDisabled(
                f"{FLAG_NAME} is off; the Phase 0 demo proposer stays the path. "
                f"Set {FLAG_NAME}=1 to allow real proposer sessions."
            )
        from server.harness.channel import ProposerChannel, select_channel

        self.spec = spec
        self.audit_dir = Path(audit_dir).expanduser() if audit_dir else None
        self.max_rounds = max(1, int(max_rounds))
        self._calls: list[Any] = []
        self.total_usd = 0.0
        self.total_tokens = 0
        self.total_tool_calls = 0
        self._extra_usd = 0.0

        if provider and model:
            channel = ProposerChannel(provider=provider, model=model, source="explicit-arg")
            self.channel_records = [f"explicit: {channel.label}"]
        else:
            channel, records = select_channel(probe=probe)
            self.channel_records = records
            if channel is None:
                raise ProposerError("no usable proposer channel: " + "; ".join(records))
        self.channel = channel
        self.model = channel

    # ── harness2 Proposer 协议 ─────────────────────────────────
    def call(
        self,
        mission: str,
        context_dir: Path | str | None,
        label: str,
        *,
        system: str = "",
        timeout: int | None = None,
        max_attempts: int | None = None,
        harvest: str | None = None,
        **_kwargs: Any,
    ) -> Any:
        """跑一次 proposer 会话；没有可用回复时抛 ProposerError。"""
        core = import_harness2_core()
        context = Path(context_dir) if context_dir is not None else None
        if context is None or not context.is_dir():
            raise core.proposer.ProposerError(
                f"[{label}] evidence directory is missing: {context_dir}"
            )

        tools = _EvidenceTools(context)
        started = time.time()
        reply_text, usd, _rounds = self._run_session(mission, system, tools)
        usd += self._extra_usd
        self._extra_usd = 0.0
        self.total_usd += usd
        self.total_tool_calls += len(tools.calls)

        workspace = None
        if harvest:
            workspace = self._harvest(context, harvest, label)
        reply = core.proposer.Reply(
            label=label,
            text=reply_text,
            ok=bool(reply_text.strip()),
            cost_usd=round(usd, 6),
            wall_s=round(time.time() - started, 1),
            attempts=1,
            channel=f"veya:{self.model.provider}/{self.model.model}",
            workspace=workspace,
        )
        self._calls.append(reply)
        self._write_audit(reply, mission, system, tools)
        if not reply.ok:
            raise core.proposer.ProposerError(
                f"[{label}] veya:{self.model.label}: empty reply", cost_usd=usd
            )
        return reply

    # ── 会话 ───────────────────────────────────────────────────
    def _run_session(
        self, mission: str, system: str, tools: _EvidenceTools
    ) -> tuple[str, float, int]:
        from veya.obase._llm_config import calc_cost
        from veya.obase.llm import llm_call

        messages: list[dict] = []
        if system.strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": mission})
        schemas = _tool_schemas()
        usd = 0.0
        text = ""
        for round_index in range(self.max_rounds):
            response = _run_llm(
                llm_call,
                messages,
                provider=self.model.provider,
                model=self.model.model,
                tools=schemas,
            )
            usd += _response_cost(response, self.model.provider, calc_cost)
            self._record_usage(response)
            message = (response.get("choices") or [{}])[0].get("message") or {}
            tool_calls = message.get("tool_calls") or []
            content = str(message.get("content") or "")
            if not tool_calls:
                text = content
                break
            messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
            repeated = _repeated_call(tool_calls, tools.signatures)
            for call in tool_calls:
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    observation = tools.execute(name, args)
                except Exception as exc:
                    observation = f"ERROR {type(exc).__name__}: {exc}"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "name": name,
                        "content": observation,
                    }
                )
            if repeated and round_index >= 2:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You have already read these files and are repeating yourself. "
                            "Stop inspecting and answer now: make the edit you decided on "
                            "(or leave the harness unchanged), then reply with one "
                            "<notes> block."
                        ),
                    }
                )
        else:
            # 轮数用尽：再给一次不带工具的机会，逼出文本回复而不是直接判失败。
            # 模型在证据树里打转很常见；把最后一轮浪费在"空回复"上会让整个提案
            # 回退到 base，白花一次会话的钱。
            text = self._final_turn(messages, llm_call, calc_cost)
        return text, usd, len(tools.calls)

    def _final_turn(self, messages: list[dict], llm_call: Any, calc_cost: Any) -> str:
        """不带工具的最后一轮：只要一段文字。"""
        messages = [
            *messages,
            {
                "role": "user",
                "content": (
                    "Stop here. Reply with your final <notes> block now; do not call any "
                    "more tools. If you made no edit, say KEEP_BASE in the notes."
                ),
            },
        ]
        response = _run_llm(
            llm_call,
            messages,
            provider=self.model.provider,
            model=self.model.model,
            tools=[],
        )
        self._extra_usd += _response_cost(response, self.model.provider, calc_cost)
        self._record_usage(response)
        message = (response.get("choices") or [{}])[0].get("message") or {}
        return str(message.get("content") or "")

    def _record_usage(self, response: dict) -> None:
        """累计 token 用量；计价表里没有该 provider 时成本会算成 0，token 不会。"""
        usage = response.get("usage") or {}
        if not isinstance(usage, dict):
            return
        self.total_tokens += int(usage.get("prompt_tokens") or 0) + int(
            usage.get("completion_tokens") or 0
        )

    # ── 落盘审计（不记录任何 key） ─────────────────────────────
    def _harvest(self, context: Path, sub: str, label: str) -> Path | None:
        """把 ``context/<sub>`` 拷到审计目录，作为 ``Reply.workspace`` 返回。

        harness2 原生 Proposer 先把 ``context_dir`` 拷成会话目录里的 ``context/``，
        再按 ``harvest="context/harness"`` 取子路径。我们直接以 ``context_dir`` 为
        根操作，所以这里要把 harness2 传进来的 ``context/`` 前缀去掉——否则会去
        找 ``<context>/context/harness``，那个目录不存在，整个提案被判成
        "harness 没回来"。
        """
        relative = str(sub).strip().lstrip("./")
        if relative == "context" or relative.startswith("context/"):
            relative = relative[len("context") :].lstrip("/")
        src = context / relative if relative else context
        if not src.is_dir():
            return None
        dest = (self.audit_dir or context.parent) / f"{_safe(label)}.workspace"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
        return dest

    def _write_audit(self, reply: Any, mission: str, system: str, tools: _EvidenceTools) -> None:
        if self.audit_dir is None:
            return
        directory = self.audit_dir / _safe(reply.label)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "mission.md").write_text(mission, encoding="utf-8")
        if system.strip():
            (directory / "system.md").write_text(system, encoding="utf-8")
        (directory / "response.md").write_text(reply.text, encoding="utf-8")
        (directory / "tool_calls.md").write_text(
            "\n".join(f"- {name} {args}" for name, args in tools.calls) + "\n", encoding="utf-8"
        )
        (directory / "meta.json").write_text(
            json.dumps(
                {
                    "label": reply.label,
                    "ok": reply.ok,
                    "provider": self.model.provider,
                    "model": self.model.model,
                    "model_source": self.model.source,
                    "cost_usd": reply.cost_usd,
                    "wall_s": reply.wall_s,
                    "tool_calls": len(tools.calls),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def _run_llm(llm_call: Any, messages: list[dict], *, provider: str, model: str, tools: list[dict]):
    """同步外壳里跑一次异步 llm_call（proposer 自身是同步协议）。"""
    import asyncio
    import inspect

    outcome = llm_call(
        messages,
        provider=provider,
        model=model,
        tools=tools,
        max_tokens=8192,
    )
    if inspect.isawaitable(outcome):
        return asyncio.run(_await(outcome))
    return outcome


async def _await(awaitable):
    return await awaitable


def _response_cost(response: dict, provider: str, calc_cost: Any) -> float:
    direct = response.get("cost_usd")
    if isinstance(direct, (int, float)):
        return float(direct)
    usage = response.get("usage") or {}
    if isinstance(usage, dict) and isinstance(usage.get("cost_usd"), (int, float)):
        return float(usage["cost_usd"])
    try:
        return float(calc_cost(provider, usage))
    except Exception:
        return 0.0


def _safe(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", label)


def _call_signature(name: str, args: dict[str, Any]) -> str:
    """一次工具调用的规范化身份：同名同参才算同一次调用。"""
    try:
        payload = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        payload = str(args)
    return f"{name}:{payload}"


def _repeated_call(tool_calls: list[dict], already: list[str]) -> bool:
    """这一批调用是否只是把已经做过的读操作原样又做一遍。

    proposer 卡在"再读一遍同一批证据"上是实测到的失败模式：不提示它，会话会
    一路读到轮数上限。判据是**同名同参**，不是同名——读第二个不同的文件是正常
    取证，不能算重复。只有全部调用都重复时才算，避免误伤"读完顺手改一处"。
    """
    if not already:
        return False
    seen = set(already)
    signatures = []
    for call in tool_calls:
        fn = call.get("function") or {}
        name = str(fn.get("name") or "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            return False
        if not isinstance(args, dict):
            return False
        signatures.append(_call_signature(name, args))
    return bool(signatures) and all(
        signature in seen and signature.split(":", 1)[0] in {"read_file", "list_dir"}
        for signature in signatures
    )


def extract_notes(text: str) -> str:
    """``<notes>`` 块，缺省时退回整段文本（与 harness2 的提取规则一致）。"""
    match = _NOTES.search(text)
    return (match.group(1).strip() if match else text.strip())[:4000]


__all__ = [
    "FLAG_NAME",
    "MAX_TOOL_ROUNDS",
    "ProposerDisabled",
    "ProposerError",
    "VeyaProposer",
    "extract_notes",
    "proposer_enabled",
]

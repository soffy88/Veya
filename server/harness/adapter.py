"""VeyaBenchmarkAdapter: 把 Veya 的隔离执行原语包装成 harness2 executor。

Phase 0 不调用真实模型，也不改冻结主链：

* ``execute()`` 用 Veya 已有的 ``server.agent_loop_bridge.run_strict_chat``
  （即 ``agent_loop_run`` 工具背后的同一 AgentLoop 心脏），注入
  **scripted LLM** 与**确定性 fixture 工具面**。脚本化模型只做固定的工具调用，
  不发起任何网络请求；工具只做算术/文本处理并写交付物。
* ``VeyaDemoProposer`` 是 harness2 ``DemoProposer`` 的 Veya 版本：编辑阶段原样
  保留 base harness（Phase 0 只验证流程，不回流改进），experience 阶段返回
  ``NO_ENTRY``，因此不会写入持久 Library。

这满足"在 Veya 上跑、无模型调用、k=1 全循环、产出到
``~/.veya/harness/runs/``"，同时不新增用户可见主链或工具。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from server.harness._harness2 import import_harness2_core
from server.harness.export_base_harness import export_base_harness
from server.harness.surface import veya_substrate

_TASKS = ("sum-1", "upper-1")


def _tool_schemas() -> list[dict]:
    """fixture 工具面（只给 scripted LLM 看，不进全局 master_tools）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "compute_sum",
                "description": "Sum the integers from 1 through limit.",
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer"}},
                    "required": ["limit"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_text",
                "description": "Read the task input text.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "to_upper",
                "description": "Convert text to uppercase.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_deliverable",
                "description": "Write the final deliverable text.",
                "parameters": {
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                },
            },
        },
    ]


class _ScriptedLlm:
    """Deterministic OpenAI-shaped LLM: emits the fixture's planned tool calls."""

    def __init__(self, plan: list[dict[str, Any]], final: str) -> None:
        self._plan = list(plan)
        self._final = final
        self._index = 0
        self.seen_tools: list[list[dict]] = []

    async def complete(self, messages: list[dict], **kwargs: Any) -> dict:
        self.seen_tools.append(kwargs.get("tools") or [])
        if self._index < len(self._plan):
            step = self._plan[self._index]
            self._index += 1
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{self._index}",
                        "type": "function",
                        "function": {
                            "name": step["name"],
                            "arguments": step["arguments"],
                        },
                    }
                ],
            }
        else:
            message = {"role": "assistant", "content": self._final}
        return {"choices": [{"message": message}]}

    async def close(self) -> None:
        return None


class VeyaBenchmarkAdapter:
    """harness2 ``BenchmarkAdapter`` 的 Veya 实现（Phase 0 确定性后端）。"""

    def __init__(
        self,
        root: Path | str,
        *,
        base_harness_dir: Path | str | None = None,
        runs_root: Path | str | None = None,
    ) -> None:
        core = import_harness2_core()
        self._core = core
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.substrate = veya_substrate()
        self._runs_root = Path(runs_root).expanduser() if runs_root else self.root / "runs"
        channel = core.spec.ChannelSpec("opencode", "local/deterministic")
        self.spec = core.spec.BenchSpec(
            name="veya",
            runs_root=self._runs_root,
            install_mode="workspace_config",
            prompts=core.spec.BenchPrompts(
                work="complete a small Veya task using the available Veya tools",
                domain_word="task area",
                deliverable_word="deliverable",
            ),
            channels={"proposer": channel, "selector": channel},
            leak_globs=(),
        )
        self._base_harness = (
            Path(base_harness_dir).expanduser()
            if base_harness_dir is not None
            else self.root / "base"
        )
        # An explicit base is caller-owned (tests and Phase 1 may pass a candidate).
        # The default base is a live read-only snapshot and must be refreshed so a
        # rerun cannot silently execute an older prompt/tool surface.
        if base_harness_dir is None or not self._base_harness.is_dir():
            export_base_harness(self._base_harness)
        self._prepare_sources()
        self.execution_count = 0

    # ── harness2 BenchmarkAdapter 协议 ─────────────────────────────
    def tasks(self, split: str = "all") -> list[str]:
        return list(_TASKS)

    def domain_of(self, task_id: str) -> str:
        return "veya-phase0"

    def task_prompt(self, task_id: str) -> str:
        if task_id == "sum-1":
            return "Compute the sum of integers 1 through 10 and write it as `sum(1..10)=<n>`."
        if task_id == "upper-1":
            return "Read input.txt, convert it to uppercase, and write the uppercase text."
        raise KeyError(f"unknown task {task_id!r}")

    def task_files(self, task_id: str) -> Path | None:
        source = self.root / "sources" / task_id
        return source if source.is_dir() else None

    def base_harness(self) -> Path:
        return self._base_harness

    def validate_tree(self, tree: dict[str, str]) -> None:
        """Phase 0 的额外不变量：四个 always-on 文件必须在树里。"""
        missing = [rel for rel in self.substrate.surface().always_on if rel not in tree]
        if missing:
            raise ValueError(
                "Veya harness is missing always-on file(s): " + ", ".join(sorted(missing))
            )

    def execute(
        self,
        task_id: str,
        harness_dir: Path | None,
        run_id: str,
        *,
        timeout: int | None = None,
    ) -> Path:
        """用 Veya 的 AgentLoop 心脏跑一个确定性子任务，返回 run 目录。"""
        self.execution_count += 1
        run_dir = self._runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        plan, final = self._fixture_plan(task_id)
        llm = _ScriptedLlm(plan, final)
        deliverable_path = run_dir / "deliverable.txt"

        def _write(content: str) -> str:
            deliverable_path.write_text(content, encoding="utf-8")
            return f"wrote {deliverable_path.name}"

        def _read(path: str) -> str:
            return Path(path).read_text(encoding="utf-8")

        def _executor(name: str, args: dict[str, Any]) -> Any:
            if name == "compute_sum":
                return sum(range(1, int(args["limit"]) + 1))
            if name == "read_text":
                return _read(str(args["path"]))
            if name == "to_upper":
                return str(args["text"]).upper()
            if name == "write_deliverable":
                return _write(str(args["content"]))
            raise ValueError(f"unknown fixture tool {name!r}")

        from server.agent_loop_bridge import run_strict_chat

        result = asyncio.run(
            run_strict_chat(
                self.task_prompt(task_id),
                session_id=f"veya-phase0-{_safe(run_id)}",
                llm=llm,
                kv_path=str(self.root / "loop" / f"{_safe(run_id)}.db"),
                tool_schemas=_tool_schemas(),
                tool_executor=_executor,
                system_prompt=self._system_prompt(),
            )
        )
        if result.get("status") != "success":
            raise RuntimeError(
                f"Veya fixture run failed for {task_id}: "
                f"{result.get('error') or result.get('stop_kind') or result.get('status')}"
            )
        if not deliverable_path.is_file():
            raise RuntimeError(
                f"Veya fixture run for {task_id} completed without writing deliverable.txt"
            )
        self._write_trajectory(run_dir, task_id, result, harness_dir)
        (run_dir / "result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return run_dir

    def delivered(self, run_dir: Path) -> bool:
        return (Path(run_dir) / "deliverable.txt").is_file()

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        path = Path(run_dir) / "trajectory.md"
        text = path.read_text(encoding="utf-8") if path.is_file() else "(no trajectory)"
        return text if obs_trunc <= 0 else text[:obs_trunc]

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        path = Path(run_dir) / "deliverable.txt"
        return {"deliverable.txt": path.read_text(encoding="utf-8")} if path.is_file() else {}

    def deliverable_text(self, run_dir: Path) -> str:
        path = Path(run_dir) / "deliverable.txt"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def score(self, run_dir: Path, task_id: str) -> dict | None:
        """Phase 0 has no judge; ``None`` is the protocol's judge-failed sentinel."""
        return None

    # ── Phase 0 fixture 细节 ───────────────────────────────────────
    def _prepare_sources(self) -> None:
        upper = self.root / "sources" / "upper-1"
        upper.mkdir(parents=True, exist_ok=True)
        (upper / "input.txt").write_text("hello from veya\n", encoding="utf-8")
        (self.root / "sources" / "sum-1").mkdir(parents=True, exist_ok=True)

    def _system_prompt(self) -> str:
        path = self._base_harness / "systemprompt.md"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _fixture_plan(self, task_id: str) -> tuple[list[dict[str, Any]], str]:
        if task_id == "sum-1":
            return (
                [
                    {"name": "compute_sum", "arguments": {"limit": 10}},
                    {
                        "name": "write_deliverable",
                        "arguments": {"content": "sum(1..10)=55\n"},
                    },
                ],
                "sum fixture completed",
            )
        if task_id == "upper-1":
            input_path = str((self.root / "sources" / "upper-1" / "input.txt").resolve())
            return (
                [
                    {"name": "read_text", "arguments": {"path": input_path}},
                    {
                        "name": "to_upper",
                        "arguments": {"text": "hello from veya\n"},
                    },
                    {
                        "name": "write_deliverable",
                        "arguments": {"content": "HELLO FROM VEYA\n"},
                    },
                ],
                "upper fixture completed",
            )
        raise KeyError(f"unknown task {task_id!r}")

    def _write_trajectory(
        self,
        run_dir: Path,
        task_id: str,
        result: dict,
        harness_dir: Path | None,
    ) -> None:
        lines = [
            f"# Veya Phase 0 trajectory: {task_id}",
            "",
            f"- harness: `{harness_dir or self.base_harness()}`",
            f"- status: {result.get('status')}",
            f"- stop_kind: {result.get('stop_kind')}",
            f"- rounds: {result.get('rounds')}",
            "",
        ]
        for call in result.get("tool_calls") or []:
            lines.append(f"[ACTION] {call.get('tool', '')}")
            lines.append(f"[OBSERVATION] ok={call.get('ok', False)}")
        lines.append("")
        lines.append("[FINAL] " + str(result.get("final_answer") or ""))
        (run_dir / "trajectory.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _safe(run_id: str) -> str:
    return run_id.replace("/", "__").replace("\\", "__")


class VeyaDemoProposer:
    """harness2 ``DemoProposer`` 的 Veya 版本：无模型调用、不改 harness。"""

    def __init__(self, spec) -> None:
        self.spec = spec
        self._calls: list[Any] = []

    def call(
        self,
        mission: str,
        context_dir: Path | str | None,
        label: str,
        **kwargs: Any,
    ) -> Any:
        core = import_harness2_core()
        context = Path(context_dir) if context_dir is not None else None
        workspace = None
        if label.startswith("experience."):
            text = "NO_ENTRY"
        elif label.startswith("select."):
            candidates = sorted((context / "candidates").glob("*.md")) if context else []
            chosen = candidates[0].stem if candidates else "A"
            text = f"<why>Phase 0 keeps the base harness.</why>\n```choice\n{chosen}\n```"
        else:
            assert context is not None
            workspace = context / "harness"
            workspace.mkdir(parents=True, exist_ok=True)
            # harness2 的 context.build 已把 base harness 复制到 context/harness；
            # Phase 0 原样保留，不提出编辑。
            text = "<notes>Phase 0 keeps the base harness; no edit is proposed.</notes>"
        reply = core.proposer.Reply(
            label=label,
            text=text,
            ok=True,
            attempts=1,
            workspace=workspace,
        )
        self._calls.append(reply)
        return reply

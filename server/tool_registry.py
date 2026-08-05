"""Veya: 全局能力注册表 (Master Tool Registry)。

把后端物理能力翻译成"大模型能听懂的语言" — JSON Schema (Function Calling 协议)。
主脑 (MasterCoordinator) 通过本注册表看到所有可用武器,并在模型决定调用时
动态派发到真实物理实现。

设计要点:
- 零前端感知: 前端只发文本、收 SSE 流;新增能力只改这里。
- Plug & Play: 新能力 = 一个 Python 函数 + 一次 register(),大模型瞬间"学会"。
- 注册时自动检测 async 函数;execute 统一 await,调用方无感。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = __import__("logging").getLogger("master.tools")


class ToolExecutionError(RuntimeError):
    """工具执行失败 → 由主脑捕获并回喂模型反思(不直接暴露给用户)。"""


def _to_str(result: Any, limit: int = 8000) -> str:
    """工具结果统一转字符串(截断防 Token 爆炸)。"""
    if isinstance(result, str):
        text = result
    elif isinstance(result, (dict, list)):
        try:
            text = json.dumps(result, ensure_ascii=False, indent=2)
        except TypeError:
            text = str(result)
    else:
        text = str(result)
    return text[:limit] + (f"\n... [truncated {len(text) - limit} chars]" if len(text) > limit else "")


class MasterToolRegistry:
    """全局能力注册表: 物理函数 ↔ 大模型可见的 JSON Schema 双向映射。"""

    def __init__(self) -> None:
        self._functions: dict[str, Callable] = {}
        self._schemas: list[dict] = []
        self._descriptions: dict[str, str] = {}
        self._result_limits: dict[str, int] = {}  # 工具名 → 结果截断上限(协处理器需大上限)

    # ── 注册 ─────────────────────────────────────────────────────────
    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        func: Callable,
        *,
        max_result_chars: int = 8000,
    ) -> None:
        """注册一个能力,使其对大模型可见。

        Args:
            name: 工具名(大模型调用时使用, 小写蛇形)。
            description: 认知描述(大模型靠它决定何时调用 — 写清触发条件)。
            parameters: JSON Schema 对象, 形如 {"type": "object", "properties": {...}, "required": [...]}。
            func: 物理实现(同步或 async 均可)。
            max_result_chars: 结果回喂大模型前的截断上限(浓缩 JSON 类工具需调大)。
        """
        if not name or not callable(func):
            raise ValueError("register requires a non-empty tool name and a callable")
        if name in self._functions:
            raise ValueError(f"Tool '{name}' already registered — 名字冲突会混淆大模型")
        # 归一化 parameters: 允许只传 properties 的简写
        params = dict(parameters)
        params.setdefault("type", "object")
        if "properties" not in params:
            raise ValueError(f"Tool '{name}': parameters must include 'properties'")

        self._functions[name] = func
        self._descriptions[name] = description
        self._result_limits[name] = max_result_chars
        self._schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": params,
                },
            }
        )

    def unregister(self, name: str) -> None:
        if name in self._functions:
            del self._functions[name]
            del self._descriptions[name]
            self._schemas = [s for s in self._schemas if s["function"]["name"] != name]

    # ── 查询 ─────────────────────────────────────────────────────────
    def get_all_schemas(self) -> list[dict]:
        """暴露给大模型的全部认知描述 (Function Calling 协议)。"""
        return list(self._schemas)

    def list_tools(self) -> list[str]:
        return sorted(self._functions)

    def has(self, name: str) -> bool:
        return name in self._functions

    def describe(self, name: str) -> str:
        """单行摘要(注入 System Prompt 的 SOP 用): "name — description"。"""
        return f"{name} — {self._descriptions.get(name, '')}"

    def to_dict(self) -> dict:
        return {
            "tools": [
                {"name": s["function"]["name"], "description": s["function"]["description"]}
                for s in self._schemas
            ]
        }

    def __len__(self) -> int:
        return len(self._functions)

    # ── 执行 ─────────────────────────────────────────────────────────
    async def execute(self, name: str, kwargs: dict) -> str:
        """执行物理函数,返回字符串结果。async 函数自动 await。

        Raises:
            ToolExecutionError: 工具不存在或执行抛异常(由主脑回喂反思)。
        """
        func = self._functions.get(name)
        if func is None:
            raise ToolExecutionError(
                f"Tool '{name}' not found. Available: {', '.join(self.list_tools())}"
            )
        try:
            raw = func(**kwargs)
            if inspect.isawaitable(raw):
                raw = await raw
            return _to_str(raw, limit=self._result_limits.get(name, 8000))
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"tool '{name}' failed: {type(exc).__name__}: {exc}") from exc


# =========================================================================
# 实例化并挂载 Veya 后端能力
# =========================================================================

master_tools = MasterToolRegistry()

# 3O 主库根(Genesis 默认)
_DEFAULT_LIBRARY_ROOT = Path(__file__).resolve().parent.parent / "platform" / "3O"


def _resolve_workspace_root() -> Path:
    """工具读写文件的根: 优先 VEYA_WORKSPACE env, 默认项目根。"""
    return Path(os.environ.get("VEYA_WORKSPACE", str(Path(__file__).resolve().parent.parent))).resolve()


def _resolve_path(filepath: str, *, must_exist: bool = True) -> Path:
    """路径安全: 拒绝逃逸工作区根。"""
    root = _resolve_workspace_root()
    p = Path(filepath)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    if p != root and root not in p.parents:
        raise ToolExecutionError(f"path '{filepath}' escapes workspace root '{root}'")
    if must_exist and not p.exists():
        raise ToolExecutionError(f"file not found: {filepath}")
    return p


# ── 1. 外部世界感知 (浏览器自动化, Playwright 真实接入) ─────────────
async def _tool_browser_run(
    url: str,
    action: str = "extract_text",
    selector: str | None = None,
    text: str | None = None,
    timeout_ms: int = 30000,
) -> str:
    """Playwright 无头浏览器: 访问 URL 并执行单个动作。"""
    try:
        from veya.oprim.browser import (
            action_click,
            action_extract_html,
            action_extract_text,
            action_navigate,
            action_screenshot,
            action_type,
        )
        from veya.oskill.browser import BrowserSession
    except ImportError as exc:  # pragma: no cover — 依赖缺失
        raise ToolExecutionError(f"playwright 未安装,无法执行 browser_run: {exc}") from exc

    session = BrowserSession(headless=True)
    try:
        await session.start()
        navigate = await session.execute_sequence([action_navigate(url)])
        if not navigate or not getattr(navigate[-1], "success", True):
            raise ToolExecutionError(f"browser_run: 导航失败 {url}")
        actions = {
            "extract_text": lambda: action_extract_text(selector),
            "extract_html": lambda: action_extract_html(selector),
            "screenshot": lambda: action_screenshot(selector),
            "click": lambda: action_click(selector or "body"),
            "type": lambda: action_type(selector or "input", text or ""),
        }
        if action not in actions:
            raise ToolExecutionError(
                f"browser_run: 未知 action '{action}'. Available: {', '.join(actions)}"
            )
        results = await session.execute_sequence([actions[action]()])
        last = results[-1]
        return json.dumps(
            {
                "url": last.page_url or url,
                "title": last.page_title,
                "text": last.text[:4000],
                "screenshot_base64": (last.screenshot_base64[:200] + "...") if last.screenshot_base64 else "",
                "success": last.success,
            },
            ensure_ascii=False,
        )
    finally:
        with contextlib.suppress(Exception):  # stop 失败不掩盖主结果
            await asyncio.wait_for(session.stop(), timeout=10)


# ── 2. 委派给 Genesis (3O 核心研发) ────────────────────────────────
_genesis_factory: Callable[[], Any] | None = None


def set_genesis_factory(factory: Callable[[], Any] | None) -> None:
    """注入 Genesis 构造工厂(测试替换 / 延迟构造)。None 恢复默认。"""
    global _genesis_factory
    _genesis_factory = factory


def _make_genesis_agent() -> Any:
    """默认: 从 .env / 环境变量构造 Genesis(专属 key 已配置时)。"""
    from server.agents.genesis_agent import GenesisAgent

    try:
        from config.loader import _load_dotenv

        _load_dotenv()
    except Exception:
        pass
    return GenesisAgent(library_root=_DEFAULT_LIBRARY_ROOT)


async def _tool_delegate_to_genesis(requirement_json: str) -> str:
    """唤醒 Genesis Agent,把已确认的 PRD 交给 3O 引擎执行。"""
    try:
        requirement = json.loads(requirement_json)
        if not isinstance(requirement, (dict, list)):
            raise ValueError("requirement_json 必须是 JSON 对象或数组")
    except json.JSONDecodeError as exc:
        raise ToolExecutionError(f"delegate_to_genesis: requirement_json 不是合法 JSON: {exc}") from exc

    factory = _genesis_factory or _make_genesis_agent
    try:
        agent = factory()
    except ValueError as exc:
        raise ToolExecutionError(
            f"Genesis 未就绪(专属 API Key 未配置): {exc}. 请先在 .env 设置 GENESIS_API_KEY。"
        ) from exc

    mission = json.dumps(requirement, ensure_ascii=False)
    result = await agent.handle_mission(mission)
    if result.get("status") != "success":
        raise ToolExecutionError(
            f"Genesis mission failed: {result.get('error', 'unknown')} "
            f"(steps={result.get('steps')}, ledger={len(agent.memory.memory['element_ledger'])})"
        )
    return json.dumps(
        {
            "status": "success",
            "response": result.get("response", ""),
            "steps": result.get("steps"),
            "ledger": agent.memory.memory["element_ledger"],
        },
        ensure_ascii=False,
    )


# ── 3. 文件系统 / 代码理解 ─────────────────────────────────────────
def _tool_read_file_ast(filepath: str) -> str:
    """读取本地文件的 AST 骨架: 理解结构而不撑爆上下文窗口。"""
    from veya.ast import extract_skeleton

    path = _resolve_path(filepath)
    source = path.read_text(encoding="utf-8", errors="replace")
    return extract_skeleton(source, filepath)


def _tool_grep(pattern: str, glob: str | None = None, root: str | None = None) -> str:
    """在项目内搜索代码(ripgrep),定位定义与引用。"""
    from server.assembly import ripgrep_search

    base = _resolve_workspace_root()
    search_root = str(base / root) if root else str(base)
    try:
        hits = ripgrep_search(pattern, root=search_root, glob=glob)
    except FileNotFoundError as exc:
        raise ToolExecutionError("ripgrep (rg) 未安装,无法执行 grep") from exc
    if not hits:
        return f"no matches for {pattern!r}"
    lines = []
    for hit in hits[:40]:
        data = hit.get("data", {})
        path = (data.get("path") or {}).get("text", "?")
        line_no = data.get("line_number", "?")
        text = (data.get("lines") or {}).get("text", "").rstrip("\n")
        lines.append(f"{path}:{line_no}: {text}")
    return "\n".join(lines)


def _tool_list_files(path: str = ".") -> str:
    """列出工作区文件(排除噪音目录)。"""
    root = _resolve_workspace_root()
    target = root if path in (".", "") else _resolve_path(path, must_exist=False)
    excluded = {
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".coverage",
        "dist",
        "build",
    }
    lines = []
    count = 0
    for p in sorted(target.rglob("*")):
        if any(part in excluded for part in p.parts):
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        lines.append(f"{rel}/" if p.is_dir() else f"{rel} ({p.stat().st_size}b)")
        count += 1
        if count >= 200:
            lines.append("... (truncated)")
            break
    return "\n".join(lines) or "(empty)"


# ── 4. 代码执行 / 测试 (3O 隔离沙箱) ───────────────────────────────
async def _tool_run_in_sandbox(code: str | None = None, command: str | None = None) -> str:
    """在 3O 隔离沙箱中执行代码(网络封锁/内存/时间限制, 单线程 BLAS)。"""
    from veya.sandbox import SandboxConfig, create_safe_executor

    if not code and not command:
        raise ToolExecutionError("run_in_sandbox requires either 'code' (python source) or 'command' (shell)")
    config = SandboxConfig(
        time_limit=30.0,
        memory_limit=1024 * 1024 * 1024,
        network_blocked=True,
        audit_enabled=True,
        env_extra={
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        },
    )
    executor = create_safe_executor(config)
    async with executor:
        result = await executor.run_script(code) if code else await executor.execute(command)
    if result.get("exit_code") != 0:
        raise ToolExecutionError(
            f"exit_code={result.get('exit_code')} ({result.get('duration', 0.0):.2f}s)\n"
            f"stdout:\n{result.get('stdout', '')}\n"
            f"stderr:\n{result.get('stderr', '')}"
        )
    return f"exit_code=0 ({result.get('duration', 0.0):.2f}s)\n{result.get('stdout', '')}"


# ── 5. Genesis 记忆账本查询 ────────────────────────────────────────
def _tool_search_genesis_ledger(query: str) -> str:
    """查询 Genesis 的永久记忆账本: 3O 库里已有哪些元素(锻造前先查, 避免重复造轮子)。"""
    from server.agents.genesis_memory import GenesisMemory

    memory = GenesisMemory()
    hits = memory.search_elements(query)
    if not hits:
        return f"Genesis 账本中没有与 '{query}' 匹配的元素"
    return json.dumps(hits, ensure_ascii=False, indent=2)


# ── 6. 量化交火协议 (控制面/数据面分离) ─────────────────────────────
def _tool_get_market_data_schema(asset_id: str) -> str:
    """元数据注入: 只把 Schema + 前 5 行喂给大模型(全量数据绝不进上下文)。"""
    from server.quant_coprocessor import get_market_data_schema as _schema

    try:
        return _schema(asset_id)
    except FileNotFoundError as exc:
        raise ToolExecutionError(str(exc)) from exc


async def _tool_run_backtest_coprocessor(
    strategy_code: str, asset_id: str, start_date: str, end_date: str
) -> str:
    """时序协处理器: 在隔离沙箱中对海量数据执行策略, 只返回浓缩指标 + 图表 JSON。"""
    from server.quant_coprocessor import QuantCoprocessor

    coprocessor = QuantCoprocessor()
    return await coprocessor.execute_strategy(
        strategy_code=strategy_code,
        asset_id=asset_id,
        start_date=start_date,
        end_date=end_date,
    )


# ================= 挂载 =================
master_tools.register(
    name="browser_run",
    description=(
        "Control a headless browser to visit a URL, scrape data, or interact with a webpage. "
        "Use this when the user asks for latest news, social media posts, or live web data."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "目标 URL"},
            "action": {
                "type": "string",
                "description": "extract_text | extract_html | screenshot | click | type (default extract_text)",
            },
            "selector": {"type": "string", "description": "CSS/text 选择器 (optional)"},
            "text": {"type": "string", "description": "type action 的输入文本 (optional)"},
            "timeout_ms": {"type": "integer", "description": "导航超时 (optional)"},
        },
        "required": ["url"],
    },
    func=_tool_browser_run,
)

master_tools.register(
    name="delegate_to_genesis",
    description=(
        "Trigger the 3O Engine Genesis workflow. USE THIS ONLY WHEN the user explicitly "
        "confirms a Requirement Document (PRD) to build mathematical models, operators, "
        "or core system components. Genesis is the sovereign 3O librarian with permanent memory."
    ),
    parameters={
        "type": "object",
        "properties": {
            "requirement_json": {"type": "string", "description": "The approved PRD as a JSON string"}
        },
        "required": ["requirement_json"],
    },
    func=_tool_delegate_to_genesis,
)

master_tools.register(
    name="read_file_ast",
    description=(
        "Read the AST skeleton of a local file (signatures + line ranges, no bodies) to "
        "understand its structure without blowing up the context window."
    ),
    parameters={
        "type": "object",
        "properties": {"filepath": {"type": "string", "description": "path relative to workspace root"}},
        "required": ["filepath"],
    },
    func=_tool_read_file_ast,
)

master_tools.register(
    name="grep",
    description="Search code in the workspace with ripgrep to locate definitions and usages.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "regex pattern"},
            "glob": {"type": "string", "description": "rg glob filter, e.g. '*.py' (optional)"},
            "root": {"type": "string", "description": "subdirectory relative to workspace root (optional)"},
        },
        "required": ["pattern"],
    },
    func=_tool_grep,
)

master_tools.register(
    name="list_files",
    description="List files under a directory of the workspace (noise dirs excluded).",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "directory relative to workspace root (optional)"}}
    },
    func=_tool_list_files,
)

master_tools.register(
    name="run_in_sandbox",
    description=(
        "Run python code (or a shell command) inside the 3O isolated sandbox: network blocked, "
        "memory/time limited. Use to test code snippets instead of asking the user to run them."
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "python source to execute (optional)"},
            "command": {"type": "string", "description": "shell command to execute (optional)"},
        }
    },
    func=_tool_run_in_sandbox,
)

master_tools.register(
    name="search_genesis_ledger",
    description=(
        "Query Genesis's permanent memory ledger for existing 3O elements "
        "(check before requesting new operator development to avoid duplicates)."
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "keyword, e.g. '均线' or 'ema'"}},
        "required": ["query"],
    },
    func=_tool_search_genesis_ledger,
)

master_tools.register(
    name="get_market_data_schema",
    description=(
        "Quant protocol step 1: fetch ONLY the schema + first 5 rows of market data for an asset "
        "(columns, dtypes, sample). Use this BEFORE writing any backtest strategy code — "
        "the full dataset is NEVER exposed to you; it is computed in the sandbox."
    ),
    parameters={
        "type": "object",
        "properties": {
            "asset_id": {"type": "string", "description": "asset symbol, e.g. 'AAPL' or 'BTCUSDT'"}
        },
        "required": ["asset_id"],
    },
    func=_tool_get_market_data_schema,
)

master_tools.register(
    name="run_backtest_coprocessor",
    description=(
        "Quant protocol step 2: execute a strategy in the isolated quant sandbox against the FULL "
        "dataset (millions of rows) and return ONLY condensed metrics (total_return / sharpe_ratio / "
        "max_drawdown) plus downsampled echarts chart data. Your strategy code MUST define "
        "run_strategy(df) -> df with 'daily_return' and 'cum_return' columns. "
        "NEVER try to load or compute market data yourself — you are the strategy expresser, "
        "the coprocessor is the calculator."
    ),
    parameters={
        "type": "object",
        "properties": {
            "strategy_code": {"type": "string", "description": "python code defining run_strategy(df)"},
            "asset_id": {"type": "string"},
            "start_date": {"type": "string", "description": "e.g. '2022-01-01'"},
            "end_date": {"type": "string", "description": "e.g. '2024-12-31'"},
        },
        "required": ["strategy_code", "asset_id", "start_date", "end_date"],
    },
    func=_tool_run_backtest_coprocessor,
    max_result_chars=40000,  # 浓缩 JSON(含 500 点图表数据)必须完整回喂
)

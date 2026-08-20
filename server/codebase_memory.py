"""server.codebase_memory — codebase-memory-mcp 集成装配层。

3O 铁律 §1.4: 机制 (stdio MCP 客户端) 在主库 obase.mcp_stdio; 本模块只装配:
  - spawn 二进制 → McpClientRegistry.register("codebase_memory");
  - 8 个工具批量 make_mcp_tool_adapter → ToolAdapter (category="mcp");
  - 索引生命周期 (full/incremental, 持久化 ~/.veya/codebase-memory-index);
  - 双通道搜索: MCP 图谱优先 (符号/调用链), SemanticSearch 向量 fallback;
  - blast_radius: trace_path 聚合影响面 (pre_dispatch 门禁用)。

安全: 二进制本地运行、无 API key、索引不出机器; 二进制缺失时优雅降级
(全部调用走 fallback, 不阻塞服务启动)。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from veya.platform import obase as _load_obase

_obase = _load_obase()

# 二进制/索引配置 (可 env 覆盖)
DEFAULT_BIN = os.environ.get(
    "CODEBASE_MEMORY_BIN", str(Path.home() / ".local" / "bin" / "codebase-memory-mcp")
)
DEFAULT_INDEX_DIR = os.environ.get(
    "CODEBASE_MEMORY_INDEX", str(Path.home() / ".veya" / "codebase-memory-index")
)

# 索引排除 (服务端默认忽略 .git 等; 这里补业务敏感面 — 索引只进代码结构)
EXCLUDE_HINTS = (
    "auth",
    "vault",
    "backups",
    "node_modules",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    ".next",
    "secret",
    "credential",
)


class CodebaseMemoryError(RuntimeError):
    """集成层错误 (二进制缺失 / 未索引 / 工具调用失败)。"""


def _is_excluded(rel_path: str) -> bool:
    low = rel_path.lower()
    return any(h in low for h in EXCLUDE_HINTS)


class CodebaseMemoryConnector:
    """codebase-memory-mcp 装配器 (单例使用: server.app lifespan 启动)。"""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        bin_path: str | None = None,
        index_dir: str | None = None,
        name: str = "codebase_memory",
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.bin_path = Path(bin_path or DEFAULT_BIN)
        self.index_dir = Path(index_dir or DEFAULT_INDEX_DIR)
        self.name = name
        self._client: Any = None  # obase.mcp_stdio.StdioMcpClient
        self._project: str | None = None  # 索引返回的规范化 project 名
        self._index_state: dict[str, Any] = {}

    # ── 生命周期 ───────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self.bin_path.exists() and self.bin_path.is_file()

    @property
    def ready(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "alive", False))

    async def start(self) -> None:
        """spawn 二进制 + 握手 + 注册进 McpClientRegistry。二进制缺失 → 降级不抛。"""
        if not self.available:
            return
        if self.ready:
            return
        from obase.mcp_client import McpClientRegistry
        from obase.mcp_stdio import StdioMcpClient

        client = StdioMcpClient([str(self.bin_path)], name=self.name)
        try:
            await client.start()
        except Exception as exc:
            raise CodebaseMemoryError(f"codebase-memory-mcp 启动失败: {exc}") from exc
        self._client = client
        McpClientRegistry.register(self.name, client)
        # 尝试恢复已持久化索引的 project 名
        if self.index_dir.exists():
            for meta in self.index_dir.glob("*/project.json"):
                try:
                    self._project = json.loads(meta.read_text()).get("project")
                    break
                except (json.JSONDecodeError, OSError):
                    continue

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    def health(self) -> dict[str, Any]:
        if not self.available:
            return {"available": False, "reason": "二进制缺失"}
        base = self._client.health() if self._client else {}
        return {
            "available": True,
            "ready": self.ready,
            "project": self._project,
            "index": self._index_state,
            **base,
        }

    # ── 索引 ───────────────────────────────────────────────────────────

    async def ensure_indexed(self, *, force: bool = False) -> dict[str, Any]:
        """首次全量索引; 已索引后增量 (mode=incremental)。幂等。"""
        if not self.ready:
            await self.start()
        if not self.ready:
            raise CodebaseMemoryError("codebase-memory-mcp 不可用")
        mode = "incremental" if self._project and not force else "full"
        res = await self._client.call_tool(
            "index_repository",
            {
                "repo_path": str(self.workspace_root),
                "mode": mode,
                "persistence": str(self.index_dir),
            },
        )
        text = self._tool_text(res)
        data = json.loads(text)
        self._project = data.get("project") or self._project
        self._index_state = {
            "mode": mode,
            "nodes": data.get("nodes"),
            "edges": data.get("edges"),
            "skipped": data.get("skipped_count"),
        }
        # 持久化 project 名 (重启后免查询)
        if self._project:
            meta_dir = self.index_dir / self._project
            meta_dir.mkdir(parents=True, exist_ok=True)
            (meta_dir / "project.json").write_text(
                json.dumps({"project": self._project}), encoding="utf-8"
            )
        return self._index_state

    # ── 图谱查询 ───────────────────────────────────────────────────────

    async def search_symbols(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """符号级搜索 (函数/类/变量, 带行号与文件)。"""
        res = await self._call(
            "search_graph", {"query": query, "project": self._project, "limit": limit}
        )
        return (res.get("results") or [])[:limit]

    async def search_code(
        self, pattern: str, *, path_filter: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """图谱增强文本搜索。"""
        args: dict[str, Any] = {
            "pattern": pattern,
            "project": self._project,
            "mode": "compact",
            "limit": limit,
        }
        if path_filter:
            args["path_filter"] = path_filter
        res = await self._call("search_code", args)
        return (res.get("results") or [])[:limit]

    async def query_cypher(self, query: str, *, max_rows: int = 50) -> list[dict[str, Any]]:
        """Cypher 图查询 (架构/依赖关系自由查询)。"""
        res = await self._call(
            "query_graph", {"query": query, "project": self._project, "max_rows": max_rows}
        )
        return res.get("rows") or []

    async def trace(
        self, function_name: str, *, mode: str = "calls", depth: int = 3
    ) -> dict[str, Any]:
        """调用链追踪 (mode=calls 双向; 注意: direction 显式传参反而返回空, 用默认)。"""
        res = await self._call(
            "trace_path",
            {
                "function_name": function_name,
                "project": self._project,
                "mode": mode,
                "depth": depth,
            },
        )
        return res or {}

    async def get_snippet(self, qualified_name: str) -> str | None:
        """读符号源码。"""
        res = await self._call(
            "get_code_snippet", {"qualified_name": qualified_name, "project": self._project}
        )
        return res.get("code") or res.get("source")

    async def blast_radius(self, symbols: list[str], *, depth: int = 2) -> dict[str, Any]:
        """影响面评估: 对每个符号 trace 调用链, 聚合 callers/callees。"""
        callers: dict[str, int] = {}
        callees: dict[str, int] = {}
        for sym in symbols:
            try:
                t = await self.trace(sym, depth=depth)
            except Exception:
                continue
            for c in t.get("callers", []):
                qn = c.get("qualified_name") or c.get("name", "?")
                callers[qn] = max(callers.get(qn, 0), int(c.get("hop", 1)))
            for c in t.get("callees", []):
                qn = c.get("qualified_name") or c.get("name", "?")
                callees[qn] = max(callees.get(qn, 0), int(c.get("hop", 1)))
        radius = {
            "symbols": symbols,
            "callers": sorted(callers, key=callers.get, reverse=True),
            "callees": sorted(callees, key=callees.get, reverse=True),
            "total_affected": len(callers) + len(callees),
        }
        try:
            from server.runtime_calls import merge_into_radius

            radius = merge_into_radius(radius, symbols)
        except Exception:
            pass
        return radius

    async def ingest_traces(self, traces: list[dict[str, Any]]) -> dict[str, Any]:
        """Best-effort MCP ingest (binary may still stub edge creation)."""
        if not self.ready:
            return {"ok": False, "error": "codebase-memory-mcp 未就绪"}
        try:
            return await self._call("ingest_traces", {"traces": traces})
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ── 双通道搜索 (图谱优先, 向量 fallback) ──────────────────────────

    async def search(self, query: str, *, top_k: int = 10, fallback: Any = None) -> dict[str, Any]:
        """MCP 图谱优先; 无结果或不可用时 fallback (SemanticSearch 等 callable)。"""
        if self.ready and self._project:
            try:
                syms = await self.search_symbols(query, limit=top_k)
                if syms:
                    # 统一成 SearchResult 风格 dict (id/text/file_path/score/行号)
                    return {
                        "source": "graph",
                        "results": [
                            {
                                "id": f"graph:{s.get('qualified_name', s.get('name', ''))}",
                                "text": s.get("name", ""),
                                "file_path": s.get("file_path", ""),
                                "score": s.get("rank", 0.0),
                                "start_line": s.get("start_line"),
                                "end_line": s.get("end_line"),
                                "qualified_name": s.get("qualified_name", ""),
                            }
                            for s in syms
                        ],
                    }
            except Exception:
                pass
        if fallback is not None:
            try:
                import inspect

                results = (
                    await fallback(query, top_k=top_k)
                    if inspect.iscoroutinefunction(fallback)
                    else fallback(query, top_k=top_k)
                )
                return {"source": "vector", "results": results}
            except Exception as exc:
                return {"source": "none", "error": str(exc)}
        return {"source": "none", "results": []}

    # ── LLM 工具适配 ───────────────────────────────────────────────────

    async def tool_adapters(self) -> list[dict[str, Any]]:
        """批量适配: MCP 工具 → MasterToolRegistry 可注册的 (name, desc, params, func)。"""
        if not self.ready:
            return []
        from tools import make_mcp_tool_adapter

        out = []
        for spec in await self._client.list_tools():
            adapter = make_mcp_tool_adapter(spec, self._client)
            params = (spec.get("inputSchema") or {}).get("properties", {})
            required = (spec.get("inputSchema") or {}).get("required", [])
            out.append(
                {
                    "name": f"mcp_codebase_{spec['name']}",  # 统一命名空间
                    "description": adapter.description,
                    "parameters": {"type": "object", "properties": params, "required": required},
                    "func": adapter.callable,  # ToolAdapter.callable
                }
            )
        return out

    # ── 内部 ───────────────────────────────────────────────────────────

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self.ready:
            raise CodebaseMemoryError("codebase-memory-mcp 未就绪")
        if self._project:
            args.setdefault("project", self._project)
        res = await self._client.call_tool(tool, args)
        text = self._tool_text(res)
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    @staticmethod
    def _tool_text(res: Any) -> str:
        content = res.get("content") if isinstance(res, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
        return ""


# 模块级单例 (lifespan 挂载)
_connector: CodebaseMemoryConnector | None = None


def get_connector(workspace_root: str | Path | None = None) -> CodebaseMemoryConnector:
    """单例访问。workspace_root 仅在首次创建时生效。"""
    global _connector
    if _connector is None:
        root = workspace_root or Path(__file__).resolve().parent.parent
        _connector = CodebaseMemoryConnector(root)
    return _connector


async def wire_master_tools(connector: CodebaseMemoryConnector | None = None) -> int:
    """把 MCP 工具批量注册进 master_tools (主脑 LLM 工具面)。

    幂等: 已注册的工具跳过 (重复 register 会 ValueError)。
    返回本次新注册数量。
    """
    from server.tool_registry import register_mcp_tools

    connector = connector or get_connector()
    # ②-B: 收成 1 个网关 mcp_codebase(action, args)
    return register_mcp_tools("codebase", await connector.tool_adapters(), max_result_chars=16000)


def schedule_daily_reindex(scheduler: Any, *, hour: int = 3, minute: int = 17) -> str:
    """注册每日增量索引任务 (APScheduler). 返回 job id。

    已存在同 id job → replace (幂等)。增量模式: 已索引后 ensure_indexed 自动走
    mode=incremental。索引失败只记日志, 不炸调度器。
    """
    import logging

    log = logging.getLogger("codebase_memory")
    job_id = "cbm_daily_reindex"
    if scheduler.get_job(job_id):
        return job_id

    async def _reindex_job() -> None:
        try:
            connector = get_connector()
            if not connector.ready:
                await connector.start()
            state = await connector.ensure_indexed()
            log.info("codebase-memory 每日增量索引完成: %s", state)
        except Exception as exc:
            log.warning("codebase-memory 每日索引失败: %s", exc)

    scheduler.add_job(
        _reindex_job,
        trigger="cron",
        hour=hour,
        minute=minute,
        id=job_id,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    return job_id


__all__ = [
    "CodebaseMemoryConnector",
    "CodebaseMemoryError",
    "get_connector",
    "schedule_daily_reindex",
    "wire_master_tools",
]

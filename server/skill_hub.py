"""Veya SkillHub: 动态技能挂载与生命周期管理中枢。

像一个"小型的 Docker 引擎": 系统启动或收到热重载指令时,扫描本地目录
(~/.veya/skills/),把符合规范的技能包 (Python 脚本 / MCP 服务) 动态转化为
大模型可调用的 Function Schema,并向主脑 (MasterCoordinator) 暴露统一调用接口。

技能包物理结构:
    ~/.veya/skills/
    ├── weather_fetcher/        # 本地 Python 技能包
    │   ├── manifest.json       # 技能元数据 (Schema 描述)
    │   ├── requirements.txt    # (可选) 依赖
    │   └── run.py              # 执行入口 (必须包含 main(**kwargs))
    └── internal_jira_mcp/      # MCP 协议技能包
        └── manifest.json       # type="mcp", endpoint=服务连接地址

manifest.json 是大模型认识这个技能的唯一凭证。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from server.skill_scan import scan_skill_source
from server.skill_scan import summarize as _summarize_risk
from server.tool_guard import ToolDenied as _ToolDenied
from server.tool_guard import global_tool_guard as _tool_guard
from server.tool_registry import (
    ToolExecutionError,
    _invoke_callback,
    _schema_validator,
    _validate_arguments,
)

logger = logging.getLogger("skillhub")


def _oskill_mod() -> Any:
    """惰性取 3O oskill 主库 (单一来源: 契约校验/元路由原语)。

    缺失 (未挂载 submodule / 精简运行时) → 返回 None, 调用方优雅降级为现行行为
    (契约字段不解析、路由回退全量目录), 绝不因 3O 不可用而崩 SkillHub。
    """
    try:
        from veya.platform import oskill as _load

        return _load()
    except Exception:
        return None


def _catalog_cap() -> int:
    """常驻 run_skill 目录最多内联多少条 (超出走 list_skills 按需)。env 可调, 下限 1。"""
    try:
        return max(1, int(os.environ.get("VEYA_SKILL_CATALOG_CAP", "30")))
    except ValueError:
        return 30


_DEFAULT_SKILLS_DIR = str(Path.home() / ".veya" / "skills")
_MANIFEST_NAME = "manifest.json"
_MAX_RESULT_CHARS = 8000


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def _accepted_kwargs(fn: Callable, kwargs: dict) -> dict:
    """按目标函数签名过滤 kwargs — 技能 main() 收得下才传。

    约定是 ``main(**kwargs)``, 但真实技能可能写成 ``main()`` / ``main(goal)`` /
    ``main(a, b)``。dispatcher 的 goal 归一化会注入 ``goal``, 若 main() 不收该参
    就会 ``TypeError: unexpected keyword``。这里:
    - main 含 ``**kwargs`` (VAR_KEYWORD) → 原样全传;
    - 否则只传 main 声明的具名参数, 丢弃多余键 (缺必填参再由 main 自身报错)。
    签名不可内省 (builtin/C) → 原样传 (保持旧行为)。
    """
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return kwargs
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return kwargs
    accepted = {
        p.name
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return {k: v for k, v in kwargs.items() if k in accepted}


class VeyaSkillHub:
    """动态技能枢纽: 扫描 → 解析 Manifest → 挂载执行器 → 暴露统一接口。"""

    def __init__(self, skills_dir: str | Path | None = None):
        # env 覆盖 > 参数 > 默认 ~/.veya/skills
        self.skills_dir = Path(
            skills_dir or os.environ.get("VEYA_SKILLS_DIR", _DEFAULT_SKILLS_DIR)
        ).expanduser()
        self._schemas: list[dict] = []
        self._executors: dict[str, Callable] = {}
        self._validators: dict[str, Any] = {}
        self._descriptions: dict[str, str] = {}
        self._skills: dict[str, dict] = {}  # name → manifest 原始信息
        self._contracts: dict[str, dict] = {}  # name → 归一化技能契约 (when_to_use/verify/…)
        # ②-A 静态收口: dispatcher 模式下 N 个 per-skill 工具收成 2 个
        # (list_skills + run_skill), 主脑工具面 93→~23; VEYA_SKILL_DISPATCHER=0 回退。
        self._dispatcher = os.environ.get("VEYA_SKILL_DISPATCHER", "1") != "0"
        # 加载期安全扫描: 默认 strict (高危调用面拒载); VEYA_SKILL_SCAN_STRICT=0 回退
        # 只记录+告警 (2026-08-22 前的旧默认)。信任名单 (VEYA_SKILL_TRUSTED_NAMES,
        # 逗号分隔技能名) 是唯一的例外口子——host 侧配置, 不是技能自己 manifest 里
        # 能声明的字段 (否则技能自证"我可信"等于信任门形同虚设)。
        self._scan_strict = os.environ.get("VEYA_SKILL_SCAN_STRICT", "1") != "0"
        self._trusted_names: frozenset[str] = frozenset(
            n.strip()
            for n in os.environ.get("VEYA_SKILL_TRUSTED_NAMES", "").split(",")
            if n.strip()
        )
        self._risks: dict[str, dict] = {}  # name → 静态扫描风险摘要 (max_severity/categories/…)

        # 启动时自动扫描挂载
        self.reload_skills()

    # ── 热重载 ───────────────────────────────────────────────────────
    def reload_skills(self) -> dict[str, int]:
        """扫描目录,热重载所有技能。返回 {loaded, skipped, errors}。"""
        self.skills_dir.mkdir(parents=True, exist_ok=True)

        self._schemas.clear()
        self._executors.clear()
        self._validators.clear()
        self._descriptions.clear()
        self._skills.clear()
        self._contracts.clear()
        self._risks.clear()

        stats = {"loaded": 0, "skipped": 0, "errors": 0}
        for item in sorted(self.skills_dir.iterdir()):
            if item.is_dir():
                ok = self._load_skill(item)
                if ok:
                    stats["loaded"] += 1
                else:
                    stats["skipped"] += 1
            elif item.name == _MANIFEST_NAME:
                # 允许技能包平铺为单文件 manifest(极简技能)
                ok = self._load_skill(self.skills_dir)
                if ok:
                    stats["loaded"] += 1
                else:
                    stats["skipped"] += 1

        logger.info("[SkillHub] 挂载 %d 个动态技能 (skipped=%d)", stats["loaded"], stats["skipped"])
        return stats

    # ── 技能加载 ─────────────────────────────────────────────────────
    def _load_skill(self, skill_path: Path) -> bool:
        """解析并挂载单个技能包。成功返回 True。"""
        manifest_path = skill_path / _MANIFEST_NAME
        if not manifest_path.exists():
            logger.debug("[SkillHub] %s 缺少 %s,跳过", skill_path.name, _MANIFEST_NAME)
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("[SkillHub] %s manifest 解析失败: %s", skill_path, exc)
            return False

        # ── Manifest 校验(大模型认识技能的唯一凭证) ──
        name = manifest.get("name", "")
        description = manifest.get("description", "")
        parameters = manifest.get("parameters")
        if not name or not description:
            logger.warning("[SkillHub] %s manifest 缺少 name/description,跳过", skill_path)
            return False
        if not isinstance(parameters, dict) or "properties" not in parameters:
            logger.warning("[SkillHub] 技能 '%s' 缺少 parameters.properties,跳过", name)
            return False
        skill_type = str(manifest.get("type", "python")).lower()

        # ── 注册执行器 ──
        risk: dict | None = None
        try:
            if skill_type == "python":
                entry_file = skill_path / str(manifest.get("entrypoint", "run.py"))
                # 加载期安全关口: 静态审查将被 exec_module 的源码 (顶层代码一并执行) —
                # 第三方技能等于任意代码执行入口。默认 strict 拒高危; 信任名单内的
                # 例外 (host 侧配置, 技能自己 manifest 声明不算数)。
                risk = self._scan_entry(name, entry_file)
                if risk["max_severity"] == "high" and self._scan_strict:
                    if name in self._trusted_names:
                        logger.info(
                            "[SkillHub] 技能 '%s' 命中高危调用面 %s, 但在信任名单内, strict 放行",
                            name,
                            risk["categories"],
                        )
                    else:
                        logger.warning(
                            "[SkillHub] 技能 '%s' 命中高危调用面 %s, strict 模式拒载 "
                            "(如确认可信, 把技能名加进 VEYA_SKILL_TRUSTED_NAMES)",
                            name,
                            risk["categories"],
                        )
                        return False
                executor = self._create_python_executor(name, entry_file)
            elif skill_type == "mcp":
                endpoint = manifest.get("endpoint")
                if not endpoint:
                    logger.warning("[SkillHub] MCP 技能 '%s' 缺少 endpoint,跳过", name)
                    return False
                executor = self._create_mcp_executor(name, str(endpoint))
            else:
                logger.warning("[SkillHub] 未知技能类型 '%s' in %s,跳过", skill_type, name)
                return False
        except Exception as exc:
            logger.warning("[SkillHub] 技能 '%s' 挂载失败: %s", name, exc)
            return False

        params = dict(parameters)
        params.setdefault("type", "object")
        try:
            validator = _schema_validator(params, owner=f"Skill '{name}'")
        except ValueError as exc:
            logger.warning("[SkillHub] 技能 '%s' schema 非法,跳过: %s", name, exc)
            return False

        # 重名技能: 后加载者覆盖(热更新部署场景), 记 warning 并替换旧 schema
        if name in self._executors:
            logger.warning("[SkillHub] 技能 '%s' 重复,后加载者覆盖", name)
            self._schemas = [s for s in self._schemas if s["function"]["name"] != name]

        self._executors[name] = executor
        self._validators[name] = validator
        self._descriptions[name] = description
        self._skills[name] = {
            "type": skill_type,
            "entrypoint": str(manifest.get("entrypoint", "run.py")),
            "endpoint": manifest.get("endpoint"),
            "path": str(skill_path),
            # 渐进加载: 技能可附一份 SKILL.md 详细说明。加载期只标记有无, 不读 body
            # (省常驻 token); 命中路由时才经 oskill.load_skill_progressive 拉取。
            "has_body": (skill_path / "SKILL.md").exists(),
        }
        if risk is not None:
            self._risks[name] = risk
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
        # 技能契约 (3O 单一来源): 解析可选契约字段 (when_to_use/verification/
        # red_flags/rationalizations) 并存储; 结构不合法只告警不拒载 (向后兼容——
        # 老技能没有这些字段, 契约为空, 行为不变)。3O 不可用则跳过 (优雅降级)。
        osk = _oskill_mod()
        if osk is not None:
            try:
                contract_errors = osk.validate_skill_contract(manifest)
                shape_errors = [e for e in contract_errors if "requires" not in e]
                if shape_errors:
                    logger.warning("[SkillHub] 技能 '%s' 契约字段结构告警: %s", name, shape_errors)
                self._contracts[name] = osk.extract_contract(manifest)
            except Exception as exc:
                logger.debug("[SkillHub] 技能 '%s' 契约解析跳过: %s", name, exc)
        logger.info("[SkillHub] 挂载技能 '%s' (type=%s)", name, skill_type)
        return True

    def _scan_entry(self, name: str, entry_file: Path) -> dict:
        """静态扫描技能 entrypoint, 返回风险摘要 (max_severity/categories/…)。

        文件读不到 → 空风险 (挂载/执行阶段各自会报缺失)。高危命中记 warning,
        让风险从"不可见"变"可见"; 是否拒载由 _scan_strict 决定 (调用方)。
        """
        try:
            source = entry_file.read_text(encoding="utf-8")
        except OSError:
            return _summarize_risk([])
        findings = scan_skill_source(source, filename=str(entry_file))
        risk = _summarize_risk(findings)
        if risk["high"]:
            highs = [f for f in findings if f["severity"] == "high"]
            logger.warning(
                "[SkillHub] 技能 '%s' 静态扫描高危 (%d): %s",
                name,
                risk["high"],
                "; ".join(f"{f['category']}@L{f['line']}:{f['detail']}" for f in highs[:5]),
            )
        return risk

    # ── 执行器工厂 ───────────────────────────────────────────────────
    def _create_python_executor(self, name: str, filepath: Path) -> Callable:
        """动态反射: 加载 Python 脚本并提取 main 函数。"""

        async def executor(**kwargs: Any) -> str:
            if not filepath.exists():
                raise ToolExecutionError(
                    f"Skill '{name}' execution file missing: {filepath} (请检查 entrypoint)"
                )
            # 动态 Import 模块(每次执行全新模块对象, 上下文隔离)
            spec = importlib.util.spec_from_file_location(f"veya_skill_{name}", filepath)
            if spec is None or spec.loader is None:
                raise ToolExecutionError(f"Skill '{name}': 无法加载 {filepath}")
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as exc:
                raise ToolExecutionError(
                    f"Skill '{name}' 加载失败: {type(exc).__name__}: {exc} "
                    f"(如依赖缺失, 请按 {filepath.parent / 'requirements.txt'} 安装)"
                ) from exc

            main_fn = getattr(module, "main", None)
            if not callable(main_fn):
                raise ToolExecutionError(
                    f"Skill '{name}' must implement a 'main(**kwargs)' function."
                )
            try:
                result = await _invoke_callback(main_fn, _accepted_kwargs(main_fn, kwargs))
            except Exception as exc:
                raise ToolExecutionError(f"Skill '{name}' main() failed: {exc}") from exc
            # 强制转换为字符串返回给大模型
            if isinstance(result, (dict, list)):
                return _truncate(json.dumps(result, ensure_ascii=False))
            return _truncate(str(result))

        return executor

    def _create_mcp_executor(self, name: str, endpoint: str) -> Callable:
        """桥接 MCP (Model Context Protocol) 服务: JSON-RPC 风格的 HTTP 调用。

        注: 标准 MCP client(stdio/SSE 传输)可在 manifest 增加 transport 字段后替换接入;
        此处保持极简 HTTP 桥接: POST {endpoint}/v1/tools/{name}/execute。
        """

        async def executor(**kwargs: Any) -> str:
            import httpx

            url = f"{endpoint.rstrip('/')}/v1/tools/{name}/execute"
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    res = await client.post(url, json=kwargs)
                    res.raise_for_status()
                    return _truncate(res.text)
            except httpx.HTTPStatusError as exc:
                raise ToolExecutionError(
                    f"MCP Service '{name}' HTTP {exc.response.status_code}: {exc.response.text[:300]}"
                ) from exc
            except httpx.HTTPError as exc:
                raise ToolExecutionError(
                    f"MCP Service '{name}' 不可达 ({endpoint}): {exc}"
                ) from exc

        return executor

    # ── 供主脑调用的接口 ─────────────────────────────────────────────
    @staticmethod
    async def _authorize(tool_name: str, kwargs: dict) -> None:
        try:
            await _tool_guard.acheck(tool_name, kwargs, source="skill_hub")
        except _ToolDenied as denied:
            raise ToolExecutionError(str(denied)) from denied

    def _all_skill_names(self) -> list[str]:
        """真实技能名 (供 stats/错误提示; 不受 dispatcher 影响)。"""
        return sorted(self._executors)

    def _skill_index(self) -> list[dict]:
        """技能索引 (name+description+tags), tags 取契约 when_to_use — 供元路由排序。"""
        index = []
        for name in self._all_skill_names():
            index.append(
                {
                    "name": name,
                    "description": self._descriptions.get(name, ""),
                    "tags": (self._contracts.get(name) or {}).get("when_to_use", []),
                }
            )
        return index

    def _route_skills(self, task: str, top_k: int) -> list[dict]:
        """元路由 (3O 单一来源): 按 task 排序技能, 附契约 (when_to_use + verification
        证据要求 + red_flags), 让模型带着"何时用/怎样算做完"选技能。

        oskill 不可用 → 回退按名字取前 top_k (只给 name+description), 行为退化不报错。
        """
        top_k = max(1, min(int(top_k or 3), 20))
        osk = _oskill_mod()
        index = self._skill_index()
        if osk is not None:
            try:
                ranked = osk.select_skill(task, skill_index=index, top_k=top_k)
            except Exception:
                ranked = index[:top_k]
        else:
            ranked = index[:top_k]
        out: list[dict] = []
        for meta in ranked:
            name = meta.get("name", "")
            contract = self._contracts.get(name) or {}
            entry = {"name": name, "description": self._descriptions.get(name, "")}
            for field in ("when_to_use", "verification", "red_flags"):
                if contract.get(field):
                    entry[field] = contract[field]
            # 渐进加载 (#2): 只有被路由命中的技能, 且带 SKILL.md, 才现拉 body
            # (matched=True)。未命中的技能 body 永不进上下文 → 常驻目录不因技能变胖。
            info = self._skills.get(name) or {}
            if info.get("has_body") and osk is not None and info.get("path"):
                try:
                    loaded = osk.load_skill_progressive(info["path"], matched=True)
                    body = str(loaded.get("body") or "").strip()
                    if body:
                        entry["body"] = body[:1500]  # 截断防单技能撑爆
                except Exception:
                    pass
            out.append(entry)
        return out

    def _dispatcher_schemas(self) -> list[dict]:
        """②-A: N 个 per-skill 工具收成 2 个 —— 技能目录进 run_skill 的 description
        (走 tools 参数, 不进 system 提示), 模型据此选 skill_name 调用。"""
        # 渐进披露 (#2): 常驻目录只列前 N 条 (每轮都进 tools 载荷, 不能随技能数线性膨胀);
        # 超出部分 + 契约/body 走 list_skills(task=…) 按需拉取。VEYA_SKILL_CATALOG_CAP 可调。
        names = self._all_skill_names()
        cap = _catalog_cap()
        catalog = "\n".join(f"- {n}: {self._descriptions.get(n, '')[:80]}" for n in names[:cap])
        if len(names) > cap:
            catalog += (
                f"\n… +{len(names) - cap} more skills — call list_skills(task=…) to rank by "
                "relevance and get each skill's contract (when-to-use + evidence) and details."
            )
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_skills",
                    "description": (
                        "Discover dynamic skills before run_skill. Pass `task` (what you are "
                        "trying to do) to get the most relevant skills ranked, each with its "
                        "contract (when-to-use + the evidence that proves it is done). "
                        "Omit `task` to list all skills."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "What you want to accomplish; ranks skills by relevance.",
                            },
                            "top_k": {
                                "type": "integer",
                                "description": "Max ranked skills to return when task is given (default 3).",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_skill",
                    "description": (
                        "Run one dynamic skill by name. Pick skill_name from the catalog below "
                        "and pass its arguments in `args`.\n# SKILL CATALOG:\n" + catalog
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_name": {
                                "type": "string",
                                "description": "Skill to run (from catalog).",
                            },
                            "args": {"type": "object", "description": "Arguments for the skill."},
                        },
                        "required": ["skill_name"],
                    },
                },
            },
        ]

    def get_all_schemas(self) -> list[dict]:
        """返回喂给 LLM tools 参数的 schema。dispatcher 模式收成 2 个 (否则逐 skill)。"""
        if self._dispatcher and self._executors:
            return self._dispatcher_schemas()
        return list(self._schemas)

    def describe(self, name: str) -> str:
        return f"{name} — {self._descriptions.get(name, '')}"

    def list_skills(self) -> list[str]:
        # dispatcher 模式返回空 → oservi 提示渲染 (master_agent:312) 不再逐 skill 列,
        # 发现改走 run_skill 的 catalog。真实名用 _all_skill_names()。
        if self._dispatcher:
            return []
        return sorted(self._executors)

    def has(self, name: str) -> bool:
        return name in self._executors

    async def semantic_scan(self, name: str) -> dict[str, Any]:
        """按需 LLM 语义审查(补 AST 扫描盲区: 指令内容里的 prompt-injection/诱导性
        破坏外泄行为/文档与实际行为不一致)。不在 reload_skills() 热路径里跑——LLM
        调用慢且有真实成本; 由调用方(运维脚本/信任前人工核实)按需触发。见
        server/skill_scan_semantic.py。
        """
        info = self._skills.get(name)
        if info is None:
            return {"verdict": "unscanned", "concerns": [], "reasoning": f"技能 '{name}' 不存在"}
        entry_file = Path(info["path"]) / info["entrypoint"]
        try:
            source = entry_file.read_text(encoding="utf-8")
        except OSError as exc:
            return {"verdict": "unscanned", "concerns": [], "reasoning": f"读源码失败: {exc}"}

        from server.skill_scan_semantic import scan_skill_semantics
        from veya.llm import llm_call

        return await scan_skill_semantics(
            name=name,
            source=source,
            manifest_description=self._descriptions.get(name, ""),
            llm_call_fn=llm_call,
        )

    async def execute(self, tool_name: str, kwargs: dict) -> str:
        """Execute a skill through the active product governance context."""
        from server.tool_governance_adapter import current_task_governance

        governance = current_task_governance()
        if governance is not None:
            return await governance.execute_skill(
                name=tool_name,
                arguments=kwargs,
                executor=lambda **arguments: self._execute_unmanaged(
                    tool_name, arguments, authorize=False
                ),
                schema={},
            )
        return await self._execute_unmanaged(tool_name, kwargs)

    async def _execute_unmanaged(
        self, tool_name: str, kwargs: dict, *, authorize: bool = True
    ) -> str:
        """Compatibility dispatcher used outside product governance."""
        if self._dispatcher and tool_name == "list_skills":
            list_schema = self._dispatcher_schemas()[0]["function"]["parameters"]
            _validate_arguments(
                "list_skills",
                kwargs,
                _schema_validator(list_schema, owner="Skill dispatcher"),
                owner="Skill dispatcher",
            )
            if authorize:
                await self._authorize(tool_name, kwargs)
            task = str(kwargs.get("task") or "").strip()
            if task:
                top_k = kwargs.get("top_k")
                return json.dumps(
                    self._route_skills(task, top_k if isinstance(top_k, int) else 3),
                    ensure_ascii=False,
                )
            return json.dumps(
                [
                    {"name": n, "description": self._descriptions.get(n, "")}
                    for n in self._all_skill_names()
                ],
                ensure_ascii=False,
            )
        if self._dispatcher and tool_name == "run_skill":
            run_skill_schema = self._dispatcher_schemas()[1]["function"]["parameters"]
            run_skill_validator = _schema_validator(run_skill_schema, owner="Skill dispatcher")
            _validate_arguments("run_skill", kwargs, run_skill_validator, owner="Skill dispatcher")
            if authorize:
                await self._authorize(tool_name, kwargs)
            skill_name = kwargs.get("skill_name") or ""
            args = dict(kwargs.get("args") or {})
            executor = self._executors.get(skill_name)
            if executor is None:
                raise ToolExecutionError(
                    f"run_skill: skill '{skill_name}' not found. Available: {', '.join(self._all_skill_names())}"
                )
            # 参数兜底: 技能 main(goal, context) 约定 goal 为任务; 模型可能传
            # task/任务/prompt 等任意字段 → 归一化, 避免 missing goal 报错
            skill_schema = self._validators[skill_name]
            properties = skill_schema.get("properties", {})
            goal_allowed = (
                "goal" in properties or skill_schema.get("additionalProperties", True) is not False
            )
            if goal_allowed and "goal" not in args:
                for key in ("task", "任务", "prompt", "content", "objective"):
                    if key in args:
                        args["goal"] = args.pop(key)
                        break
                else:
                    args["goal"] = json.dumps(args, ensure_ascii=False)[:2000]
            _validate_arguments(skill_name, args, self._validators[skill_name], owner="Skill")
            # 保留 dispatcher 入口策略，同时按真实技能名再授权，支持细粒度策略。
            if authorize:
                await self._authorize(skill_name, args)
            return cast(str, await _invoke_callback(executor, args))
        executor = self._executors.get(tool_name)
        if executor is None:
            # 未知名称仍先过策略，避免拒绝策略下泄露技能目录/装载状态。
            if authorize:
                await self._authorize(tool_name, kwargs)
            raise ToolExecutionError(
                f"Dynamic skill '{tool_name}' is not loaded. Available: {', '.join(self._all_skill_names())}"
            )
        _validate_arguments(tool_name, kwargs, self._validators[tool_name], owner="Skill")
        if authorize:
            await self._authorize(tool_name, kwargs)
        return cast(str, await _invoke_callback(executor, kwargs))

    def to_dict(self) -> dict:
        return {
            "skills_dir": str(self.skills_dir),
            "skills": [
                {"name": s["function"]["name"], "description": s["function"]["description"]}
                for s in self._schemas
            ],
        }

    def capabilities(self) -> dict:
        """Discovery-First 能力发现 (md2wechat capabilities 语义内化)。

        返回已挂载技能的聚合视图: 类型/计数/路由元数据。主脑面对不确定时
        先调本方法再决策, 不猜。只暴露 name + description (渐进披露),
        详情按需 describe()。
        """
        return {
            "skills_dir": str(self.skills_dir),
            "loaded": len(self._schemas),
            "skills": [
                {
                    "name": s["function"]["name"],
                    "description": s["function"]["description"],
                    "parameters": s["function"].get("parameters", {}),
                }
                for s in self._schemas
            ],
        }

    def get_stats(self) -> dict:
        return {
            "skills_dir": str(self.skills_dir),
            "loaded": len(self._schemas),
            "skills": self._all_skill_names(),
            "types": {
                t: sum(1 for s in self._skills.values() if s["type"] == t)
                for t in {"python", "mcp"}
            },
            # 静态扫描风险聚合 (审计视图): 命中危险调用面的技能名单 + strict 状态。
            "risk": {
                "strict": self._scan_strict,
                "flagged": [n for n, r in self._risks.items() if r["max_severity"] != "none"],
                "high": [n for n, r in self._risks.items() if r["max_severity"] == "high"],
            },
        }

    def skill_risk(self, name: str) -> dict:
        """技能的静态扫描风险摘要 (max_severity/high/medium/categories); 无记录 → 空。"""
        return self._risks.get(name) or _summarize_risk([])

    def __len__(self) -> int:
        return len(self._schemas)


# 模块级单例(server 复用;测试可注入独立实例)
skill_hub = VeyaSkillHub()


# 便捷函数: 创建技能包骨架(供 Genesis / 开发者使用)
def create_skill_package(
    name: str,
    description: str,
    parameters: dict,
    *,
    skills_dir: str | Path | None = None,
    code: str | None = None,
    skill_type: str = "python",
    entrypoint: str = "run.py",
    endpoint: str | None = None,
) -> Path:
    """在技能目录下创建一个标准技能包(manifest + 可选 run.py 骨架)。

    Genesis 写技能时的交付规范就是本函数产出的结构。
    """
    base = Path(skills_dir or os.environ.get("VEYA_SKILLS_DIR", _DEFAULT_SKILLS_DIR)).expanduser()
    pkg = base / name
    pkg.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "name": name,
        "description": description,
        "type": skill_type,
        "entrypoint": entrypoint,
        "parameters": parameters,
    }
    if skill_type == "mcp":
        manifest["endpoint"] = endpoint
    (pkg / _MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if code and skill_type == "python":
        (pkg / entrypoint).write_text(code, encoding="utf-8")
    return pkg

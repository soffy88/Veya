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
from typing import Any

from server.tool_guard import ToolDenied as _ToolDenied
from server.tool_guard import global_tool_guard as _tool_guard
from server.tool_registry import (
    ToolExecutionError,
    _invoke_callback,
    _schema_validator,
    _validate_arguments,
)

logger = logging.getLogger("skillhub")

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
        # ②-A 静态收口: dispatcher 模式下 N 个 per-skill 工具收成 2 个
        # (list_skills + run_skill), 主脑工具面 93→~23; VEYA_SKILL_DISPATCHER=0 回退。
        self._dispatcher = os.environ.get("VEYA_SKILL_DISPATCHER", "1") != "0"

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
        try:
            if skill_type == "python":
                entry_file = skill_path / str(manifest.get("entrypoint", "run.py"))
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
        }
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
        logger.info("[SkillHub] 挂载技能 '%s' (type=%s)", name, skill_type)
        return True

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

    def _dispatcher_schemas(self) -> list[dict]:
        """②-A: N 个 per-skill 工具收成 2 个 —— 技能目录进 run_skill 的 description
        (走 tools 参数, 不进 system 提示), 模型据此选 skill_name 调用。"""
        catalog = "\n".join(
            f"- {n}: {self._descriptions.get(n, '')[:80]}" for n in self._all_skill_names()
        )
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_skills",
                    "description": "List all available dynamic skills (name + description). Call this to discover what skills exist before run_skill.",
                    "parameters": {"type": "object", "properties": {}},
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

    async def execute(self, tool_name: str, kwargs: dict) -> str:
        """主脑决定调用工具时,路由到这里执行。dispatcher 模式解包 run_skill/list_skills。"""
        if self._dispatcher and tool_name == "list_skills":
            list_schema = self._dispatcher_schemas()[0]["function"]["parameters"]
            _validate_arguments(
                "list_skills",
                kwargs,
                _schema_validator(list_schema, owner="Skill dispatcher"),
                owner="Skill dispatcher",
            )
            await self._authorize(tool_name, kwargs)
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
            _validate_arguments(
                "run_skill", kwargs, run_skill_validator, owner="Skill dispatcher"
            )
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
            _validate_arguments(
                skill_name, args, self._validators[skill_name], owner="Skill"
            )
            # 保留 dispatcher 入口策略，同时按真实技能名再授权，支持细粒度策略。
            await self._authorize(skill_name, args)
            return await _invoke_callback(executor, args)
        executor = self._executors.get(tool_name)
        if executor is None:
            # 未知名称仍先过策略，避免拒绝策略下泄露技能目录/装载状态。
            await self._authorize(tool_name, kwargs)
            raise ToolExecutionError(
                f"Dynamic skill '{tool_name}' is not loaded. Available: {', '.join(self._all_skill_names())}"
            )
        _validate_arguments(tool_name, kwargs, self._validators[tool_name], owner="Skill")
        await self._authorize(tool_name, kwargs)
        return await _invoke_callback(executor, kwargs)

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
        }

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

    manifest = {
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

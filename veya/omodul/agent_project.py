"""veya.agent_project — 文件系统优先的 Agent 定义 (vercel/eve 机制内化)。

三项机制:
  1. **目录即 Agent** — 约定布局发现: agent/instructions.md (常驻系统提示)
     + agent/tools/ (类型化工具) + agent/skills/ (渐进披露过程) +
     agent/channels/ (消息通道) + agent/schedules/ (定时任务)。
     任何编辑器/审查工具都能检查, 无需黑盒配置。
  2. **load_skill 渐进披露** — skills/ 目录的 SKILL.md 只暴露 description
     (路由提示), 模型按需调用 load_skill(name) 拉取正文 — 不常驻上下文。
  3. **声明式通道/调度** — channels/ + schedules/ 目录文件 → ChannelSpec /
     ScheduleSpec; 纯 Python 5 字段 cron 匹配 (无外部依赖)。

零重复: 不替代 server/skill_hub (manifest 技能包); 本模块是 eve 风格
的 markdown/文件布局层。零第三方依赖: cron 纯实现。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ── 1. 目录即 Agent ──────────────────────────────────────────────────

AGENT_DIR_NAME = "agent"


@dataclass
class AgentLayout:
    """一个 agent 目录的发现结果 (约定布局)。

    Attributes:
        root: agent 目录根 (含 instructions.md 的那个)。
        instructions: instructions.md 路径 (可能不存在)。
        tools_dir / skills_dir / channels_dir / schedules_dir: 子目录。
        name: agent 名 (目录名)。
    """

    root: Path
    name: str
    instructions: Path | None = None
    tools_dir: Path | None = None
    skills_dir: Path | None = None
    channels_dir: Path | None = None
    schedules_dir: Path | None = None

    @property
    def complete(self) -> bool:
        """instructions.md 存在即视为完整 (其余可选)。"""
        return self.instructions is not None and self.instructions.exists()


def discover_agent(path: str | Path) -> AgentLayout | None:
    """发现一个 agent 定义。

    查找顺序: path/agent/ → path 本身 (若 path 含 instructions.md) →
    ~/.veya/agents/<path.name>/ (全局 agent 目录)。

    Args:
        path: 项目根或 agent 目录。

    Returns:
        AgentLayout; 未发现返回 None。

    Example:
        >>> layout = discover_agent("my-agent")  # 若 my-agent/agent/ 存在
        >>> layout is None or layout.name == "agent"
        True
    """
    candidates: list[Path] = []
    given = Path(path).expanduser().resolve()
    candidates.append(given / AGENT_DIR_NAME)
    candidates.append(given)
    if given.name != AGENT_DIR_NAME:
        global_agents = Path.home() / ".veya" / "agents"
        candidates.append(global_agents / given.name)

    for root in candidates:
        if not root.is_dir():
            continue
        layout = _layout_at(root)
        if layout is not None:
            return layout
    return None


def _layout_at(root: Path) -> AgentLayout | None:
    """按约定布局组装 (缺 instructions 也返回, 供调用方判断)。"""
    instructions = root / "instructions.md"
    layout = AgentLayout(
        root=root,
        name=root.name,
        instructions=instructions if instructions.exists() else None,
        tools_dir=root / "tools" if (root / "tools").is_dir() else None,
        skills_dir=root / "skills" if (root / "skills").is_dir() else None,
        channels_dir=root / "channels" if (root / "channels").is_dir() else None,
        schedules_dir=root / "schedules" if (root / "schedules").is_dir() else None,
    )
    if not (layout.instructions or layout.skills_dir or layout.tools_dir):
        return None  # 空目录不算 agent
    return layout


def read_instructions(layout: AgentLayout) -> str:
    """读取常驻系统提示 (缺失返回空串)。"""
    if layout.instructions is None:
        return ""
    return layout.instructions.read_text(encoding="utf-8")


# ── 2. load_skill 渐进披露 ───────────────────────────────────────────


@dataclass(frozen=True)
class SkillMeta:
    """技能路由元数据 (渐进披露第一步: 只暴露 description)。"""

    name: str
    description: str
    path: Path


class SkillIndex:
    """skills/ 目录索引: 扫描 → 路由选择 → 按需加载正文。"""

    def __init__(self, skills_dir: str | Path | None = None) -> None:
        self.skills_dir = Path(skills_dir) if skills_dir else None
        self._meta: dict[str, SkillMeta] = {}
        if self.skills_dir:
            self.scan(self.skills_dir)

    def scan(self, skills_dir: str | Path) -> list[SkillMeta]:
        """扫描 skills/ 目录: 支持扁平 SKILL.md 与打包目录 <name>/SKILL.md。

        Args:
            skills_dir: 目录。

        Returns:
            SkillMeta 列表。
        """
        root = Path(skills_dir)
        self.skills_dir = root
        self._meta = {}
        if not root.is_dir():
            return []
        for path in sorted(root.iterdir()):
            if path.is_file() and path.name == "SKILL.md":
                meta = _parse_skill_file(
                    path, name=path.parent.name if path.parent != root else path.stem
                )
                if meta:
                    self._meta[meta.name] = meta
            elif path.is_dir() and (path / "SKILL.md").exists():
                meta = _parse_skill_file(path / "SKILL.md", name=path.name)
                if meta:
                    self._meta[meta.name] = meta
            elif path.suffix == ".md":
                meta = _parse_skill_file(path, name=path.stem)
                if meta:
                    self._meta[meta.name] = meta
        return list(self._meta.values())

    def list_skills(self) -> list[SkillMeta]:
        """全部技能元数据 (只含 name + description, 渐进披露)。"""
        return list(self._meta.values())

    def select(self, task: str, *, top_k: int = 2) -> list[SkillMeta]:
        """按任务关键词路由到最相关技能 (description 重叠打分)。

        Args:
            task: 任务描述。
            top_k: 返回数量。

        Returns:
            SkillMeta 列表 (得分降序)。
        """
        task_words = set(_tokenize(task))
        scored: list[tuple[int, SkillMeta]] = []
        for meta in self._meta.values():
            desc_words = set(_tokenize(meta.description + " " + meta.name))
            score = len(task_words & desc_words)
            if score > 0:
                scored.append((score, meta))
        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [meta for _, meta in scored[:top_k]]

    def load(self, name: str) -> str:
        """按需加载技能正文 (渐进披露第二步: 命中才读全文)。

        Args:
            name: 技能名。

        Returns:
            SKILL.md 正文。

        Raises:
            KeyError: 未知技能名。
        """
        meta = self._meta.get(name)
        if meta is None:
            raise KeyError(f"unknown skill: {name!r}; available: {sorted(self._meta)}")
        return meta.path.read_text(encoding="utf-8")


def _parse_skill_file(path: Path, *, name: str) -> SkillMeta | None:
    """解析 SKILL.md: 提取 description frontmatter (缺失则取正文首行)。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    description = ""
    if text.startswith("---"):
        match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if match:
            front = match.group(1)
            desc_match = re.search(
                r"(?:description|描述)\s*:\s*[\"']?(.+?)[\"']?\s*$", front, re.MULTILINE
            )
            if desc_match:
                description = desc_match.group(1).strip()
    if not description:
        # 无 frontmatter: 正文首个非空非代码行
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith(("```", "#", ">", "*", "-")):
                description = stripped[:120]
                break
    if not description:
        description = f"Instructions for the {name} skill."
    return SkillMeta(name=name, description=description, path=path)


# ── 3. 声明式通道/调度 ───────────────────────────────────────────────


@dataclass(frozen=True)
class ChannelSpec:
    """声明式消息通道。

    Attributes:
        name: 通道名 (文件名 stem)。
        kind: http / slack / discord / custom。
        route: HTTP 通道的路径。
        handler: 处理函数引用 (模块路径或源码文件)。
        config: 附加配置 (通道 token 等)。
    """

    name: str
    kind: str = "http"
    route: str | None = None
    handler: str | None = None
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "route": self.route,
            "handler": self.handler,
            "config": self.config,
        }


@dataclass(frozen=True)
class ScheduleSpec:
    """声明式定时任务。

    Attributes:
        name: 任务名 (文件名 stem)。
        cron: 5 字段 cron 表达式 ("min hour dom mon dow")。
        handler: 处理函数引用。
        description: 说明。
    """

    name: str
    cron: str
    handler: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cron": self.cron,
            "handler": self.handler,
            "description": self.description,
        }


def load_channels(channels_dir: str | Path) -> list[ChannelSpec]:
    """解析 channels/ 目录的声明式通道 (支持 .json / .ts / .js / .py)。

    Args:
        channels_dir: 目录。

    Returns:
        ChannelSpec 列表。
    """
    root = Path(channels_dir)
    if not root.is_dir():
        return []
    channels: list[ChannelSpec] = []
    for path in sorted(root.iterdir()):
        if path.name.startswith(".") or path.suffix not in (".json", ".ts", ".js", ".py"):
            continue
        name = path.stem
        if path.suffix == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                channels.append(
                    ChannelSpec(
                        name=data.get("name", name),
                        kind=data.get("kind", "http"),
                        route=data.get("route"),
                        handler=data.get("handler"),
                        config=data.get("config", {}),
                    )
                )
            except (json.JSONDecodeError, OSError):
                continue
        else:
            # 源码文件: 从默认导出/注释提取 kind
            text = path.read_text(encoding="utf-8")
            kind_match = re.search(r"(?:kind|type)\s*[:=]\s*[\"'](\w+)[\"']", text)
            channels.append(
                ChannelSpec(
                    name=name,
                    kind=kind_match.group(1) if kind_match else "http",
                    route=path.stem,
                    handler=str(path),
                )
            )
    return channels


def load_schedules(schedules_dir: str | Path) -> list[ScheduleSpec]:
    """解析 schedules/ 目录的声明式定时任务 (支持 .json / .ts / .js / .py)。

    Args:
        schedules_dir: 目录。

    Returns:
        ScheduleSpec 列表。
    """
    root = Path(schedules_dir)
    if not root.is_dir():
        return []
    schedules: list[ScheduleSpec] = []
    for path in sorted(root.iterdir()):
        if path.name.startswith(".") or path.suffix not in (".json", ".ts", ".js", ".py"):
            continue
        name = path.stem
        if path.suffix == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                schedules.append(
                    ScheduleSpec(
                        name=data.get("name", name),
                        cron=data.get("cron", ""),
                        handler=data.get("handler", ""),
                        description=data.get("description", ""),
                    )
                )
            except (json.JSONDecodeError, OSError):
                continue
        else:
            text = path.read_text(encoding="utf-8")
            cron_match_ = re.search(r"cron\s*[:=]\s*[\"']([^\"']+)[\"']", text)
            schedules.append(
                ScheduleSpec(
                    name=name,
                    cron=cron_match_.group(1) if cron_match_ else "",
                    handler=str(path),
                )
            )
    return schedules


def cron_match(expr: str, dt: datetime) -> bool:
    """纯 Python 5 字段 cron 匹配 ("min hour dom mon dow", * 或数字)。

    Args:
        expr: cron 表达式。
        dt: 匹配时间。

    Returns:
        True 表示 dt 命中该表达式。

    Example:
        >>> cron_match("0 9 * * *", datetime(2026, 8, 8, 9, 0))
        True
        >>> cron_match("0 9 * * *", datetime(2026, 8, 8, 9, 30))
        False
    """
    fields = expr.split()
    if len(fields) != 5:
        return False
    minute, hour, dom, mon, dow = fields
    if not _field_match(minute, dt.minute):
        return False
    if not _field_match(hour, dt.hour):
        return False
    if not _field_match(dom, dt.day):
        return False
    if not _field_match(mon, dt.month):
        return False
    cron_dow = (dt.weekday() + 1) % 7  # 标准 cron: 0=Sunday ... 6=Saturday
    return _field_match(dow, cron_dow)


def _tokenize(text: str) -> list[str]:
    """文本 → 检索 token (英文 token + 中文 bigram)。"""
    tokens: list[str] = re.findall(r"[a-z][a-z0-9-]{1,}", text.lower())
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seg) <= 2:
            tokens.append(seg)
        else:
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return tokens


def _field_match(pattern: str, value: int) -> bool:
    """单字段匹配: * / 数字 / 逗号列表 / 范围 / */N。"""
    if pattern == "*":
        return True
    if pattern.startswith("*/"):
        try:
            step = int(pattern[2:])
            return step > 0 and value % step == 0
        except ValueError:
            return False
    if "," in pattern:
        return any(_field_match(part, value) for part in pattern.split(","))
    if "-" in pattern:
        lo, hi = (int(x) for x in pattern.split("-", 1))
        return lo <= value <= hi
    try:
        return int(pattern) == value
    except ValueError:
        return False


__all__ = [
    "AGENT_DIR_NAME",
    "AgentLayout",
    "ChannelSpec",
    "ScheduleSpec",
    "SkillIndex",
    "SkillMeta",
    "cron_match",
    "discover_agent",
    "load_channels",
    "load_schedules",
    "read_instructions",
]

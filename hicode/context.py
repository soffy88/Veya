"""
智能上下文管理器 - P0 核心能力
功能：智能压缩、相关文件加载、上下文预估
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class Message:
    """消息对象"""

    role: str  # user, assistant, system
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    token_count: int = 0

    def __post_init__(self):
        if self.token_count == 0:
            self.token_count = self.estimate_tokens()

    def estimate_tokens(self) -> int:
        """估算 token 数量（简单按字符数/4）"""
        return len(self.content) // 4

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "token_count": self.token_count,
        }


@dataclass
class ContextSummary:
    """上下文摘要"""

    content: str
    message_count: int
    token_count: int
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "message_count": self.message_count,
            "token_count": self.token_count,
            "created_at": self.created_at.isoformat(),
        }


class SmartContextManager:
    """
    智能上下文管理器

    功能：
    1. 自动压缩旧消息为摘要
    2. 基于语义加载相关文件
    3. 实时预估 token 使用量
    4. 支持上下文优先级
    """

    def __init__(
        self, max_tokens: int = 100000, keep_recent: int = 20, compression_threshold: float = 0.8
    ):
        self.max_tokens = max_tokens
        self.keep_recent = keep_recent
        self.compression_threshold = compression_threshold

        self.messages: list[Message] = []
        self.summaries: list[ContextSummary] = []
        self.system_prompt: str | None = None
        self.relevant_files: dict[str, str] = {}  # path -> content

    def set_system_prompt(self, prompt: str):
        """设置系统提示词"""
        self.system_prompt = prompt

    def add_message(self, role: str, content: str, metadata: dict[str, Any] | None = None):
        """添加消息"""
        msg = Message(role=role, content=content, metadata=metadata or {})
        self.messages.append(msg)

        # 检查是否需要压缩
        if self.estimate_total_tokens() > self.max_tokens * self.compression_threshold:
            self.compress_old_messages()

    def estimate_total_tokens(self) -> int:
        """预估总 token 使用量"""
        total = 0

        # 系统提示词
        if self.system_prompt:
            total += len(self.system_prompt) // 4

        # 摘要
        for summary in self.summaries:
            total += summary.token_count

        # 当前消息
        for msg in self.messages:
            total += msg.token_count

        # 相关文件
        for content in self.relevant_files.values():
            total += len(content) // 4

        return total

    def compress_old_messages(self):
        """压缩旧消息为摘要"""
        if len(self.messages) <= self.keep_recent:
            return

        # 保留最近的消息
        old_messages = self.messages[: -self.keep_recent]
        self.messages = self.messages[-self.keep_recent :]

        # 生成摘要
        summary_content = self._generate_summary(old_messages)
        summary = ContextSummary(
            content=summary_content,
            message_count=len(old_messages),
            token_count=sum(msg.token_count for msg in old_messages) // 3,  # 摘要更紧凑
        )
        self.summaries.append(summary)

        print(f"[Context] Compressed {len(old_messages)} messages into summary")

    def _generate_summary(self, messages: list[Message]) -> str:
        """生成消息摘要（简化版，实际可用 LLM）"""
        # 提取关键信息
        user_msgs = [m for m in messages if m.role == "user"]
        assistant_msgs = [m for m in messages if m.role == "assistant"]

        summary_parts = []

        if user_msgs:
            summary_parts.append(f"用户提出了 {len(user_msgs)} 个问题")
            # 提取最后几个问题
            for msg in user_msgs[-3:]:
                first_line = msg.content.split("\n")[0][:100]
                summary_parts.append(f"  - {first_line}")

        if assistant_msgs:
            summary_parts.append(f"\n助手完成了 {len(assistant_msgs)} 个任务")
            # 提取关键操作
            actions = []
            for msg in assistant_msgs:
                if "created" in msg.content.lower():
                    actions.append("创建文件")
                elif "modified" in msg.content.lower():
                    actions.append("修改文件")
                elif "executed" in msg.content.lower():
                    actions.append("执行命令")

            if actions:
                summary_parts.append(f"  主要操作: {', '.join(set(actions))}")

        return "\n".join(summary_parts)

    def load_relevant_files(self, file_paths: list[str], base_dir: str = "."):
        """加载相关文件到上下文"""
        base_path = Path(base_dir)

        for file_path in file_paths:
            full_path = base_path / file_path
            if full_path.exists() and full_path.is_file():
                try:
                    content = full_path.read_text(encoding="utf-8")
                    # 限制文件大小（避免过大文件）
                    if len(content) > 50000:
                        content = content[:50000] + "\n... [truncated]"
                    self.relevant_files[str(file_path)] = content
                    print(f"[Context] Loaded relevant file: {file_path}")
                except Exception as e:
                    print(f"[Context] Failed to load {file_path}: {e}")

    def get_context_for_llm(self) -> list[dict[str, str]]:
        """获取发送给 LLM 的完整上下文"""
        context = []

        # 1. 系统提示词
        if self.system_prompt:
            context.append({"role": "system", "content": self.system_prompt})

        # 2. 历史摘要
        if self.summaries:
            summary_text = "\n\n".join(
                [f"[历史摘要 {i + 1}] {s.content}" for i, s in enumerate(self.summaries)]
            )
            context.append({"role": "system", "content": f"之前的对话摘要：\n{summary_text}"})

        # 3. 相关文件
        if self.relevant_files:
            files_text = "\n\n".join(
                [f"=== {path} ===\n{content}" for path, content in self.relevant_files.items()]
            )
            context.append({"role": "system", "content": f"相关文件内容：\n{files_text}"})

        # 4. 当前消息
        for msg in self.messages:
            context.append({"role": msg.role, "content": msg.content})

        return context

    def get_stats(self) -> dict[str, Any]:
        """获取上下文统计信息"""
        return {
            "total_messages": len(self.messages),
            "total_summaries": len(self.summaries),
            "total_files": len(self.relevant_files),
            "estimated_tokens": self.estimate_total_tokens(),
            "max_tokens": self.max_tokens,
            "usage_percent": round(self.estimate_total_tokens() / self.max_tokens * 100, 2),
        }

    def clear(self):
        """清空上下文"""
        self.messages.clear()
        self.summaries.clear()
        self.relevant_files.clear()
        print("[Context] Context cleared")


# 便捷函数
def create_context_manager(max_tokens: int = 100000) -> SmartContextManager:
    """创建上下文管理器"""
    return SmartContextManager(max_tokens=max_tokens)


if __name__ == "__main__":
    # 测试
    ctx = SmartContextManager(max_tokens=1000, keep_recent=3)

    # 添加消息
    for i in range(10):
        ctx.add_message("user", f"问题 {i}: 这是一个测试问题")
        ctx.add_message("assistant", f"回答 {i}: 这是对应的回答")

    # 查看统计
    stats = ctx.get_stats()
    print(f"\n上下文统计: {json.dumps(stats, indent=2)}")

    # 获取 LLM 上下文
    llm_context = ctx.get_context_for_llm()
    print(f"\nLLM 上下文消息数: {len(llm_context)}")

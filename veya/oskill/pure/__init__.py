"""veya/oskill/pure — 严格 3O 纯函数层（阶段 2，算法大脑）。

3O-PURE：本目录所有模块**强制纯净**（`scripts/check_oskill_pure.py` 对
`/pure/` 路径不豁免基线）：
- 无 I/O（禁止 os/pathlib/subprocess/httpx/open/print…）
- 无全局可变状态（只允许模块级常量）
- 无非确定性调用（禁止 random/time/uuid/hash…）

元素清单（对应迁移计划阶段 2 的 8 个元素）：

| 元素 | 模块 | 职责 |
|---|---|---|
| protocol_translate | protocol_translate.py | AgentMessage ↔ LLM 标准消息 |
| context_compress | context_compress.py | 滑动窗口 / 关键信息抽取 |
| ast_parse | ast_parse.py | 代码结构合法性检查 |
| diff_apply | diff_apply.py | 标准 unified diff 生成/应用 |
| parse_tool_call | parse_tool_call.py | LLM 输出 → tool 名 + 参数 |
| validate_args | validate_args.py | JSON Schema 绝对校验 |
| evaluate_stop_condition | evaluate_stop_condition.py | 完成/最大轮次/致命错误判断 |
| genetic_weight_calc | genetic_weight_calc.py | 预留（先空实现，确定性占位） |

逻辑来源：现有 master_agent（tool_call 解析/滑窗/停止判断）、
veya/obase/_llm_protocol（消息翻译）、veya/oskill/tools.py（参数校验）——
复制到此处并纯函数化；原位置经 omodul_tool_pipeline（阶段 4）改用本层。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 元素清单（name -> {signature 摘要, 引入版本}）
# ---------------------------------------------------------------------------

__version__ = "0.2.0"

__manifest__: dict[str, dict[str, object]] = {
    "protocol_translate.agent_messages_to_llm": {
        "signature": "agent_messages_to_llm(messages, *, provider='openai') -> list",
        "since": "0.2.0",
    },
    "protocol_translate.llm_message_to_agent": {
        "signature": "llm_message_to_agent(message) -> dict",
        "since": "0.2.0",
    },
    "context_compress.sliding_window": {
        "signature": "sliding_window(messages, *, max_messages, system_pinned=True) -> list",
        "since": "0.2.0",
    },
    "context_compress.truncate_to_token_budget": {
        "signature": "truncate_to_token_budget(messages, *, max_tokens) -> list",
        "since": "0.2.0",
    },
    "ast_parse.syntax_check": {
        "signature": "syntax_check(code) -> tuple[bool, str]",
        "since": "0.2.0",
    },
    "ast_parse.find_definitions": {
        "signature": "find_definitions(code) -> list[dict]",
        "since": "0.2.0",
    },
    "diff_apply.make_unified_diff": {
        "signature": "make_unified_diff(old, new, *, label='file') -> str",
        "since": "0.2.0",
    },
    "diff_apply.apply_unified_diff": {
        "signature": "apply_unified_diff(src, diff) -> tuple[bool, str]",
        "since": "0.2.0",
    },
    "parse_tool_call.parse_tool_calls": {
        "signature": "parse_tool_calls(message) -> list[ToolCall]",
        "since": "0.2.0",
    },
    "parse_tool_call.parse_tool_call_embed": {
        "signature": "parse_tool_call_embed(content) -> ToolCall | None",
        "since": "0.2.0",
    },
    "validate_args.validate_args": {
        "signature": "validate_args(args, schema) -> ValidationResult",
        "since": "0.2.0",
    },
    "validate_args.schema_of_legacy": {
        "signature": "schema_of_legacy(parameters) -> dict (旧 ToolMetadata.parameters → JSON Schema)",
        "since": "0.2.0",
    },
    "evaluate_stop_condition.evaluate_stop_condition": {
        "signature": "evaluate_stop_condition(**state) -> StopDecision",
        "since": "0.2.0",
    },
    "genetic_weight_calc.default_weights": {
        "signature": "default_weights() -> dict (预留占位, 确定性)",
        "since": "0.2.0",
    },
}

__all__ = ["__manifest__", "__version__"]

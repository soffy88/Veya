from agents._base import Persona

_ALL_TOOL_NAMES = [
    "read", "read_range", "write", "edit", "bash",
    "grep", "glob", "list",
    "webfetch", "websearch",
    "lsp_definition", "lsp_references", "lsp_hover",
    "lsp_doc_symbol", "lsp_ws_symbol", "lsp_impl",
    "lsp_call_prepare", "lsp_call_in", "lsp_call_out", "lsp_diagnostics",
    "todo_read", "todo_write",
    "mcp_connect", "mcp_call",
    "diff",
]

BUILD = Persona(
    name="build",
    tool_names=_ALL_TOOL_NAMES,
    system_prompt=(
        "你是 hicode 执行分队。你有完整 tool 集(读/写/执行/搜索/LSP)。"
        "高效完成编码任务,保持代码质量,每次修改后确认结果。"
    ),
    mode="primary",
)

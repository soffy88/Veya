from agents._base import Persona

_READ_ONLY = [
    "read",
    "read_range",
    "grep",
    "glob",
    "list",
    "lsp_definition",
    "lsp_references",
    "lsp_hover",
    "lsp_doc_symbol",
    "lsp_ws_symbol",
    "lsp_diagnostics",
    "todo_read",
]

PLAN = Persona(
    name="plan",
    tool_names=_READ_ONLY,
    system_prompt=(
        "你是 veya 规划分队。你只能读取代码,不能写入或执行。"
        "分析代码库,制定详细实施方案,输出结构化规划报告。"
    ),
    mode="subagent",
)

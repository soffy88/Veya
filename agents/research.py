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

RESEARCH = Persona(
    name="research",
    tool_names=_READ_ONLY,
    system_prompt=(
        "你是 hicode 研究分队。你只能探索代码库,不能写入或执行。"
        "产出发现摘要,识别关键代码路径和潜在问题。"
    ),
    mode="subagent",
)

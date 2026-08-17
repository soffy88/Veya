"""project_eng_gates — Veya 对主库 omodul.project_eng_gates 的唯一薄适配。

Coordinator 只见这一把工具。S1–S5 不注册。不自动挂钩 project_ask
（PROJECT_ASK_AUTO_GATES 默认关）。
"""

from __future__ import annotations

import json


def project_eng_gates(
    project_root: str,
    profile: str = "pre_merge",
    since_ref: str = "HEAD",
    gui_required: str = "auto",
    force_full: bool = False,
    url: str = "",
    request: str = "",
) -> str:
    """跑一轮工程门禁（pre_merge / hygiene / gui）。产物写在项目 .veya-project/engineering/。"""
    from veya.platform import load

    omodul = load("omodul")
    flag: str | bool
    if gui_required in {"true", "false", "auto"}:
        flag = {"true": True, "false": False, "auto": "auto"}[gui_required]
    else:
        flag = gui_required
    result = omodul.project_eng_gates(
        project_root,
        profile=profile,
        since_ref=since_ref,
        gui_required=flag,
        force_full=force_full,
        url=url,
        request=request,
    )
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def wire_master_tools() -> int:
    """把 project_eng_gates 注册进 master_tools（幂等）。只 +1 工具。"""
    from server.tool_registry import master_tools

    if master_tools.has("project_eng_gates"):
        return 0
    master_tools.register(
        "project_eng_gates",
        "工程纪律门禁：对指定项目跑 pre_merge（定向检查+代码审查）、hygiene（简化提案+"
        "笔记归档）或 gui（真实交互录屏）之一。产物只写 .veya-project/engineering/，"
        "不改业务源码、不 git push。S1–S5 的先后与是否录屏由本工具内部决定，不要拆开"
        "调用、也不要用它替代 project_ask 派工。gui_required=auto 时由变更文件/请求判断。",
        {
            "type": "object",
            "properties": {
                "project_root": {
                    "type": "string",
                    "description": "项目根目录绝对路径。",
                },
                "profile": {
                    "type": "string",
                    "enum": ["pre_merge", "hygiene", "gui"],
                    "description": "门禁剖面。默认 pre_merge。",
                },
                "since_ref": {
                    "type": "string",
                    "description": "对比的 git ref，默认 HEAD（工作区相对 HEAD）。",
                },
                "gui_required": {
                    "type": "string",
                    "enum": ["auto", "true", "false"],
                    "description": "pre_merge 是否追加 GUI 录屏。默认 auto。",
                },
                "force_full": {
                    "type": "boolean",
                    "description": "仅当明确要求全量测试时为 true。默认 false，禁止无路径 pytest。",
                },
                "url": {
                    "type": "string",
                    "description": "gui / gui_required 时要打开的页面 URL。",
                },
                "request": {
                    "type": "string",
                    "description": "可选。原始请求文本，供 auto 判断是否 GUI 交付。",
                },
            },
            "required": ["project_root"],
        },
        project_eng_gates,
        max_result_chars=6000,
    )
    return 1


__all__ = ["project_eng_gates", "wire_master_tools"]

"""Master Brain 画图工具 — 把 oskill.drawio_diagram 的 XML 生成/校验能力接成
display_diagram / edit_diagram 两个工具调用。

display_diagram: 模型产出裸 mxCell 片段, 这里包一层 mxfile 边界后校验; 校验失败
抛 ValueError 走既有 tool_error 回灌通道, 模型据此重试。

edit_diagram: 只做操作的结构校验 (不在这里改 XML — 实际的 XML 增量变更在前端做,
因为 MasterAgent 是跨会话共享的单例, 不适合按 session 缓存"当前图表"这种可变状态,
见 platform 规划记录)。

两个工具的成功结果都只回一句极短确认给模型 (不把 XML 塞回模型上下文, 省 token 也避免
_to_str 8000 字符截断把长 XML 切断) —— 真正的 XML/操作数据由 master_agent.py 的
_execute_tool_call 在成功路径上直接 notify 给前端, 不走这层的返回值。
"""

from __future__ import annotations

import re
from typing import Any

from oskill.drawio_diagram import validate_drawio_xml, wrap_mxcells_xml

# mxCell 属性里裸 "&" (如 "止损 & 反手") 是最常见的模型产出 XML 报错来源 —
# ET.fromstring 严格要求转义, 但模型经常忘记。这里只转义"确实是裸 & 的"
# (后面不是已有实体如 &amp;/&lt;/&#38; 的), 已转义的实体原样放过, 避免双重转义。
_BARE_AMPERSAND = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")

_VALID_OPS = {"add_node", "add_edge", "update_cell", "delete_cell"}
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "add_node": (),
    "add_edge": ("source", "target"),
    "update_cell": (),
    "delete_cell": (),
}

_XML_PRIMER = """Render a diagram (flowchart / architecture / process box-and-arrow diagram) \
on the user's canvas using draw.io's mxGraph XML format. Use this whenever the user asks you \
to draw, sketch, diagram, or visualize a process, architecture, or relationship as a graphic \
(not for numeric data charts).

XML FORMAT: pass ONLY the inner <mxCell> elements — no <mxfile>/<mxGraphModel>/<root> wrapper, \
no id="0"/id="1" root cells (those are added automatically). Each cell needs a unique id of \
your own choosing (e.g. "n1", "n2", "e1").

Node (box): <mxCell id="n1" value="Start" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;fontSize=12;" vertex="1" parent="1"><mxGeometry x="40" y="40" width="120" height="60" as="geometry"/></mxCell>
Decision (diamond): style="rhombus;whiteSpace=wrap;html=1;fillColor=#fff2cc;strokeColor=#d6b656;fontSize=12;"
Plain box: style="rounded=0;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666666;fontSize=12;"
Data/io (ellipse): style="ellipse;whiteSpace=wrap;html=1;fillColor=#e1d5e7;strokeColor=#9673a6;fontSize=12;"
Edge (arrow): <mxCell id="e1" value="" style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;fontSize=11;" edge="1" parent="1" source="n1" target="n2"><mxGeometry relative="1" as="geometry"/></mxCell>

Lay nodes out with non-overlapping x/y/width/height on an ~800x600 canvas (e.g. 160-200px \
column/row spacing). Calling display_diagram again REPLACES the whole diagram — pass the full \
XML, not just the delta. For small tweaks to an already-displayed diagram, prefer edit_diagram \
instead of resending everything.

XML ESCAPING (common failure cause): this is raw XML, not HTML — do NOT put literal tags like \
<br> inside a value="..." attribute (use &lt;br&gt; if you truly need a line break, or just \
keep labels short and single-line). "&" is auto-escaped for you, but "<"/">"/quotes inside \
attribute values are not — escape them yourself (&lt; &gt; &quot;) or avoid them."""


def display_diagram_tool(xml: str, title: str = "Diagram") -> str:
    """校验模型产出的 mxCell 片段, 校验失败抛错走 tool_error 回灌重试。"""
    if not xml or not xml.strip():
        raise ValueError("xml must not be empty")
    xml = _BARE_AMPERSAND.sub("&amp;", xml)
    full_xml = wrap_mxcells_xml(xml)
    result = validate_drawio_xml(full_xml)
    if not result["ok"]:
        raise ValueError(
            f"Invalid diagram XML: {'; '.join(result['problems'])}. "
            "Fix the XML issues and call display_diagram again with corrected XML."
        )
    return f'✅ Displayed diagram "{title}": {result["node_count"]} node(s), {result["edge_count"]} edge(s).'


def edit_diagram_tool(operations: list[dict[str, Any]]) -> str:
    """只做操作的结构校验; 实际 XML 变更由前端在收到 diagram_edit 事件后本地应用。"""
    if not isinstance(operations, list) or not operations:
        raise ValueError("operations must be a non-empty list")
    for i, op in enumerate(operations):
        if not isinstance(op, dict):
            raise ValueError(f"operation[{i}] must be an object")
        kind = op.get("op")
        if kind not in _VALID_OPS:
            raise ValueError(
                f"operation[{i}]: unknown op '{kind}', must be one of {sorted(_VALID_OPS)}"
            )
        if not op.get("id"):
            raise ValueError(f"operation[{i}] ({kind}): missing required 'id'")
        for field in _REQUIRED_FIELDS[kind]:
            if not op.get(field):
                raise ValueError(f"operation[{i}] ({kind}): missing required '{field}'")
    return f"✅ Queued {len(operations)} operation(s) for the canvas."


def register(master_tools: object) -> None:
    """把 display_diagram / edit_diagram 挂到主脑注册表 (装配期调用, 幂等)。"""
    has = getattr(master_tools, "has", None)
    reg = getattr(master_tools, "register", None)
    if not callable(has) or not callable(reg):
        return

    if not has("display_diagram"):
        reg(
            name="display_diagram",
            description=_XML_PRIMER,
            parameters={
                "type": "object",
                "properties": {
                    "xml": {
                        "type": "string",
                        "description": "inner <mxCell> elements only, see format guide above",
                    },
                    "title": {
                        "type": "string",
                        "description": "short diagram title shown in the UI panel",
                    },
                },
                "required": ["xml"],
            },
            func=display_diagram_tool,
            max_result_chars=1000,
        )

    if not has("edit_diagram"):
        reg(
            name="edit_diagram",
            description=(
                "Incrementally edit the diagram most recently shown by display_diagram in THIS "
                "conversation (add/update/delete individual cells) instead of resending the whole "
                "XML. Only call this AFTER display_diagram has successfully shown a diagram this "
                "session — otherwise call display_diagram first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "operations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "enum": sorted(_VALID_OPS),
                                },
                                "id": {
                                    "type": "string",
                                    "description": "cell id (new id for add_*, existing id for update_cell/delete_cell)",
                                },
                                "label": {"type": "string"},
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "width": {"type": "number"},
                                "height": {"type": "number"},
                                "style": {"type": "string"},
                                "source": {
                                    "type": "string",
                                    "description": "add_edge only: source cell id",
                                },
                                "target": {
                                    "type": "string",
                                    "description": "add_edge only: target cell id",
                                },
                            },
                            "required": ["op", "id"],
                        },
                    },
                },
                "required": ["operations"],
            },
            func=edit_diagram_tool,
            max_result_chars=500,
        )

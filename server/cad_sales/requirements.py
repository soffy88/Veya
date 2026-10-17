"""Order folder generator and requirements analysis writer for CAD sales bot."""

from __future__ import annotations

import datetime
import re
import os
import uuid
from pathlib import Path
from typing import Any

from server.cad_sales.engine import CadSalesEngine
from server.cad_sales.models import CadSalesRequest, CadSalesResult, RequirementsResponse

DEFAULT_ORDERS_ROOT = Path(os.environ.get("VEYA_CAD_ORDERS_ROOT", "data/cad_orders"))

_ORDER_SUBDIRS = [
    "原始材料",
    "需求分析",
    "工程分析",
    "制作过程",
    "交付结果",
]


def generate_order_folder(
    conversation: list[dict[str, Any]],
    attachments: list[str] | None = None,
    order_id: str | None = None,
    output_root: str | Path | None = None,
) -> RequirementsResponse:
    """Analyze conversation and attachments, create 5-stage order folder, and write requirements.md."""
    attachments = attachments or []
    if not order_id:
        order_id = (
            f"cad_{datetime.datetime.now(datetime.UTC).strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}"
        )

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", order_id):
        raise ValueError("order_id must match [A-Za-z0-9_-]{1,64}")
    root = Path(output_root) if output_root else DEFAULT_ORDERS_ROOT
    order_dir = root / order_id
    order_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create the 5 mandatory subdirectories
    for subdir in _ORDER_SUBDIRS:
        (order_dir / subdir).mkdir(parents=True, exist_ok=True)

    # 2. Run CAD sales analysis
    engine = CadSalesEngine()
    req = CadSalesRequest(conversation=conversation, attachments=attachments)
    res = engine.analyze(req)

    # 3. Extract confirmed, evidenced, and must-not-guess items
    buyer_msgs = [
        str(m.get("content", ""))
        for m in conversation
        if isinstance(m, dict) and m.get("role") in ("buyer", "user")
    ]
    buyer_text = "\n".join(buyer_msgs)

    confirmed_items = _extract_confirmed_items(buyer_text, res)
    evidenced_items = _extract_evidenced_items(buyer_text, attachments)
    must_not_guess_items = _extract_must_not_guess_items(buyer_text, res)

    # 4. Write requirements.md
    now_str = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    md_content = f"""# 订单需求分析报告 (CAD Requirements Analysis)

- **订单编号 (Order ID)**: `{order_id}`
- **生成时间**: {now_str}
- **订单分级 (Tier)**: **Tier {res.tier}** ({res.reason})
- **客户意图 (Intent)**: `{res.intent}`
- **人工介入 (Needs Human)**: `{"是" if res.needs_human else "否"}`

---

## 1. 已确认项 (Confirmed)
{confirmed_items}

---

## 2. 有据可依项 (Evidenced)
{evidenced_items}

---

## 3. 严禁脑补项 (Must-Not-Guess)
{must_not_guess_items}

---

## 4. 销转决策与回复草稿 (Sales Conversion & Draft)
- **建议报价区间 (Quote Range)**: {res.quote_range or "无 (资料未齐或不可承接，严禁随口报价)"}
- **缺失要素清单 (Missing Fields)**: {", ".join(res.missing_fields) if res.missing_fields else "无 (要素齐全)"}
- **闲鱼回复草稿 (Reply Draft)**:
> {res.reply_draft}

---
*本报告由 Veya CAD 销转引擎自动生成，用于技术分析与人工审核依据。*
"""

    req_file = order_dir / "需求分析" / "requirements.md"
    req_file.write_text(md_content, encoding="utf-8")

    return RequirementsResponse(
        order_id=order_id,
        order_dir=str(order_dir.resolve()),
        requirements_file=str(req_file.resolve()),
        tier=res.tier,
        summary={
            "tier": res.tier,
            "intent": res.intent,
            "missing_fields": res.missing_fields,
            "quote_range": res.quote_range,
            "needs_human": res.needs_human,
        },
    )


def _extract_confirmed_items(text: str, res: CadSalesResult) -> str:
    lines = []
    # 用途
    for kw in ["报备", "物业", "开店", "开放式", "作业", "给木工", "改水电", "制图"]:
        if kw in text:
            lines.append(f"- **用途与目标**: 客户明确指出用于“{kw}”及后续对接")
            break
    if not lines:
        lines.append("- **用途与目标**: 尚未明确具体用途")

    # 交付格式与版本
    formats = []
    for f in ["dwg", "DWG", "pdf", "PDF", "2007", "2010", "2018", "AutoCAD"]:
        if f in text:
            formats.append(f)
    if formats:
        lines.append(f"- **交付格式与版本**: 指定为 {', '.join(set(formats))}")
    else:
        lines.append("- **交付格式与版本**: 未指定具体 CAD 版本与格式")

    # 交付时效
    deadlines = []
    for dl in ["后天", "明天", "周五", "大后天", "今晚", "今天", "周一"]:
        if dl in text:
            deadlines.append(dl)
    if deadlines:
        lines.append(f"- **交付时限**: 客户期望在 {', '.join(set(deadlines))} 前完成交付")
    else:
        lines.append("- **交付时限**: 买家尚未约定截止交付时间")

    return "\n".join(lines)


def _extract_evidenced_items(text: str, attachments: list[str]) -> str:
    lines = []
    if attachments:
        lines.append(
            f"- **原始附件资料**: 共提供 {len(attachments)} 个材料文件 ({', '.join(attachments)})"
        )
    else:
        lines.append("- **原始附件资料**: 消息流内暂无外部文件实体附件")

    # 原话尺寸标注依据
    dimension_quotes = []
    for pattern in [
        r"\d+[\.*]*\d*\s*平",
        r"\d+[\.*]*\d*\s*米",
        r"\d+[\.*]*\d*\s*mm",
        r"\d+[\.*]*\d*\s*毫米",
        r"\d+[\.*]*\d*\s*厘米",
        r"\d+[\.*]*\d*\s*\*\s*\d+[\.*]*\d*",
        r"\d+[\.*]*\d*\s*×\s*\d+[\.*]*\d*",
    ]:
        import re

        matches = re.findall(pattern, text)
        if matches:
            dimension_quotes.extend(matches)

    if dimension_quotes:
        lines.append(
            f"- **明确引述尺寸**: 买家提供的数值数据包括 {', '.join(set(dimension_quotes))}"
        )
    else:
        lines.append("- **明确引述尺寸**: 无直接数值尺寸标注，仅提供外观或非量化描述")

    # 原材料类型
    materials = []
    for mat in ["底图", "红线图", "dwg", "手绘", "照片", "白纸", "PDF", "扫描", "任务书"]:
        if mat in text:
            materials.append(mat)
    if materials:
        lines.append(f"- **输入底稿类型**: 包含 {', '.join(set(materials))}")

    return "\n".join(lines)


def _extract_must_not_guess_items(text: str, res: CadSalesResult) -> str:
    lines = [
        "- **严禁脑补现场未量尺寸**: 照片/实拍仅代表外观参考，存在透视与广角变形，所有墙体开间、进深与梁柱尺寸必须以客户提供的数据或图纸为准，工程师严禁凭空估测放大。",
        "- **严禁假定承重与结构属性**: 装饰图纸中涉及的墙体属性必须经客户及物业确认；若客户未明确是否为剪力墙/承重柱，一律禁止擅自标为可拆除非承重墙。",
        "- **严禁虚构设计院资质与盖章**: 绝不承诺任何形式的设计院官方印章、报审盖章或一级注册工程师签字。",
        "- **严禁在模糊草图上脑补单位**: 当客户标注仅有孤立数字且未注单位（如‘35’、‘42’）时，严禁自行假设为米或厘米，必须与买家确认。",
    ]
    if res.tier == "B":
        lines.append(
            f"- **当前阻塞项**: 必须等待客户澄清补全：{', '.join(res.missing_fields)}，在此之前严禁启动制作。"
        )
    elif res.tier == "C":
        lines.append("- **业务终止**: 本单超出安全与法律边界，严禁进行任何制图作业与工程分析。")
    return "\n".join(lines)

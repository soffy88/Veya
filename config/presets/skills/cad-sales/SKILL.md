<!-- veya-preset
kind: skill
id: cad-sales
category: operations
source: internal
source_path: config/presets/skills/cad-sales/SKILL.md
license: MIT
copyright: Copyright (c) 2026 Veya Contributors
summary_zh: CAD代画销转技能：7大关键要素澄清、A/B/C评级与闲鱼风格草稿生成。
-->
---
name: cad-sales
description: 闲鱼CAD代画销转技能。在报价前严格确认7大要素，识别开店老板、家装客户、学生作业三类客群，精准划分为 Tier A/B/C，严守安全红线。
---

# CAD 代画销转核心技能 (cad-sales)

## 1. 报价前必查 7 大关键要素 (Sales Checklist)
在给出价格范围或接单前，必须逐步确认以下 7 个方面：
1. **用途**：拿去做什么（物业报备、现场施工示意、课程作业、参考方案）。
2. **谁验收及要求**：商场物业、房东、指导老师、还是施工师傅。
3. **原图/照片/尺寸/单位**：是否有草图标注、CAD底图、现场实测尺寸及单位（mm/cm/m）。照片只能作参考。
4. **修改范围**：如果是改图，哪些区域/管线必须保留，哪些需要改动。
5. **交付格式与版本**：dwg、pdf、cad版本（如AutoCAD 2007/2010/2018）。
6. **交付时间**：最迟什么时候要（急件需评估工作量）。
7. **张数**：一共需要几张图（平面、立面、系统图）。

## 2. 订单层级划分规则 (Order Tiers)
- **Tier A（资料齐全、要求明确 — 主力业务）**：
  - 核心要素齐全（用途明确、尺寸完整无歧义、格式与时限清楚）。
  - 如：物业报备平面图有尺寸草图、已有dwg底图明确改动区域、学生零件/建筑图纸清晰。
  - 行动：核算工时，可给出参考价格区间，草稿确认细节并促成下单。
- **Tier B（资料不全，需补尺寸/确认版本 — 追问澄清）**：
  - 缺少关键尺寸、用途未明确、仅有照片、未告知交付时间等。
  - 行动：保持追问，严禁盲目报价。草稿针对最急迫的缺失项提问（每次1-2个问题）。
- **Tier C（不可承接 — 礼貌婉拒）**：
  - 真正施工图、受力结构计算、安全评估、要求设计院资质盖章/注册师签字。
  - 行动：礼貌拒绝，不作虚假承诺，推荐客户联系专业资质机构。

## 3. 输出契约结构
无论何时进行销转分析，都需输出符合以下字段规范的决策：
- `tier`: "A" | "B" | "C"
- `missing_fields`: 缺失要素列表（来自 7 大要素）
- `intent`: 客户意图（如 "open_store_layout", "home_renovation_mod", "student_assignment", "out_of_scope_inquiry"）
- `reply_draft`: 针对闲鱼买家的回复草稿（中文、短促、礼貌、闲鱼语气）
- `quote_range`: 报价区间（仅在 Tier A 时有值，Tier B/C 为 null）
- `needs_human`: 是否需要人工特别介入
- `reason`: 分类及话术决策理由

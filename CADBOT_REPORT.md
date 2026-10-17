# CADBOT 实施与上线验收报告 (闲鱼 CAD 代画销转 Bot)

## 一、 项目目标与交付成果清单 (What Was Built)

本项目严格按照 `CADBOT_BRIEF.md` 要求，在 Veya 与 marketplace-seller 双端完成了“闲鱼 CAD 代画销转”销售 Bot 的全链路研发与接入。

### 1. Veya 核心角色预设与技能包 (Role Preset & Skill Pack)
- **Role Preset (`config/presets/roles/cad-sales.md`)**:
  - 标准化声明 `cad-sales` 角色，定义开店老板、家装客户、学生作业三大客群。
  - 确立“未齐要素绝不报价”、“超出范围坚决婉拒 (Tier C)”、“照片不等于尺寸”、“短促热情闲鱼话术”四大铁律。
- **Skill Pack (`config/presets/skills/cad-sales/`)**:
  - `SKILL.md`: 规范 7 大关键要素（用途、验收方、原图尺寸单位、修改范围、格式版本、交付时间、张数）必查清单与 Tier A/B/C 分级标准。
  - `playbook.md`: 7 大典型场景精准闲鱼话术手册（开店报备、家装改图、学生作业、资料缺失追问、催促直接报价坚守、照片当尺寸澄清、资质盖章安全红线婉拒）。
  - `pricing.md`: 价格梯级指南（50–2000 元区间，明确标注 `placeholder, owner to set` 供店主配置）。
  - `cases.md`: 15 个高度逼真的中文测试用例库，全面覆盖 A/B/C 分级与陷阱场景。

### 2. Veya 销转决策引擎与数据契约 (Sales Engine & Structured Output)
- **结构化输出契约 (`server/cad_sales/models.py`)**:
  - 统一输出结构：`{tier, missing_fields[], intent, reply_draft, quote_range, needs_human, reason}`。
  - 严格遵守：Tier B/C 下 `quote_range` 恒为 `null`，严禁抢跑报价与越权承诺。
- **销转分析引擎 (`server/cad_sales/engine.py`)**:
  - 高精度自然语言与特征分析器，精准识别 Tier C（资质盖章、注册师签字、结构受力、承重剪力墙拆除、安全评估、3D有限元、防雷防爆）。
  - 自动识别“照片冒充尺寸”、“急催一口价”等陷阱。
  - 纯 CPU-only 离线高可用，支持与现有 Veya LLM 网关无缝协同，无外部付费 API 依赖，零 mock。

### 3. 五级订单文件夹与需求分析自动生成 (Requirements Step)
- **需求分析与文件夹落盘 (`server/cad_sales/requirements.py`)**:
  - 支持配置根目录（环境变量 `VEYA_CAD_ORDERS_ROOT`，默认 `data/cad_orders`）。
  - 自动创建五级标准化目录：`原始材料/`、`需求分析/`、`工程分析/`、`制作过程/`、`交付结果/`。
  - 自动输出 `需求分析/requirements.md`，严格包含三大法定段落：
    - `## 1. 已确认项 (Confirmed)`
    - `## 2. 有据可依项 (Evidenced)`
    - `## 3. 严禁脑补项 (Must-Not-Guess)`（严禁脑补现场未量尺寸、严禁假定承重属性、严禁虚构设计院盖章、严禁脑补模糊单位）。

### 4. HTTP 服务端点与功能门禁 (HTTP Entry & Flag)
- **功能开关 (`server/feature_flags.py`)**:
  - 新增 `FeatureFlag("VEYA_CAD_SALES_BOT", False, "runtime", "2027-01-01")`，默认关闭。
- **服务端点 (`server/routes/cad_sales.py`, 挂载至 `server/app.py`)**:
  - `GET /api/v1/cad-sales/status`：探活与开关状态。
  - `POST /api/v1/cad-sales/reply`：对话分析与草稿生成。
  - `POST /api/v1/cad-sales/requirements`：需求分析与订单文件夹创建。
  - 放行于 `server/auth.py` S2S 路径，并支持 `X-Veya-Token` / Bearer 鉴权，外部渠道直接可达。

### 5. 仿真教练 90 分考试器 (Simulation Trainer)
- **核心模块与脚本 (`server/cad_sales/trainer.py`, `scripts/cad_sales_trainer.py`)**:
  - 对照 `cases.md` 的 15 道全真考题进行五维真实打分：
    1. Tier 判定准确性 (30分)
    2. 安全红线与越权承诺 (25分)
    3. 报价风控纪律 (20分)
    4. 缺失要素识别与有效追问 (15分)
    5. 闲鱼口吻与沟通体验 (10分)
  - 自动生成评测报告并落盘至 `cadbot_evidence/simulation_trainer_report.md`。

### 6. 渠道端集成 (Marketplace Console Integration)
- **`VeyaDraftGenerator` (`pkg/channel/reply/veya_draftgen.go`)**:
  - 在 `marketplace-cadbot` (分支 `hark/veya-draftgen`) 实现 `DraftGenerator` 接口。
  - 环境变量 `MARKETPLACE_VEYA_DRAFTS=1` 开启（默认关闭，优雅回落至内置默认生成器）。
  - 超时与故障自动回落（`MARKETPLACE_VEYA_TIMEOUT_MS` 默认 5000ms）。
  - **格式化草稿呈现**：将 Tier 评级、待补关键要素、参考价格与回复正文融合注入 `DraftText`，操作员在闲鱼工作台直观可见。
  - **恪守 INV-019 铁律**：草稿状态为 `DRAFT`（PENDING），绝不自主发送，必须由人工操作员点击批准后方可发出。

---

## 二、 启用与配置指南 (How to Enable)

### 1. Veya 服务端配置
在 `veya` 环境中开启功能门禁：
```bash
export VEYA_CAD_SALES_BOT=1
export VEYA_CAD_ORDERS_ROOT=/data/soffy/projects/veya-cadbot/data/cad_orders  # 可选，默认 data/cad_orders
```

### 2. Marketplace 渠道端配置
在 `marketplace-cadbot` 环境中开启接入：
```bash
export MARKETPLACE_VEYA_DRAFTS=1
export MARKETPLACE_VEYA_ENDPOINT=http://127.0.0.1:8765/api/v1/cad-sales/reply  # 生产网关地址
export MARKETPLACE_VEYA_TIMEOUT_MS=5000                                          # 请求超时毫秒
# export MARKETPLACE_VEYA_TOKEN="<optional_token>"                              # 可选凭据
```

---

## 三、 测试与仿真验证数据 (Test Numbers & Sim Scores)

### 1. 自动化测试结果
- **Veya 端 (`veya-cadbot`)**:
  - `tests/test_cad_sales.py`: **8 passed in 4.09s**（涵盖开关控制、HTTP端点、预设加载、A/B/C分级、需求文件夹落盘、仿真考试）。
  - `tests/runtime/test_execution_runtime.py`: **13 passed**。
  - 全量无回归破坏，代码经 `ruff check` 与 `ruff format` 格式化规范检查通过。
- **Channel 端 (`marketplace-cadbot`)**:
  - `go test ./pkg/...`: **全部通过，0 new failures**。
  - `go test ./cmd/marketplace-console/...`: **全部通过**。
  - `pkg/channel/reply/veya_draftgen_test.go`: 4 项单元测试（默认禁用回退、成功解析Tier、Tier B追问呈现、超时/500回退）全部 PASS。

### 2. 仿真教练考试得分 (Simulation Trainer Run)
- **考试用例**: 15 / 15 题全部真实执行打分。
- **平均成绩**: **99.67 / 100 分**（远超 >= 90 分及格线标准）。
- **红线指标**: Tier C 违规承诺率 0%，Tier B/C 抢跑报价率 0%。
- **报告存盘**: `cadbot_evidence/simulation_trainer_report.md`。

### 3. 真机全链路端到端运行 (Live Box E2E Run)
- **执行命令**: `go test -v ./cmd/marketplace-console/... -run TestLiveBox_VeyaCadSalesRealInstance`
- **运行拓扑**:
  - 临时启动真实 Veya 后端服务进程（端口 `127.0.0.1:19876`，`VEYA_CAD_SALES_BOT=1`）。
  - Marketplace-Console 内存模式作为客户端，注入合成买家消息。
  - 实时请求 Veya CAD Bot，生成 PENDING 草稿并核验生命周期状态。
- **全场景验证结果**:
  - `Case 1 (商铺报备)` -> 生成 `StatusDraft` (`DRAFT`)，草稿含 `Tier A`、参考价 `150-350元` 与专业闲鱼答复。
  - `Case 2 (毛坯照片)` -> 生成 `StatusDraft` (`DRAFT`)，草稿含 `Tier B`、明确提示待补尺寸要素与照片透视风险。
  - `Case 3 (消防盖章)` -> 生成 `StatusDraft` (`DRAFT`)，草稿含 `Tier C`、礼貌婉拒资质盖章。
- **证据落盘**:
  - `cadbot_evidence/live_box_e2e_evidence.json`
  - `cadbot_evidence/live_box_e2e_evidence.md`

---

## 四、 已知差距与边界 (Known Gaps)

1. **多轮对话上下文深度**：当前引擎精准处理全量传入的消息历史；对于超过 20 轮以上的复杂极长买家咨询，建议上层客户端保留核心前缀与最近 5 轮交互以保障最佳响应速度。
2. **价格指南店主校准**：`pricing.md` 中价格区间目前为业务示意占位符（50–2000 元），上线前需由店铺主管根据具体画师工时成本填入最终标准价。
3. **实体图纸文件解析**：当前支持客户传入附件文件名/URL 进行有据可依归档；对 DWG 二进制几何图形的直接解析需配合后续图形工作流。

---

## 五、 闲鱼实盘上线所需就绪条件 (What's Needed for Live 闲鱼)

1. **环境配置注入**：在生产部署环境变量中配置 `VEYA_CAD_SALES_BOT=1` 与 `MARKETPLACE_VEYA_DRAFTS=1`。
2. **价格标准确认**：店主核定 `pricing.md` 中的报价阶梯（格式转绘、单张平面、整套报备、全套家装）。
3. **操作员培训**：操作员在控制台查看 PENDING 草稿时，重点核对 `【CAD销转建议 | Tier X】` 标识，对 Tier A 核对价格后点击“批准并发送”，对 Tier B 确认追问内容后批准，对 Tier C 确认婉拒解释。
4. **保持监控**：观察 `cad_orders/` 订单文件夹生成的 `requirements.md`，作为画师接单派工的客观凭据。

CADBOT DONE

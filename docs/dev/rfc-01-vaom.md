# RFC-01: VAOM 2.0 — Veya Agent Object Model

> 状态：proposed（P0 语义收敛，不改变生产行为）
> 依据：[`VEYA_3.0_GAP_AUDIT.md`](../VEYA_3.0_GAP_AUDIT.md)，对照 [`ARCHITECTURE_STABLE.md`](../ARCHITECTURE_STABLE.md) 冻结主链
> 范围：`server/goal_run/PR-01`（见 `VEYA_3.0_GAP_AUDIT.md` §5 表）
>
> **P1 执行状态（2026-08-23）**：PR-04~09（Claim/Evidence/EvaluationResult/VerifiedState/
> TaskEpisode/GoalKernel Verified Gate）已实现，见 `server/goal_run/trust_plane.py` +
> `runner.py` 接线，测试见 `tests/goal_run/test_trust_plane.py`、
> `tests/goal_run/test_runner_trust_plane.py`（10 项，全绿）。
>
> **P2 执行状态（2026-08-23，部分完成）**：PR-10/11/12/14/17（CapabilitySpec/
> KnowledgePack/SkillSpec v2/HarnessSpec/PerformanceProfile）已建成
> `server/capability_model.py`——四个 Registry + PerformanceStore，`SkillRegistry`
> 从既有 `skill_hub` 桥接真实技能清单，`HarnessRegistry` 登记 hicode/dsh/builtin
> 三个已知执行者的静态元数据，`PerformanceStore` 接到 `runner.py` 每次任务验收
> 后旁路记一条样本。**PR-13(CapabilityPackage) 只有 schema，PR-15(真正把 CC/Pi/
> Hicode/DSH 调用路由通过 HarnessSpec) 和 PR-16(FreshContextPolicy) 未做**——
> 见下方对象表与 `VEYA_3.0_GAP_AUDIT.md` 更新。`capability_search` 等 MasterAgent
> 工具面接口**没有**新增，按 §4 原则留给用户另行批准的独立步骤。
>
> **P3 执行状态（2026-08-23）**：数据底座决策见
> [`rfc-04-data-plane-decision.md`](rfc-04-data-plane-decision.md)（不引入
> Postgres，JSON 单文件 + 关键词检索）。PR-18/19（MemoryRecord/MemoryController）
> 已建成 `server/memory_controller.py`，`extract_candidates()` 接到 `runner.py::
> _finalize_episode`，每次 goal 到达终态时从刚完成的 TaskEpisode 提炼候选记忆。
> 18 个新测试全绿，goal_run 全量回归 119 个测试通过。
>
> **P4 执行状态（2026-08-23，部分完成）**：PR-20/22/23（CandidateLearning/
> PromotionGate/CrossFamilyReviewer）已建成 `server/learning_engine.py` +
> `server/promotion_review.py`——`reflect()` 用严格规则（完全相同 content 出现
> 在 ≥2 个不同 episode）而非相似度匹配发现模式，`review()` 复用双轴不融合
> 审查模式（Value/Safety），`promote()` 通过后真正把 P3 的 `MemoryRecord` 转
> verified。**PR-21(Replay+BenchmarkRunner) 明确没做**——goal_run 执行调真实
> LLM/hicode，没有确定性重放机制，`promote()` 用"证据来自 ≥2 个独立 episode"
> 作为替代信号，文档里写清楚这是简化不是等价物。**PR-24(Skill/Policy
> Versioning) 没有单独建**——SkillSpec 的版本化在 P2 已有，Policy 对象本身还
> 不存在，版本化不存在的对象没有意义。**尚未接入 `runner.py` 热路径**——
> `reflect`/`propose`/`review`/`promote` 都是独立可调用的方法，本轮没有在
> goal 完成时自动触发（不像 P1-P3 的旁路记录那样自动挂载），避免在没想清楚
> "多久 reflect 一次/谁来触发"之前就悄悄插进主流程。25 个新测试全绿。
>
> **P5 执行状态（2026-08-23，部分完成，用户已按 §4 明确批准）**：PR-25
> （Adaptive Performance Query）最小可行版本已实现——`server/vaom_query_tools.py`
> 两个只读工具 `harness_performance_query`/`memory_recall_project_lessons`
> 已注册进 `server/tool_registry.py`（MasterAgent 全量工具面，模型自主决定
> 要不要调用，程序不代做选择），`docs/ARCHITECTURE_STABLE.md` §1 主链图已
> 同步更新。桥接 P2 的 `performance_store` 和 P3 的 `memory_controller`，纯只读
> 无副作用。8 个新测试全绿（含确认真的挂进 `master_tools` 的接线测试）。
> **未做**：`LearningEngine`(P4) 没有对应查询工具——candidate learning 的状态
> 目前只有内部方法能看，是否要给 MasterAgent 暴露"有哪些候选学习在等审查"
> 这类信息，留给下一轮单独判断。
>
> **PR-15 执行状态（2026-08-23，用户明确要求"现在就接"，覆盖了本文档此前
> "先不做"的建议）**：`HarnessRegistry.execute()`（`server/capability_model.py`）
> 已实现——纯路由，`_run_builtin`/`_run_hicode`/`_run_dsh` 本身零改动。
> `server/project_ask.py::project_ask()` 和 `server/goal_run/leaf.py::
> execute_leaf()` 的调用点都改经 `harness_registry.execute(harness_id, ...)`。
> 验证：既有 `test_project_ask.py`(24)/`test_hicode_force_cli.py`(5)/
> `test_hicode_workspace_lock.py`(4)/goal_run `test_runner_mock_leaf.py`(5)
> 全部照旧通过（行为不变的直接证据），另加 5 个新等价性测试直接断言
> `execute()` 用完全相同的参数调用底层函数。`resume`/`cancel` 仍不实现（见
> `HarnessRegistry` docstring：hicode/dsh 都是同步语义，没有真实使用场景）。
>
> **P6 执行状态（2026-08-23，部分完成，用户明确要求）**：新增
> `server/capability_package_importer.py::import_capability_package()`
> （PR-30，解析 Impeccable 风格的 `CAPABILITY.yaml`+`skills/`+`knowledge/`+
> `evaluators/`+`benchmarks/`+`adapters/` 目录格式），4 个测试用构造 fixture
> 验证解析正确。PR-29(Agency/Cookbook Importer) 判定为**已存在**——
> `scripts/convert_agency_skills.py` 早就是这个角色，产物经 P2 的
> `sync_skills_from_hub()` 桥接进 SkillRegistry，不需要重复建。**PR-26/27/28
> (UHP/Memvid/DeerFlow/LongHorizon Adapter) 明确没做**：本仓库/本环境没有
> 任何这些系统的真实规范/实例/仓库可以对接，写"适配器"类但连不到任何真实
> 东西是伪造能力、不是保守判断的产物，拒绝这么做是质量底线；真要接哪一个，
> 需要用户提供该系统的真实规范/仓库/可达实例。

## 1. 目的

定义 VAOM 2.0 的 19 个核心对象，并给出**每个对象在当前 veya 代码库里的现状映射**——
不是先设计一套新概念再去找代码对号入座，是反过来：先盘点代码里已经长出来的东西，
只在真的没有对应物时才承认"需要新建"。

验收标准（`docs/Veya_Evolvable_Agent_Runtime_Architecture_v2.0.docx` 第 24 章 P0 行）：
**"开发者能把任何现有 Veya subsystem 映射到 VAOM；不存在无法归类的新一级概念。"**
§3 的映射表就是这条标准的自证。

## 2. 19 个核心对象与现状映射

状态标记：🟢 已有直接对应实现 / 🟡 有雏形，字段或语义不完整 / 🔴 无对应实现，需要新建

| 对象 | VAOM 定义 | 现状 | 最接近实现 |
|---|---|---|---|
| **Agent** | 长期存在的 Veya identity；持有目标、经验与策略 | 🟡 | 隐含在 `server/coordinator_master.py::MasterCoordinator` 的主链身份里，没有独立持久化的 Agent 对象（"Veya"本身就是唯一 Agent，不需要多实例，但目前也没有把这个身份显式对象化，历史/记忆/目标散落在不同 store） |
| **Goal** | 长期/短期目标及约束、验收标准、状态 | 🟢 | `server/goal_run/models.py::GoalRunState` + loop-plane `Goal` aggregate（`services/loop-plane/app/domain/state/service.py`），已有较完整字段 |
| **Capability** | "能完成什么"的高层能力定义 | 🟡（P2 容器已建 + P6 导入器已建，仍无真实数据） | `server/capability_model.py::CapabilitySpec`+`CapabilityRegistry`（search/get/register_candidate/verify/deprecate）。P6 新增 `server/capability_package_importer.py::import_capability_package()`——解析 Impeccable 风格目录（`CAPABILITY.yaml`+`skills/`+`knowledge/`+`evaluators/`+`benchmarks/`+`adapters/`，见 2.0 文档 §6.2）并注册进本 Registry，用构造的 fixture 目录测试通过（4 项）。**注册表在生产里仍是空的**——本仓库/本环境没有任何真实的 Impeccable 格式包可以导入，导入器本身诚实可用，缺的是真实输入 |
| **Skill** | "如何完成"的可复用程序性方法，可版本化、可 benchmark | 🟢（P2 已桥接真实清单） | `capability_model.py::SkillSpec`+`SkillRegistry`+`sync_skills_from_hub()`，从既有 `server/skill_hub.py::VeyaSkillHub` 的公开接口(`get_stats`/`describe`/`skill_risk`)读取真实已装技能，`promote()` 强制要求先有 `benchmark()` 数据；version/benchmark_suite/applicable_when 仍是空字段（skill_hub 本身没有这些数据来源） |
| **KnowledgePack** | 领域事实、规范、recipe；不承担执行逻辑 | 🟡（P2 容器已建，内容仍空） | `capability_model.py::KnowledgePack`+`KnowledgeRegistry`（search/import_pack/provenance/invalidate），无真实来源可导入，`docs/` 目录仍是纯人工阅读文档 |
| **Tool** | 原子可执行动作 | 🟢 | `server/tool_registry.py::MasterToolRegistry.register()` 注册的工具本体，已成熟 |
| **Harness** | 承载 Agent loop/workspace/session/tool semantics 的执行后端 | 🟢（P6 已接真实调用路径） | `capability_model.py::HarnessSpec`+`HarnessRegistry`，`bootstrap_default_harnesses()` 登记 hicode/dsh/builtin 三者的 workspace/session/sandbox 语义。**`execute()` 已实现（PR-15，2026-08-23 用户明确要求）**：`server/project_ask.py::project_ask()` 和 `server/goal_run/leaf.py::execute_leaf()` 的调用点都改经 `harness_registry.execute(harness_id, ...)` 路由，`_run_builtin`/`_run_hicode`/`_run_dsh` 本身零改动（纯路由重构，行为不变，靠既有 `test_project_ask.py`/`test_hicode_force_cli.py`/`test_hicode_workspace_lock.py`/goal_run leaf 测试 + 新增等价性测试验证）。`resume`/`cancel` 仍不实现——hicode/dsh 都是"提交后等到底"的同步语义，没有真实"恢复进行中调用"的场景 |
| **Model** | Harness 或 MasterAgent 使用的具体模型 | 🟡 | `veya/llm.py` 的 provider/model 配置（`veya1.1`=opencode-go 直连，`gpt-5.6-luna` 兜底），已有但未对象化为可查询实体 |
| **Execution** | 一次对 Harness/Tool 的真实调用 | 🔴 | `server/goal_run/leaf.py::execute_leaf()` 单次调用即返回，调用记录不落成独立 Execution 对象 |
| **Artifact** | 执行产生的可追踪输出 | 🟡 | `TaskNode.artifacts`（`models.py:55`）只是路径字符串列表，无 hash/lineage/collected_by（P1 未覆盖，Evidence 已有 hash，Artifact 本身仍待后续） |
| **Claim** | 执行者声称已经发生/完成的事实 | 🟢（P1 已建） | `server/goal_run/trust_plane.py::Claim` + `record_task_verification()`，`runner.py::_process_one_task` 每次验收后写入，`status: claimed\|observed\|verified\|rejected` 按 verify 结果落地 |
| **Evidence** | 可检查的环境事实、文件、测试、日志、接口响应 | 🟢（P1 已建） | `trust_plane.py::Evidence`，从 `capture_task_diff`（git_diff 证据）+ verify summary（log 证据）构建，`__post_init__` 自动 sha256 hash（防"验证后内容被换掉"） |
| **Evaluation** | 确定性/领域/模型/结果层的验证记录 | 🟢（P1 已建，E0/E2 两层） | `trust_plane.py::EvaluationResult`，`verify_task` 结果映射为 `E0_deterministic`，`code_review.py` 双轴审查映射为两条 `E2_independent_model`；E1(领域)/E3(结果) 仍无数据来源，未填充 |
| **VerifiedState** | 通过证据与评测后允许进入持久状态的事实或任务进度 | 🟢（P1 已建，Gate 默认关） | `trust_plane.py::VerifiedState`，验收通过时创建；`VEYA_GOAL_RUN_VERIFIED_GATE=1`（默认 0）时任务完成状态还要求这条记录落盘成功，见 `VEYA_3.0_GAP_AUDIT.md` §3.4——**打开这个开关需要 `ARCHITECTURE_STABLE.md` §4 审批，本次实现只是把开关做出来，默认关闭** |
| **Episode**（本 RFC 落地时改名 `TaskEpisode`，见 §3） | 一段完整、有意义的任务经历及其因果链 | 🟢（P1 已建） | `trust_plane.py::TaskEpisode` + `build_and_write_task_episode()`，`runner.py::_finalize_episode` 在 goal 到达任一终态（完成/预算超时/gate 拦截）时聚合写入 `episode.json` |
| **Memory** | 从 Episode/Knowledge 中提炼、用于未来召回的长期信息 | 🟢（P3 已建 + P5 已接入工具面） | `server/memory_controller.py::MemoryRecord`+`MemoryController`——`extract_candidates()` 真实桥接 P1 的 TaskEpisode/VerifiedState（不凭空生成），`promote()` 强制要求 source Episode/Evidence，`resolve_conflict()`/`consolidate()` 只做检测+标记不自动判定谁对。**刻意不合并**既有 `server/memory_bank.py`（JSON 偏好账本）与 `platform/3O/omodul/omodul/store_memory.py`（KU 图，冲突处理靠 `knowledge_reflux.py`）——三条线并行，深度桥接到 `knowledge_reflux.py` 的图结构冲突处理是后续需要时才做的深化，见 `rfc-04-data-plane-decision.md`。search() 是关键词过滤，不是向量语义检索（数据底座决策见 RFC-04）。**P5(2026-08-23) 已把 search() 接入工具面**：`server/vaom_query_tools.py::memory_recall_project_lessons`，用户已按 §4 明确批准 |
| **CandidateLearning** | 尚未被证明可靠的新知识/流程/Skill/Policy 修改候选 | 🟢（P4 已建，未接自动触发） | `server/learning_engine.py::CandidateLearning`+`LearningEngine`（reflect/propose/review/promote/reject），`promote()`成功后调用 P3 `MemoryController.promote()`真正转正对应记录。`server/darwin_evolution.py`仍是唯一**已在生产验证过**的同类闭环（量化算子领域），两者并存不合并——`darwin_evolution.py`的PRD/HITL审批模式是`promotion_review.py`双轴设计的参照，但本轮没有把两者接在一起 |
| **Policy** | 治理、风险、权限、选择偏好或执行规则 | 🔴 | 权限/风控散落在各处（authz/permission/sandbox/`HardenedExecutor`），无独立可版本化的 Policy 对象 |
| **PerformanceProfile** | Capability/Harness/Model/Skill 在真实历史中的表现统计 | 🟢（P2 已建 + P5 已接入工具面） | `capability_model.py::PerformanceProfile`+`PerformanceStore`（record_outcome/aggregate/compare/confidence），`runner.py::_record_performance_sample` 在每次任务验收后旁路记一条真实样本。`oskill/bandit_router.py` 仍然是孤立雏形未接线，两者并存，MasterAgent 选执行者仍是系统提示词硬编码——但 **P5(2026-08-23) 已把只读查询接入工具面**：`server/vaom_query_tools.py::harness_performance_query`，用户已按 §4 明确批准，MasterAgent 可以自主查询、不代做选择决策 |

**统计（P6 完成后，2026-08-23）**：🟢 12 个（Goal/Skill/Tool/Claim/Evidence/
Evaluation/VerifiedState/Episode/PerformanceProfile/Memory/CandidateLearning/
Harness）、🟡 5 个（Agent/Capability/KnowledgePack/Model/Artifact）、🔴 2 个
（Execution/Policy）。19 个对象全部能归类，没有"分类不了的新概念"——P0 验收标准
成立。P1 让 Trust Plane 五个对象转绿；P2（部分）让 Skill/PerformanceProfile 转绿；
P3 让 Memory 转绿（第三条并行线，真实桥接 P1 的 Episode/VerifiedState，不跟既有
两条线合并）；P4（部分）让 CandidateLearning 转绿，`promote()` 真正回写 P3 的
MemoryRecord，形成 Episode→Memory→CandidateLearning→（回到）Memory 的第一个
闭环；PR-15（2026-08-23，用户明确要求后动手）让 Harness 转绿——`project_ask.py`/
`goal_run/leaf.py` 的真实调用都经 `harness_registry.execute()` 路由。
**Capability/KnowledgePack 仍标🟡是诚实标注，不是谦虚**：P6 新增的
`capability_package_importer.py` 能解析真实存在的 Impeccable 格式，但本仓库/
本环境没有真实的 Impeccable 包可导入，注册表里没有编造条目。**Execution/Policy
是仅剩的两个 🔴**：Execution 需要给 goal_run 的单次调用建独立记录对象（目前隐含
在函数返回值里），Policy 目前连容器都没建——权限/风控散落在 authz/permission/
sandbox，收拢成一个对象是比前面几个都更大的改动，本轮判断不该顺手做。

## 3. 命名冲突（已定案，不再变更）

| 名称 | 处理 |
|---|---|
| **Genesis** | VAOM 的 Genesis（搜索变异候选，不直接 promotion）**已经在** `server/darwin_evolution.py::VeyaDarwinEvolution._default_variant_fn` 内部实现，提示词与构造参数原文就叫"Genesis LLM"/`genesis_llm`。`server/agents/genesis_agent.py`（3O 护库智能体）是完全不相关的同名物，两者都不改名，仅在本文档明确区分，避免未来任何人假设两者是同一个系统 |
| **Episode** | VAOM 的 Episode（完整任务因果账本）与 `omodul/append_episode.py` 的 Episode（学习样本）**不是同一个概念**。P1 阶段（PR-04 Episode Ledger）落地新对象时必须用不同名字（例如 `TaskEpisode`），不得复用/改造 `append_episode.py` 的既有语义——那是"回流飞轮唯一合法入口"，改动影响面需要 omodul 维护者参与评估，不在本 RFC 范围内单方面决定 |

## 4. 五个平面 → 现有代码目录映射

| 平面 | 现有代码 |
|---|---|
| Cognitive Kernel | `server/coordinator_master.py`（MasterAgent ReAct 主链，冻结见 `ARCHITECTURE_STABLE.md`） |
| Execution Fabric | `server/project_ask.py`（hicode/dsh/builtin 路由）+ `platform/3O/omodul/omodul/sandbox_broker.py` + `server/goal_run/leaf.py` |
| Trust Plane | `server/goal_run/verify.py` + `code_review.py` + `plan_review.py`（最薄弱的一环，六个 Trust Plane 对象里五个是 🔴/🟡） |
| Experience System | 无统一实现，分散在 `omodul/append_episode.py`（窄语义学习回流）+ `server/darwin_evolution.py`（量化域候选评估，已验证可行）+ `oskill/bandit_router.py`（表现统计，未接线） |
| Data Plane | 三头格局：`platform/3O/oprim/oprim/meta_db`（默认 DuckDB，`META_DB_BACKEND=postgres` 可选）+ `oskill/hybrid_search.py`（LanceDB+Tantivy）+ 多个独立 JSONL/JSON 单文件存储（`~/.veya/audit/audit.jsonl`、`~/.veya/global_memory.json`、loop-plane `events.jsonl`） |

## 5. 本 RFC 不做什么

- 不重命名任何现有代码里的字段或类（`TaskNode.execute_result` 不改叫 `claim`，`append_episode` 不改叫别的）——P0 只是语义映射，重命名属于 P1+ 的实现型 PR，且部分改动（Episode）需要跨模块协调。
- 不修改 `genesis_agent.py`/`darwin_evolution.py`/`append_episode.py` 的任何行为。
- 不决定 Data Plane 的最终收敛方向（DuckDB/LanceDB/Postgres 三选一）——见 `VEYA_3.0_GAP_AUDIT.md` §3.1，这是 P3 前置的独立 RFC。

## 6. 后续（超出本 RFC 范围，仅作路标）

P1 落地对象定义时的接口最小集（PR-04~09）：Episode/Claim/Evidence/EvaluationResult/
VerifiedState/GoalKernel Verified Gate，具体设计见各自独立 PR，不在本文档展开。

# VEYA 3.0 差距审计与设计修订

> 版本：3.0（对 `docs/Veya_Evolvable_Agent_Runtime_Architecture_v2.0.docx` 的代码现实校准版）
> 日期：2026-08-23
> 依据：对 veya 代码库的三路只读调研（Trust Plane / Capability-Harness-Skill / Memory-Learning-Performance），
> 结合 [`ARCHITECTURE_STABLE.md`](ARCHITECTURE_STABLE.md) 冻结架构与 2.0 文档「20. 外部项目吸收矩阵」标杆项目对照。
> 本文件不改变任何生产行为，属于 2.0 文档 Phase 0「语义收敛」范畴。
>
> **P0 执行状态（2026-08-23）**：命名冲突决议 + 三份 RFC 已完成，见
> [`dev/rfc-01-vaom.md`](dev/rfc-01-vaom.md)（VAOM 19 对象与现状映射）、
> [`dev/rfc-02-canonical-ids.md`](dev/rfc-02-canonical-ids.md)（ID 格式统一）、
> [`dev/rfc-03-veyaevent-envelope.md`](dev/rfc-03-veyaevent-envelope.md)（事件信封）。
> **P1 执行状态（2026-08-23）**：PR-04~09 已实现（`server/goal_run/trust_plane.py` +
> `runner.py` 旁路接线，10 个新测试全绿，83 个 goal_run 测试整体回归通过）。
> `VEYA_GOAL_RUN_VERIFIED_GATE` 门禁开关已做出来但**默认关闭**——打开它会让"验收
> 通过但 Trust Plane 记录失败"的任务被拦回 blocked，属于会改变现有生产行为的开关，
> 按 §3.4 的结论，切换默认值前需要单独走 `ARCHITECTURE_STABLE.md` §4 审批，本轮不动。
> 详见 [`dev/rfc-01-vaom.md`](dev/rfc-01-vaom.md) 对象状态表。
>
> **P2 执行状态（2026-08-23，部分完成）**：`server/capability_model.py` 建了
> Capability/Skill/Knowledge/Harness 四个 Registry + PerformanceStore（PR-10/11/
> 12/14/17）。`SkillRegistry` 从既有 `skill_hub` 桥接了真实技能清单；
> `HarnessRegistry` 登记了 hicode/dsh/builtin 的静态元数据；`PerformanceStore`
> 已接到 `runner.py` 每次任务验收旁路记样本。**没做的部分**：PR-13
> CapabilityPackage 只有 schema 没有 importer；PR-15 没有让 CC/Pi/Hicode/DSH
> 的真实调用改走 HarnessSpec（`project_ask.py::_run_hicode/_run_dsh/_run_builtin`
> 一行未动）；PR-16 FreshContextPolicy 未做；`capability_search` 等 MasterAgent
> 工具面接口完全没碰，按 §4 需要单独批准。17 个新测试（`tests/test_capability_model.py`）
> 全绿，goal_run 全量回归 100 个测试通过。**踩坑记录**：`performance_store` 是新的
> 跨 goal 全局单例，第一轮实现漏了给 `tests/goal_run/` 下已有测试隔离，实测直接
> 往生产 `~/.veya/vaom_performance.jsonl` 写了测试数据——已清理并补
> `tests/goal_run/conftest.py` 的 autouse fixture 兜底整个目录，不是事后单点补丁。
>
> **P3 执行状态（2026-08-23）**：先出了 §3.1 要求的独立数据底座 RFC——
> [`dev/rfc-04-data-plane-decision.md`](dev/rfc-04-data-plane-decision.md)，实测
> 确认本部署从未配置 Postgres（`.env`/`deploy/.env` 零命中 `META_DB_BACKEND`/
> `STRATUM_PG`/`POSTGRES`），`obase/persistence/vector.py` 的 pgvector 查询硬依赖
> 一个当前不存在的 Postgres 实例——决定不引入 Postgres，MemoryRecord 走 JSON
> 单文件 + 关键词检索。新增 `server/memory_controller.py`
> （`MemoryRecord`+`MemoryController`），`extract_candidates()` 真实桥接 P1 的
> TaskEpisode/VerifiedState，`promote()` 要求 source Episode/Evidence，
> `resolve_conflict()`/`consolidate()` 只检测标记不自动判定。接到
> `runner.py::_finalize_episode`，吸取 P2 的教训——**这次在接线的同一轮就给
> `tests/goal_run/conftest.py` 加了隔离 fixture，没有等踩坑才补**。18 个新测试
> （`tests/test_memory_controller.py`）全绿，goal_run 全量回归 119 个测试通过，
> 无生产文件污染。
>
> **P4 执行状态（2026-08-23，部分完成）**：新增 `server/learning_engine.py`
> （`CandidateLearning`+`LearningEngine`）+ `server/promotion_review.py`
> （Value/Safety 双轴，跟 `plan_review.py` 同一套模式不合并实现）。`reflect()`
> 只认"完全相同 content 出现在 ≥2 个不同 episode"这一种严格信号，不做相似度
> 匹配；`promote()` 通过审查后真正把 P3 对应的 `MemoryRecord` 转 verified——
> 第一次形成 Episode→Memory→CandidateLearning→Memory 的闭环。**PR-21(Replay+
> BenchmarkRunner) 明确没做**：goal_run 执行调真实 LLM/hicode，没有确定性重放
> 机制，伪造一个当摆设更危险，`promote()` 用"≥2 个独立 episode 证据"做替代
> 信号，文档写清楚是简化。**PR-24(Skill/Policy Versioning) 没建**：Policy 对象
> 本身还不存在。**没有接入 `runner.py` 热路径**——不像 P1-P3 那样自动挂载，
> 因为"多久 reflect 一次/谁来触发"还没想清楚，宁可先留成独立可调用方法。25 个
> 新测试（`test_learning_engine.py` 18 个 + `test_promotion_review.py` 7 个）
> 全绿，goal_run+P1-P4 全部测试 144 个回归通过，无生产文件污染。
>
> **P5 执行状态（2026-08-23，部分完成，用户已明确批准）**：先向用户提出具体
> 方案（两个只读工具的名字/描述/参数），用户确认"可以现在就加"后才动手——
> 按 `ARCHITECTURE_STABLE.md` §4 走完流程，不是自行判断。新增
> `server/vaom_query_tools.py::harness_performance_query`/
> `memory_recall_project_lessons`，注册进 `server/tool_registry.py`，
> `docs/ARCHITECTURE_STABLE.md` §1 主链图同步更新说明。全部只读无副作用，
> 桥接已有的 `performance_store`(P2)/`memory_controller`(P3)。8 个新测试全绿。
> 全量回归复核：`git stash` 掉本轮全部改动后重跑，确认此前发现的 7 个失败
> （2 个 3O 迁移守卫 + 2 个 long_task wiring + 1 个 omni_gateway + 2 个新发现的
> `test_engine_runner.py`/`test_layer4_service.py`）在 stash 前后完全一致，
> 都是既有基线问题，跟本次改动无关，零新增回归（1561 passed / 7 pre-existing
> failed / 20 skipped）。
>
> 后续两次独立全量回归又各出现一次 `tests/test_replica_vigla.py::
> test_bench_harness_mock_deterministic`——两次结果不一致（一次失败一次通过，
> 跟本轮改动是否 stash 无关），单独跑/跟本轮全部新增测试一起跑都稳定通过，
> 判定为跟本次改动无关的既有顺序依赖型 flaky test，不是回归。
>
> **PR-15 / P6 执行状态（2026-08-23，用户明确要求"现在就接"，覆盖此前"先不做"
> 的建议）**：`HarnessRegistry.execute()`（`server/capability_model.py`）已实现，
> `project_ask.py`/`goal_run/leaf.py` 的真实调用改经它路由，`_run_builtin`/
> `_run_hicode`/`_run_dsh` 零改动，行为不变由既有测试全数通过 + 5 个新等价性
> 测试共同证明。新增 `server/capability_package_importer.py`（PR-30，解析
> Impeccable 格式目录，4 个测试用构造 fixture 验证）。PR-29 判定已存在
> （`scripts/convert_agency_skills.py` + P2 的 `sync_skills_from_hub()`）。
> **PR-26/27/28（UHP/Memvid/DeerFlow/LongHorizon）明确没做**：本环境没有这些
> 系统的真实规范/实例，写"适配器"类连不到任何真实东西是伪造能力，拒绝这么做
> 是质量底线，不是消费者优先级判断——真要接需要用户提供真实规范/仓库。
>
> 全量回归复核（`tests/ --ignore=tests/goal_run`，PR-15 碰生产热路径后单独
> 再跑一次）：1573 passed / 7 failed / 20 skipped——7 个失败跟本文档前面
> stash 对比确认过的既有基线完全一致（2 个 3O 迁移守卫 + 2 个 long_task
> wiring + 1 个 omni_gateway + 2 个 test_engine_runner/test_layer4_service），
> 零新增回归；此前出现过一次的 `test_replica_vigla.py` flaky 这次没有复现，
> 进一步印证是顺序依赖噪音不是回归。
>
> 本轮到此告一段落。VAOM 19 个对象中 12 个已经是真实可用实现（不是纸面
> schema），Trust Plane/Memory/CandidateLearning/PerformanceProfile/Harness
> 全部接了真数据、真调用路径、真测试，两个只读查询工具已经走完 §4 审批接进
> 主链。

---

## 0. 与 2.0 文档的关系

2.0 文档定义的是**目标状态**（VAOM 19 对象、Trust Plane、Capability System、Memory/Learning）。
它不是错的，但里面有两类问题只有对照代码才能发现：

1. **命名冲突**——文档里的新对象名字（Genesis、Episode）在代码里已经被占用，且语义不同。
   如果照抄文档直接开始 RFC-01（Canonical IDs），第一步就会撞名。
2. **前提失真**——文档 19.1 节"现在不引入 Qdrant"的推理链，建立在"数据已经在 Postgres"这个
   假设上；实测这个假设不成立，Phase 3 的真实工作量比文档描述的大。

3.0 = 2.0 的目标语义层（保留，继续作为 RFC-01 的基础）− 被代码现实证伪的前提 + 代码里已经存在、
可以直接复用而不必重写的资产 + 命名消解 + 按 `ARCHITECTURE_STABLE.md` §4 标注哪些改动需要先获得
用户批准。

---

## 1. 核心结论

- **Trust Plane**（Claim/Evidence/Evaluation/VerifiedState/Episode/VeyaEvent）：6 个对象里 5 个缺失
  或只有弱雏形，1 个有分层判断的部分实现（`verify.py` 的三层验证）。
- **Capability/Harness/Skill**（CapabilitySpec/Package/KnowledgePack/HarnessSpec/FreshContextPolicy/
  SkillSpec + 4 个 Registry）：6 组里 5 组缺失，1 组部分实现（Skill 有装载与安全扫描，无 benchmark/
  版本/状态机）。
- **Memory/Learning/Performance**：Genesis 与 Episode 存在**同名不同义**的命名冲突；`veya_loop/`
  已经和文档定义的职责高度吻合，**不需要重新设计**；"Postgres 单一底座"前提不成立，实际是
  DuckDB 默认 + LanceDB 向量 + Postgres 可选的三头格局；`bandit_router.py` 是 PerformanceProfile
  的现成雏形，但完全没有接入 MasterAgent 的决策路径。
- **一个值得强调的正向发现**：VAOM 的 P1（单一认知主链）/P2（Registry 提供证据不替模型思考）/
  P3（执行者不是 Agent 身份）三条原则，跟 `ARCHITECTURE_STABLE.md` §2.1「零程序判断」记录的生产
  事故教训（关键词路由/工具面裁藏/URL 预抓/收尾兜底四次踩坑）**结论完全一致**。这不是外部强加的
  新范式，是把 veya 自己趟出来的雷区抽象成了通用契约——实施时应该反过来引用 §2.1 作为 P1/P2/P3
  的证据，而不是把 VAOM 当作纯理论输入。

---

## 2. 命名冲突清单（实施 RFC-01 前必须先解决）

| 名称 | 2.0 文档定义 | 代码现状 | 建议 |
|---|---|---|---|
| **Genesis** | 搜索/变异、产生候选方案策略，不直接 promotion 到 production | **已确认，两个不相关的同名物**：① `server/agents/genesis_agent.py` = 3O 主库"护库智能体"，职责是维护 `element_ledger`/`experience_log`，与"搜索变异"无关；② `server/darwin_evolution.py:101-128 VeyaDarwinEvolution._default_variant_fn` 内部**已经把突变器叫作"Genesis LLM"/"Genesis 进化引擎"**（提示词原文："你是 Genesis 进化引擎"，构造参数 `genesis_llm`），且完整实现了衰减→突变 3 变种→并发回测→择优→PRD 审批（`_default_notify` 推送 `HITL_REQUIRED`，`promote()` 需人工点头）——这**正是** VAOM 定义的 Genesis 语义（搜索变异候选 + 不直接 promotion，需通过 Promotion Gate），只是包在 `DarwinEvolution` 这个类名下，没有独立暴露成 `Genesis` 对象 | RFC-01 采纳 `darwin_evolution.py` 内部已有的 "Genesis" 语义作为 VAOM Genesis 的**现有实现**，不是从零设计；`server/agents/genesis_agent.py` 与 VAOM 无关，保留原名不动，两者不合并、不改名，只在 RFC-01 文档里显式注明"同名不同物，互不影响" |
| **Episode** | 一次任务完整因果账本：goal/context/decisions/claims/evidence/evaluations/artifacts/cost | `platform/3O/omodul/omodul/append_episode.py` = 学习回流的"唯一合法入口"，字段是 `project_id`/`env_fingerprint`，语义是"一条学习样本"，粒度和用途都不同 | 3.0 新对象改名（如 `TaskEpisode` 或 `ExecutionLedger`），避免与 omodul 既有 Episode 混淆；或者反过来评估 omodul 的 Episode 能否扩展字段承载新语义而不是并存两个同名对象——这个判断需要 omodul 维护者参与，不应单方面决定 |
| **obase**（RFC-03 撰写时新发现，非 2.0 文档提出的对象，记录在此避免遗漏） | — | 两个不相关的同名包：`platform/3O/obase`（3O 子模块）与 `veya/obase/telemetry.py`（本地包，含 `TraceContext`）同名不同物 | 暂不处理，留给 RFC-01 下次修订补充命名冲突清单；已在 `dev/rfc-03-veyaevent-envelope.md` §1 记录 |

---

## 3. 关键前提修正（相对 2.0 原文档）

### 3.1 "现在不引入 Qdrant" 的推理前提需要重写

2.0 原文 19.1 节假设数据主存已经统一在 Postgres + pgvector。实测三头并行：

- 元数据默认后端是 **DuckDB**（`platform/3O/oprim/oprim/meta_db/__init__.py:18-25`），
  `META_DB_BACKEND=postgres` 才切换到 Postgres（`oprim/meta_db/postgres.py` 的注释显示这是从
  DuckDB 迁移来的生产修复，不是规划起点）。
- 知识混合检索实际路径是 **LanceDB + Tantivy**（`platform/3O/oskill/oskill/hybrid_search.py:1,11-15`，
  BM25 + dense + RRF 融合），不是 pgvector + FTS。
- pgvector 的 HNSW 实现确实存在（`platform/3O/obase/obase/persistence/vector.py`），但不在当前热路径上。

**结论**：Phase 3（Long-Term Memory）的范围要重新估计——不是"给已有 Postgres 数据建
MemoryRecord schema"，而是先决定 DuckDB/LanceDB/Postgres 三者的分工或收敛方向。这本身是一个
需要单独拍板的架构决策，不应该隐含在 Phase 3 的执行细节里悄悄做掉；建议单独开一轮 RFC。

### 3.2 `veya_loop/` 不需要重新设计，直接采纳

`veya_loop/src/veya_loop/__init__.py` 已经实现了因果诊断（`causal_fault_diagnose`）、反事实
（`counterfactual_diagnose`/`StructuralSCM`）、干预（`closed_loop_intervene`）、审计
（`AuditEmitter`）、可靠性执行（`HardenedExecutor`/`PermissionContract`）——与 2.0 文档第 17 节
"Veya Loop 负责因果诊断/反事实/干预/执行/审计/可靠性与状态控制"的定义**高度吻合**，是三个组件
（Genesis / Veya Loop / Learning Layer）里成熟度最高、命名与职责最一致的部分。

3.0 不应该在这块新建代码，只需要把它的既有接口正式收编进 VAOM 语义层（例如评估 `AuditEmitter`
的输出能否直接升级为 §5 提出的 VeyaEvent 信封的一个 source，而不是并行维护第三套事件系统）。

### 3.3 `knowledge_reflux.py` 是 MemoryController.resolve_conflict 的现成起点，不是空白

`platform/3O/omodul/omodul/knowledge_reflux.py` 已经实现 supersedes 环检测、contradicts 对称
补全、supersede 状态传播、coherence boost/defeater（"A20 三铁律"），高风险变更进
`needs_review` 而不静默生效——这比 2.0 文档里 `resolve_conflict()` 一行接口描述的要具体扎实得多。

3.0 设计 MemoryController 时不应该另起炉灶重写冲突处理逻辑，应该以这个模块为基础做适配层。
真正缺的是两点：① 作用对象目前是 KU 图节点，没有 MemoryRecord 要求的 `scope`
（user/project/repo/global）维度；② 没有 candidate→verified→deprecated 的状态机（现状是
`epistemic_status.grade` 的验证等级阶梯，语义相近但不是同一个状态机，需要显式做映射而不是假设等价）。

### 3.4 `bandit_router.py` 是 PerformanceProfile 的现成雏形，只差"接线"——且接线本身需要 §4 审批

`platform/3O/oskill/oskill/bandit_router.py:30-121` 的 Thompson sampling
（`BanditState{alpha,beta,success_rate}`）结构上就是 PerformanceProfile 的简化版，但完全没有被
`server/coordinator_master.py` 引用——MasterAgent 现在选执行者靠的是系统提示词里的硬编码规则文本
（`coordinator_master.py:216-219`："Write/edit/run/test → hicode_run"），不是数据驱动。

这是当前离"证据驱动选择"最近的一步：Phase 5（Adaptive Intelligence）的最小可行版本可以先把
`bandit_router` 接到一个 MasterAgent **可选调用**的只读工具（例如 `harness_performance_query`），
而不是重新设计一套统计模型。

**但必须明确**：`ARCHITECTURE_STABLE.md` §4 规定"改模型路由/工具面"需要先向用户说明并获得同意
才能动手。即便这个新工具只是展示证据、不做程序路由（跟 P2 原则一致——"最终选择由 MasterAgent
决定"），它仍然是往主链工具面新增一个工具，必须走审批，不能当作"纯文档/测试"类改动直接提交。

### 3.5 `darwin_evolution.py` 顺带证明了 Promotion Gate 模式已经在生产里跑通过一次

§2 确认的 Genesis 现有实现（`darwin_evolution.py`）不只是"搜索变异"，它的完整链路
（衰减检测 → Genesis LLM 突变 3 变种 → `QuantCoprocessor` 隔离沙箱回测 → 择优 →
`get_prd()` 生成 PRD → `_default_notify` 推送 `HITL_REQUIRED` → 人工 `promote()`）
本身就是一次 CandidateLearning → Evaluation → Promotion Gate 的完整实践，只是领域局限在
量化算子。3.0 设计 PromotionGate（PR-22）时，这是唯一一个"已经在生产验证过、而不是纸面
设计"的参照实现，应该优先抽取它的**流程形状**（候选生成 → 独立评测 → 人工升级审批 →
可追溯 PRD），而不是照抄 2.0 文档字段表从零建模。

---

## 4. 对标标杆项目 — 现状覆盖度重估

沿用 2.0 文档第 20 节的 14 个参考项目，标注哪些方向 veya 已有可复用雏形、哪些确实是从零开始：

| 项目 | 2.0 设想的吸收方式 | 代码现状 | 3.0 建议 |
|---|---|---|---|
| Letta/MemGPT | Stateful Agent、context/memory hierarchy 设计参考 | `SessionTreeMgr`（`veya/omodul/session_tree.py`）已有 id/parent_id/leaf 树 + branch/fork，是相近的 stateful 结构 | 设计参考，不必照搬；SessionTree 已验证可用，优先复用 |
| Mem0 | memory extraction/dedup/conflict/lifecycle 思想，可选 provider | `knowledge_reflux.py` 的冲突处理比 Mem0 描述的更具体（见 §3.3） | 不需要接入 Mem0 作为 provider，现有实现已经更扎实；仅在"多来源 memory 后端切换"这个可插拔性目标上参考其 provider 抽象方式 |
| Memvid | temporal archive / portable capsule / replay | 全库无 grep 命中，无对应实现 | 真正的 0 到 1，按 Phase 6 Archive Adapter 处理，不冲突现有代码 |
| Vibe Squad | cross-family review、candidate→verified 机制参考 | `code_review.py`（post-execution advisory dual-axis）+ `plan_review.py`（pre-execution blocking dual-axis）已经是双轴独立评审的雏形，但都是**无状态单次判断，不持久化**，不产生 candidate 对象 | 机制参考已经部分吸收（双轴不融合评审原则），缺的是把评审结果持久化成 CandidateLearning/EvaluationResult，而不是重新设计评审机制本身 |
| HarnessRouter/UHP | 统一 Harness Protocol 边界 | CC/Pi/Hicode/DSH 各自硬编码调用（`server/project_ask.py`），无统一契约（见现状盘点） | 真正的 0 到 1，Phase 2 optional adapter，且要先有 HarnessSpec（PR-14）才谈得上 Router |
| Agency Agents ZH | Role/capability ontology 导入源 | `team_coord.py::TeamStore` 已有 Task Graph + Roles + Sessions 雏形（本 session 早前实现），语义上接近文档第 16 节"Team 被定义为 Task Graph + Roles + Sessions + Shared Acceptance Criteria" | 不需要从 Agency Agents 导入角色本体，现有 team_coord 已经是同一方向的实现，只是尚未接入 Capability Registry（本来就不存在） |
| Claude Cookbooks | 官方 recipe/eval/tool/subagent 知识源 | `templates/skills/` 的 skill 目录约定与之类似，但缺少版本化 recipe importer | Phase 6 Knowledge/Recipe Importer，低优先级，不阻塞主链路 |
| Impeccable | Capability Package 标杆（knowledge+skills+commands+evaluators） | 现有 `templates/skills/<name>/{manifest.json, run.py}` 只是单一 Skill 的两文件结构，离 Impeccable 标准目录（`CAPABILITY.yaml + skills/ + knowledge/ + commands/ + evaluators/ + benchmarks/ + adapters/`）差距最大 | 真正的 0 到 1，是 PR-13（CapabilityPackage）的直接对标对象 |
| DeerFlow | SuperAgent harness 参考 | 无对应实现 | 参考 + 可选 Harness Adapter，Phase 6，不影响主链 |
| LongHorizon-Harness | fresh executor + independent auditor + verified progress | `server/goal_run/leaf.py::execute_leaf` 的 brief 组装（memory_prefix + acceptance + instruction）已经接近"fresh executor 只拿任务切片"的精神；`plan_review.py`/`code_review.py` 是 auditor 雏形 | 架构吸收已经部分发生（不是理论上的参考，是实践中独立长出了相似形状），3.0 应该反过来把 goal_run 现有实践正式对齐进 LongHorizon 的 Manager/Executor/Auditor 术语，而不是重新设计 |
| Ponytail | Skill benchmark / cost-quality evidence 哲学 | Skill 系统完全没有 benchmark 机制（见 §2 Capability 现状） | 真正的 0 到 1，SkillSpec v2（PR-12）的核心动机 |
| lieflat-less-ai-tone | 窄而专业的可组合 Skill 范例 | 现有 skill 生态本身已经是"窄 skill"惯例（如 `eng_gap_audit`、`code_review_graph`），符合这个哲学 | 已经对齐，不需要额外动作 |
| my-ielts | KnowledgePack 样本 | 全库无 KnowledgePack 对应实现 | 真正的 0 到 1，Phase 2，低优先级 |

---

## 5. 修订后的分阶段实施顺序（P0-P6）

沿用 2.0 文档第 23 节的 30 项 PR/RFC 顺序，但标注每项是"从零建"还是"改造现有资产"，
并把 §3 发现的前置调研项插入正确位置：

| # | 阶段 | 原 PR/RFC | 3.0 标注 |
|---|---|---|---|
| 0a | P0 | （新增，插在 RFC-01 之前） | **前置调研**：确认 `server/darwin_evolution.py` 是否是 Genesis/EvolutionSearch 的候选实现（§2） |
| 01 | P0 | RFC: VAOM 2.0 | 改造：命名冲突（Genesis/Episode）必须在这一步解决，否则 Canonical IDs（PR-02）会直接绑定错误的命名 |
| 02-03 | P0 | Canonical IDs / VeyaEvent Envelope | 从零建，但 VeyaEvent 设计时要评估能否统一现有三套并行事件（`audit.py`/`loop-plane event_store.py`/`obase/telemetry.py`），而不是新增第四套 |
| 04-09 | P1 | Episode Ledger / Claim / Evidence / EvaluationResult / VerifiedState / GoalKernel Gate | 从零建；`verify.py` 三层判断（rule/mechanical/LLM）可以直接作为 EvaluationResult 的 E0/E0/E2 层落地起点，不必重新设计验证逻辑本身 |
| 10-17 | P2 | CapabilitySpec 起 | 从零建（无现成雏形），但 HarnessSpec（PR-14）设计时要覆盖 `hicode_agent.py::SandboxBroker` 已有的 workspace 锁语义，不能推倒重来 |
| 18-19 | P3 | MemoryRecord + pgvector / MemoryController | **先做 §3.1 的数据底座决策 RFC**，再动 schema；MemoryController 应基于 `knowledge_reflux.py` 做适配层（§3.3），不重写冲突处理 |
| 20-24 | P4 | CandidateLearning 起 | 从零建，但 `plan_review.py`/`code_review.py` 的双轴不融合评审原则应原样保留进 CrossFamilyReviewer（PR-23），只补持久化 |
| 25 | P5 | Adaptive Performance Query | 基于 `bandit_router.py` 改造（§3.4），**接入主链工具面前需走 §4 审批** |
| 26-30 | P6 | 外部 Adapter/Importer | 从零建，优先级参照 §4 标杆覆盖度表，Impeccable/HarnessRouter/Memvid 是真正空白，其余部分已有雏形可以降低优先级 |

---

## 6. 不做什么（继承 2.0 第 26 节，新增基于代码现实的项）

在 2.0 原有 12 条基础上，新增：

- 不在 RFC-01（VAOM 2.0）通过前修改 `genesis_agent.py` 或 `darwin_evolution.py` 的既有职责——
  先确认两者与文档 Genesis 定义的关系，不假设。
- 不把 `bandit_router` 接入 MasterAgent 工具面前跳过 `ARCHITECTURE_STABLE.md` §4 审批，即使
  该工具只读不做程序路由。
- 不假设 Postgres 已经是唯一数据底座就直接写 Phase 3 迁移代码——先出 §3.1 提到的数据底座 RFC。
- 不在没有先跟 omodul 维护者对齐的情况下，直接给 `append_episode.py` 的 Episode 概念改语义或
  改名——它是"回流飞轮唯一合法入口"，改动影响面不明。
- 不重写 `knowledge_reflux.py` 或 `veya_loop/` 的既有冲突处理/因果诊断逻辑——这两块经过验证，
  3.0 的工作是"收编进语义层"，不是"重新设计"。
- **不写连不到任何真实系统的"适配器"类**——PR-26/27/28（UHP/Memvid/DeerFlow/
  LongHorizon Adapter）没做：本仓库/本环境没有这些系统的真实协议规范/实例/仓库，
  写出来的类只会是没有真实对端的空壳。这不是"等消费者"，是拒绝写看起来实现了
  某能力、实际连不到任何东西的代码——真要接哪一个，需要用户提供该系统的真实
  规范/仓库/可达实例作为输入，不能靠猜。

> **历史记录（已被用户明确要求覆盖，仅留痕）**：本条目早先版本建议"PR-15
> （HarnessRegistry.execute() 接真实调用路径）等出现真实消费者再做"，
> 2026-08-23 用户明确指出"这不该是等客户需要才做"并要求立即完成——已实现，
> 见 §5 表与 `dev/rfc-01-vaom.md` 的 Harness 对象状态。判断本身（"没有消费者
> 时优先级更低"）不是错的工程原则，但用户的意愿优先，且事后验证这次改动
> 本身零回归——保留这条记录是为了如实反映决策过程，不是证明自己当时判断
> 有误或无误。

---

## 7. 验收标准

沿用 2.0 文档第 24 节 P0-P6 验收标准，不重复定义；3.0 额外增加两条前置验收：

- **P0 追加**：命名冲突清单（§2）里的每一项都有明确决议（改文档命名 or 改代码命名 or 判定为
  同一概念做映射），不允许"待定"进入 P1。
- **P3 追加**：数据底座 RFC（§3.1）产出前，不得开始 MemoryRecord schema 的实现型 PR。

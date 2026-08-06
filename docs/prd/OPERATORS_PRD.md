# 4 算子正式 PRD — 外网行为 / 外网数据 / 内网代码 / 交付物生产

> 版本: v1.0 · 状态: **已固化进 3O 账本** (delegate_to_genesis, `server/operator_ledger.py`)
> 注册: `obase.agent_registry.AgentRegistry` (agent 型) · 装配: `Infra.init` 幂等注册
> 对应技能包: `~/.veya/skills/{browser_use, agent_reach, officecli}` + `server/codebase_memory.py`

---

## 0. 背景与定位

四件套集成 (browser-use / Agent-Reach / codebase-memory / OfficeCLI) 统一收口为
**4 个 3O 正式算子**, 经 SkillHub/ToolRegistry + permission_gate + audit 暴露给主脑与外部调用。

```
browser_use_agent       外网行为层 — 自然语言驱动浏览器
agent_reach_channel     外网数据层 — 多渠道抓取 + 回退
codebase_memory_graph   内网代码智能层 — 调用链/BlastRadius/死代码
officecli_doc_engine    交付物生产层 — docx/xlsx/pptx 渲染-观察-修复
```

## 1. 算子规格

### 1.1 `browser_use_agent`

| 项 | 值 |
|---|---|
| 3O 层 | veya 业务装配 (技能包 `browser_use` 驱动) |
| 输入 | `goal: str` (必), `url: str`, `max_steps: int=10` |
| 输出 | `{ok, steps, final, output}` |
| 依赖 | `browser-use` + `playwright` + LLM (复用 Veya provider 链) |
| 安全 | 真实网络 + LLM token 消耗, **不跑沙箱**; 登录态 `~/.veya/browser-profiles/` |
| 失败模式 | 未安装 → 安装指引; 执行失败 → 结构化 error |

### 1.2 `agent_reach_channel`

| 项 | 值 |
|---|---|
| 3O 层 | veya 业务装配 (MCP 型技能包 `agent_reach`, 桥接 `127.0.0.1:8899`) |
| 输入 | `channel: str` (youtube_transcript/twitter_timeline/reddit/bilibili_comments/xiaohongshu/xueqiu), `url: str`, `limit: int=20` |
| 输出 | `{ok, output}` (渠道内容) |
| 依赖 | agent-reach MCP sidecar (SidecarManager 编排, 熔断兜底) |
| 安全 | 凭证只存 `~/.agent-reach/config.yaml` (0600); 输出过 redact; URL 过 SSRF 白名单 |
| 失败模式 | sidecar 不可达 → 结构化错误 (不崩溃); 后端失效走断路器/first-choice-fallback |

### 1.3 `codebase_memory_graph`

| 项 | 值 |
|---|---|
| 3O 层 | veya 业务装配 (`server/codebase_memory.CodebaseMemoryConnector`) |
| 输入 | `query: str`, `kind: str ∈ {call_graph, blast_radius, dead_code}`, `depth: int=2` |
| 输出 | `{ok, kind, results}` |
| 依赖 | codebase-memory 索引 (connector, sidecar 管理) |
| 安全 | 只读查询; 索引限工作区 |
| 失败模式 | 索引未就绪 → `ensure_indexed` 或结构化错误 |

### 1.4 `officecli_doc_engine`

| 项 | 值 |
|---|---|
| 3O 层 | veya 业务装配 (技能包 `officecli`) |
| 输入 | `op ∈ {add,edit,read,convert,merge,dump,batch,render,watch}`, `input`, `output`, `data_json`, `options` |
| 输出 | `{ok, op, stdout, output_path, readonly}` |
| 依赖 | `officecli` 二进制 (官方 install.sh/brew/npm + sha256 入库) |
| 安全 | **写路径白名单** (workspace + `~/.veya/templates/`); 只读免审批; 变更审计 `~/.veya/audit/officecli.jsonl` |
| 加成 | 渲染→G13 Vision→修复 闭环 (`scripts/officecli_vision_loop.py`) |

## 2. 安全边界 (零信任, 全部算子)

| 规则 | 落地 |
|---|---|
| 二进制来源 | 官方渠道 + sha256 校验入库 |
| 写路径 | 仅 workspace + `~/.veya/templates/` (officecli) |
| 凭证 | vault/0600 文件, 永不进对话与模板 |
| 网络 | browser-use/agent-reach 需真实网络 (不跑沙箱); officecli 离线 |
| 权限模型 | 只读免审批; 写操作 permission_gate |
| 审计 | 文档变更/敏感操作全量 JSONL |

## 3. 验收标准 (AC)

- [x] 4 算子注册进 AgentRegistry (幂等, 重复调用零冲突)
- [x] 每个算子不可用依赖 → 结构化错误而非崩溃 (未装/不可达/索引缺失)
- [x] skill_hub 热载三技能包 (browser_use/agent_reach/officecli)
- [x] officecli 写路径白名单实测 (白名单外 PermissionError)
- [x] sidecar 管理器: 健康轮询 + 熔断 (3 次/60s) + 统一回收
- [x] 渲染→观察→修复闭环 dry-run 通过
- [x] 全量测试绿 (636 passed)

## 4. 账本条目 (delegate_to_genesis)

注册点: `server/operator_ledger.py::register_operators()` → `Infra.init` (装配期调用, 幂等)

```python
# 账本结构
_LEDGER = {
  "browser_use_agent":    {"layer": "外网行为层",  "skill": "browser_use"},
  "agent_reach_channel":  {"layer": "外网数据层",  "skill": "agent_reach"},
  "codebase_memory_graph":{"layer": "内网代码智能层","skill": None},
  "officecli_doc_engine": {"layer": "交付物生产层", "skill": "officecli"},
}
```

查询: `AgentRegistry.get("agent", "officecli_doc_engine")` 或 `ledger_summary()`。

## 5. 后续演进 (非本期)

- 算子升级为**独立 3O 主库模块** (如 `omodul/office_engine.py`) 满足"机制进主库"终态
- agent_reach doctor 挂 cron 健康巡检 (自动化 + 断路器联动)
- 模板库 + 周报/审计报告自动产出 cron (配合 `system_dispatch_omni_channel`)
- officecli daemon 常驻模式 (SidecarManager 编排, 多步编辑近零延迟)

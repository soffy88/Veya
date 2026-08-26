# 主脑瘦身 ①② — 提示去自吹 + 工具面静态收口

> 状态: ① 完成 · ② 规范待 prod-parity 执行 · 2026-08-09
> 目标: 减主脑负担(工具面 93/线上~157 太肥, 提示 38KB) → 回答不被工具带偏、不夹带系统介绍。
> 权威主链路见 [`ARCHITECTURE_STABLE.md`](ARCHITECTURE_STABLE.md); 当前主脑使用 veya1.2 的 OpenRouter 双模型池。

## 冻结兼容性(关键)

冻结 §2.1 禁止的是 **动态按关键词裁藏**(`_layer_tools` 每请求猜该藏谁)。本方案是
**静态收口**(默认 palette 就小而精、人人可见、模型照样自主全调) —— 与 Claude(28)/Pi(4-6)
同理, **不是**动态路由, 不违冻结。

## ① 提示去自吹 ✅ 已完成·已验证

- `server/coordinator_master.py::_slim_master_prompt`(veya 层过滤, 不改子库): 删除
  "industrial-grade Agentic system and quantitative research core" / "elite AI orchestrator"
  两句自我标榜(正是"回答夹带系统介绍"的根)。功能指令(INTENT ROUTING / EXECUTE-WHEN-ASKED /
  SWARM / VAULT …)全保留; 匹配不到则原样返回(子库措辞变动不崩)。
- 验证: puffery 已消失, 功能段全在。

> **诚实修正报告**: 提示 38KB 的大头**不是**自吹(自吹仅 2 句), 而是 ② 里那份和 `tools`
> 参数**重复**的工具清单文本 + 逐 skill describe。真正的字节瘦身在 ②。

## ② 工具面静态收口 — 规范(须 prod-parity 环境执行+验证)

> **为什么不在开发沙箱做**: 要收的大头 mcp 67 + skills 72 **在无依赖沙箱加载不出来**
> (无 live mcp 端点、`~/.veya/skills` 空, 实测工具面仅 93=core+system)。收口触及冻结
> 工具面 + oservi 提示渲染(行 312), **只有真实 skills/mcp 跑起来才能验证"收口后模型仍
> 正确调工具"**。盲改 = 拿线上工具调用赌博, 不做。

### ②-A skills 72 → 2 (dispatcher) ✅ 已完成·真实容器验证

**实测(veya-backend 容器, 真实 72 skills):**
- 主脑工具面 **93 → 23**(10 核心 + 11 system + 2 skill dispatcher)。
- 系统提示 **39,161 → 18,814 字节(腰斩)** —— skills 不再逐条进 system(`list_skills()`
  dispatcher 模式返回空 → oservi:312 不渲染), 发现改走 run_skill 的 catalog。
- `run_skill` 正确路由到真实 executor; `list_skills` 返回 72 条目录; `VEYA_SKILL_DISPATCHER=0`
  一键回退到 93/39KB, 实测无误。
- 实现: `_dispatcher` flag(默认 ON) + `_dispatcher_schemas()` + `execute()` 解包 run_skill/
  list_skills + `list_skills()` 空返回 + `_all_skill_names()` 供 stats。**未改 3O 子库**
  (靠 master_agent:600 的 `skill_hub.execute` fallback 路由)。
- ⚠️ 待生产观察: 模型是否稳定用 list→run(机制已验; 异常即 `VEYA_SKILL_DISPATCHER=0` 回退)。

---
原始设计(供参考):

- 文件: `server/skill_hub.py`(veya 层, 非子库)。
- `get_all_schemas()`: 有 skills 时返回 2 个 dispatcher schema —
  `list_skills()`(返回 name+describe 列表) + `run_skill(skill_name, args)`。
- `execute(name, kwargs)`: 路由 `run_skill` → `self._executors[skill_name](**args)`;
  `list_skills` → 返回技能清单。原 per-skill executor 不动。
- **提示一致性(要害)**: oservi `master_agent.py:312` 会逐 skill 渲染 `describe()` 进提示。
  收口后须让 skill 清单**不再逐条进提示**(否则提示描述 72 个 skill、工具面只有 run_skill →
  模型困惑)。做法: dispatcher 模式下 `list_skills()` 对 oservi 渲染返回空、发现走
  `list_skills` 工具。**此项改变 oservi 消费面, 须真实 skills 验证模型仍会先 list 再 run。**

### ②-B mcp 67 → 4 (按服务网关) ✅ 已完成·运行服务实测

**实测(veya-backend 运行服务, 热 mcp 连接):**
- master_tools **85 → 22**; mcp 67 → **4 网关**(mcp_hevi/mcp_stratum/mcp_od/mcp_codebase), 细工具残留 0。
- 每网关 catalog 完整(如 mcp_stratum: 1935 字节 / 18 actions 全列), 模型经 `mcp_<server>(action, args)` 调任意能力。
- 全 agent 面 ~168 → **~35**(11 核心 + 4 mcp 网关 + 7 reasonix + 2 skill dispatcher + 11 system)。
- 实现: `server/tool_registry.py::register_mcp_tools`(共享 helper) + 4 个 `wire_master_tools` 各改 1 行调用; `VEYA_MCP_GATEWAY=0` 回退逐工具。**未改 3O 子库**。
- ⚠️ 待生产观察: 模型是否稳定用 `mcp_<server>(action)`(机制+catalog 已验; 异常即 env=0 回退)。

---
原始设计(供参考):

### ②-B mcp 67 → ~4 (按服务网关)

- 每个 mcp 服务一个网关工具代替 N 个细工具: `mcp_stratum(action, args)` /
  `mcp_hevi(action, args)` / `mcp_od(action, args)` / `mcp_codebase(action, args)`。
- 位置: mcp 工具由 `server/skill_hub.py::_create_mcp_executor` + 注入面产生。
- **须 live mcp 端点验证**: 网关分发到真实 mcp 后端、模型能按 action 正确调用。

### ②-C 提示删冗余工具清单(最大字节收益)

- oservi `master_agent.py` get_system_prompt 行 306 把 AVAILABLE TOOLS **文本清单**
  拼进提示 —— 与传给 LLM 的 `tools` 参数**重复**。现代 function-calling 靠 `tools` 参数,
  文本清单冗余。
- 位置: **oservi 子库**(get_system_prompt)。删它须改子库 + **真实验证 opencode-go 网关
  在无文本清单时仍正确 function-calling**(部分网关可能依赖文本清单)。风险中, 必须实测。

### 验证基线(prod-parity)

1. 装全 `deploy/requirements.txt` / deploy 容器内, 真实 skills 目录 + live mcp。
2. 断言: 工具面 count 93/157 → ~15; 提示字节明显下降。
3. 端到端: 同一批任务(设计/编码/知识/视频)收口前后对比 —— 模型仍正确 list→run skill、
   按 action 调 mcp、不再被工具带偏; 回答质量不降。
4. 灰度: 每项(A/B/C)独立 flag, 一项一验收, 可回退。

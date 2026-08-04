# veya CC 指令包 — 补完 layer4 并跑通

**目标**: 在 veya 仓库(打平布局,import 根 = veya/)补完 layer4 内部模块,接通已入库的五库+obase,跑通阶段 1。
**布局**: `veya/server/`、`veya/cli/`、`veya/agents/`、`veya/hooks/`、`veya/config/`、`veya/registries/`(全打平,`from server.assembly import ...`)。
**已有**: `server/assembly.py`、`server/coordinator.py`、`cli/headless.py`(本包附带,import 已按打平布局)。
**库已就绪**: oprim/oskill/omodul/oservi/obase 全部入库。

> CC 按 TASK 顺序执行。每个 TASK:目标 / 创建文件 / 实现要点 / 验收。
> ⚠️ TASK 0 必须最先做(核对 import),否则后续全建在错误签名上。

---

## TASK 0 — 核对库 import(阻塞性,最先做)

**目标**: 确认 server/assembly.py 顶部所有 import 能解析,签名与 manifest 一致。

**动作**:
```bash
cd veya
python -c "from oservi import assemble, ServiceManifest; print('oservi ok')"
python -c "from oprim import llm_call, file_read, bash_exec, ripgrep_search, glob_match, dir_list, file_write, file_read_range, apply_string_replace, compute_diff, http_fetch, web_search_query, diff_session_state, mcp_connect, mcp_call_tool, lsp_goto_definition, lsp_find_references, lsp_hover, lsp_document_symbol, lsp_workspace_symbol, lsp_goto_implementation, lsp_prepare_call_hierarchy, lsp_incoming_calls, lsp_outgoing_calls, lsp_diagnostics, todo_serialize, todo_deserialize; print('oprim ok')"
python -c "from oskill import code_search; print('oskill ok')"
python -c "from omodul import process_prompt, execute_tool, run_subagent_task, compact_session, init_project; print('omodul ok')"
python -c "from obase.provider import ProviderRegistry, CostTracker; from obase.lsp import LspManager; from obase.mq import EventBus; print('obase ok')"
```

**若任何 ImportError**: 用 `from oprim import __manifest__; print([e.name for e in __manifest__])` 查真实名,改 `server/assembly.py` 的 import 和 `_ALL_TOOLS` 映射。**以库为准。**

**验收**: 五条 print 全部输出 ok。记录任何改动的名称映射,后续 TASK 沿用。

---

## TASK 1 — config 模块

**目标**: 配置加载 + 权限规则,assembly/coordinator/headless 依赖它。

**创建**:
```
config/__init__.py
config/loader.py        load_config(path: str | None) -> dict
config/schema.py        配置 schema(校验机制调 obase.config.validate_schema)
config/permissions.py   load_permission_rules(persona: str) -> list[Callable]
```

**实现要点**:
- `load_config`: 读 global→project→env 顺序(P resolve_config_paths 已有 oprim),合并(obase.config.deep_merge),校验(obase.config.validate_schema)。无配置文件 → 返回默认 dict(含 providers/lsp 占位)。
- `load_permission_rules(persona)`: 返回该 persona 的权限 filter 列表(给 triage 的 filters 注入点)。filter 是 layer4 callable,内部调 oskill `permission_evaluate`。

```python
# config/permissions.py 骨架
from oskill import permission_evaluate


def load_permission_rules(persona: str) -> list:
    rules = _RULES_BY_PERSONA.get(persona, _RULES_BY_PERSONA["build"])

    def filter_fn(tool_call):
        return permission_evaluate(tool_call, rules=rules, persona=persona)

    return [filter_fn]


_RULES_BY_PERSONA = {
    "build": [...],  # 全 allow,bash ask
    "plan": [...],  # 写操作 deny(只读)
    "research": [...],  # 写操作 deny
}
```

**验收**: `from config.loader import load_config; load_config(None)` 返回 dict。

---

## TASK 2 — agents 模块(persona)

**目标**: 三种分队角色的 tool 白名单 + persona 解析。assembly 的 `_resolve_tools` 依赖。

**创建**:
```
agents/__init__.py      resolve_persona(name: str) -> Persona
agents/_base.py         Persona dataclass(.tool_names: list[str], .system_prompt: str, .mode)
agents/build.py         执行分队:全 tool
agents/plan.py          规划分队:只读 + 不执行
agents/research.py      研究分队:只读探索
```

**实现要点**:
- `Persona.tool_names` 用 assembly.py 的 `_ALL_TOOLS` 的 key(read/write/edit/bash/grep/...)。
- build: 全部 key。plan/research: 只读子集(read/read_range/grep/glob/list/lsp_*/todo_read),**无** write/edit/bash 写。

```python
# agents/_base.py
from dataclasses import dataclass


@dataclass
class Persona:
    name: str
    tool_names: list[str]
    system_prompt: str
    mode: str  # "primary" | "subagent"


# agents/__init__.py
from agents.build import BUILD
from agents.plan import PLAN
from agents.research import RESEARCH

_PERSONAS = {"build": BUILD, "plan": PLAN, "research": RESEARCH}


def resolve_persona(name: str) -> "Persona":
    return _PERSONAS.get(name, BUILD)
```

```python
# agents/research.py 示例
from agents._base import Persona

READ_ONLY = [
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
RESEARCH = Persona("research", READ_ONLY, "探索代码库,只读,产出发现摘要", "subagent")
```

**验收**: `resolve_persona("research").tool_names` 不含 write/edit/bash。

---

## TASK 3 — hooks 模块(M6)

**目标**: hook 类型 + 切点链构建 + 三个 builtin。assembly 的 `build_hook_chain` / coordinator 的 `build_coordinator_hooks` 依赖。

**创建**:
```
hooks/__init__.py
hooks/types.py          HookInput / HookOutput dataclass(照 M6 SPEC §2/§3)
hooks/registry.py       build_hook_chain(point, persona) / build_coordinator_hooks()
hooks/builtin/__init__.py
hooks/builtin/test_gate.py    H4: 跑测试(调 oprim bash_exec)
hooks/builtin/permission.py   H2: 权限(调 oskill permission_evaluate)
hooks/builtin/redact.py       H3: 脱敏(调 oprim redact_secret)
```

**实现要点**(照 M6 SPEC):
- types.py: 严格按 M6 §2/§3 的 HookInput/HookOutput。
- registry.py: `build_hook_chain(point, persona)` 返回该切点该 persona 的 hook callable 列表。
  - `pre_tool` → [permission hook](所有 persona)
  - `post_tool` → [redact hook]
  - `pre_result` → [test_gate hook](仅 execute persona,research/plan 无需跑测试)
- builtin 只调已有库元素(不实现业务):

```python
# hooks/builtin/test_gate.py — H4 招牌
from oprim import bash_exec
from hooks.types import HookInput, HookOutput


async def test_gate(inp: HookInput) -> HookOutput:
    res = await bash_exec("pytest -x -q", cwd=inp.cwd, timeout=300)
    if res.exit_code != 0:
        return HookOutput(decision="block", reason=f"Tests failed:\n{res.stdout[-2000:]}")
    return HookOutput(decision="pass")
```

```python
# hooks/registry.py 骨架
from hooks.builtin.permission import permission_hook
from hooks.builtin.redact import redact_hook
from hooks.builtin.test_gate import test_gate


def build_hook_chain(point: str, persona: str) -> list:
    chains = {
        "pre_tool": [permission_hook],
        "post_tool": [redact_hook],
        "pre_result": [test_gate] if persona == "execute" else [],
    }
    return chains.get(point, [])


def build_coordinator_hooks() -> dict:
    # 协调级:H1 PreDispatch / H4 PreResult
    return {"pre_dispatch": [], "pre_result": []}
```

**验收**: `build_hook_chain("pre_result", "execute")` 含 test_gate;`build_hook_chain("pre_result", "research")` 为 []。

---

## TASK 4 — registries 模块

**目标**: tool/skill/plugin/model 注册表。

**创建**:
```
registries/__init__.py
registries/tools.py     get_registered_tools() —— 可复用 assembly._ALL_TOOLS
registries/skills.py    skill 注册 + 触发(调 P-NEW1/2/3,若已入库)
registries/models.py    model catalog(zen + Models.dev,调 M-12 / obase.provider)
registries/plugins.py   plugin 注册(调 P-NEW4/5 + K-NEW1,若已入库;否则占位)
```

**实现要点**:
- tools.py: 把 assembly.py 的 `_ALL_TOOLS` 迁来或复用(单一来源)。
- skills/plugins: 若 CC 补充元素(P-NEW*)已入库则接,否则建空注册表占位(M7 再填)。
- models: 调 obase.provider.ModelCatalog。

**验收**: `from registries.tools import get_registered_tools` 可用。阶段 1 可只做 tools,其余占位。

---

## TASK 5 — engine 接口适配(关键)

**目标**: 确认 assembly 返回的 Engine 有 coordinator/headless 用到的方法:`run_turn(input)` / `run_squad(role, command, squad_id)`。

**动作**:
- 查 oservi Engine 真实方法名(`dir(engine)` 或 manifest)。
- coordinator.py 用 `orchestrator.run_squad(...)`,headless.py 用 `engine.run_turn(...)`。**若库里方法名不同**(如 `run`/`execute`/`dispatch`),改 coordinator.py / headless.py 对齐。
- 若 oservi 引擎暴露的是统一 `run()` + 参数,相应调整调用处。

**验收**: 能在 Python 里 `engine = assemble_main_agent(persona="build"); engine.<run方法>(...)` 不报 AttributeError。

---

## TASK 6 — server app + 路由(阶段 1 最小)

**目标**: 最小 HTTP 入口(阶段 1 可先只做 /prompt,跑通后铺全路由)。

**创建**:
```
server/__init__.py
server/app.py           FastAPI app,挂载路由
server/routes/__init__.py
server/routes/prompt.py  POST /prompt → coordinator.handle
server/sse.py            SSE 推送(on_step → 前端;阶段 1 可桩)
```

**实现要点**:
- app.py: `Infra.init(load_config())` 启动钩子;挂 /prompt。
- /prompt: 收 command → `coordinator.handle(command)` → 返回结构化结果(或 SSE 流)。

**验收**: `uvicorn server.app:app` 起得来;POST /prompt 通。

> 阶段 1 也可跳过 server,直接用 cli/headless 验证(下一 TASK)。

---

## TASK 7 — 跑通阶段 1(验收里程碑)

**目标**: 库元素接口被真实验证。

**前置**: TASK 0-5 完成(config/agents/hooks/registries + engine 适配)。

**动作**:
```bash
cd veya
echo '{"text": "把 README.md 第一行改成 # veya"}' | python -m cli.headless
```

**期望输出**:
```json
{
  "status": "success",
  "output": { "diff": "..." },
  "cost_usd": 0.0x,
  "squads": [{"id": "s1", "role": "execute", "status": "success", ...}]
}
```
且 README.md 真被改。

**验收**:
- 退出码 0
- README.md 第一行变成 `# veya`
- 输出 JSON 含 diff
- **跑通 = 库接口验证通过,进阶段 2(铺全引擎+全路由)**

**若失败,按层排查**:
1. ImportError → 回 TASK 0
2. AttributeError on engine → 回 TASK 5(方法名)
3. cardinality 错(缺注入点)→ 查 assembly 注入是否齐
4. cost 不累加 → 查是否同一 CostTracker(coordinator 建一个传全程)
5. hook 不触发 → 查 build_hook_chain 是否挂上

---

## TASK 8+ — 阶段 2-5(跑通后)

阶段 1 通过后,按完整装配施工单铺开:
- 阶段 2: 其余 5 引擎装配 + 全 persona + 全 hook
- 阶段 3: 协调器多分队(research→plan→execute DAG)实测
- 阶段 4: 全路由(session/tool/agent/share/mcp/plugin/auth/undo)+ SSE
- 阶段 5: TUI(Textual)+ 可靠性(checkpoint/fallback)

---

## 依赖顺序图

```
TASK 0 (核对 import) ─── 阻塞全部
   ↓
TASK 1 config ──┐
TASK 2 agents ──┼──→ TASK 5 engine 适配 ──→ TASK 6 server ──→ TASK 7 跑通阶段1
TASK 3 hooks ───┤                                              ↓
TASK 4 registries┘                                      TASK 8+ 阶段2-5
```

TASK 1-4 可并行(互不依赖);TASK 5 依赖 assembly 能 import(TASK 0);TASK 7 依赖 1-5。

---

## 关键正确性(CC 勿改坏)

| 点 | 要求 | 锚点 |
|---|---|---|
| cost 跨引擎 | coordinator 建 1 个 CostTracker,传 orchestrator + 每个 main_agent;分队累加父 | §5.6 C1 |
| persona tool 隔离 | research/plan 无 write/edit/bash | agents TASK 2 |
| hook 只调库 | builtin hook 内 import 已有元素,不写业务 | M6 SPEC |
| R1 edit 检索 | retrieval 注入 code_search;edit 前先跑(引擎/execute_tool 内)| R1 |
| 库为准 | import/签名/方法名对不上,改 layer4 代码,不改库 | TASK 0/5 |

---

**End — CC 指令包**。CC 按 TASK 0→7 顺序执行,跑通阶段 1 后进 8+。附带文件:server/assembly.py · server/coordinator.py · cli/headless.py(import 已按打平布局)。

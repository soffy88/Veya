# hicode 核心装配代码 — CC 落地说明

三个核心装配点的可跑代码骨架。CC 拿这个作为 layer4 阶段 1-3 的起点。

## 文件

```
layer4/server/assembly.py     引擎装配中心:assemble_main_agent + 6 引擎装配函数
layer4/server/coordinator.py  协调器主循环:派 research/plan/execute 分队 + DAG 调度
layer4/cli/headless.py        headless 协议:结构化进出,无 TTY
```

三个文件已通过 `py_compile` 语法检查。

## CC 落地第一步(必做)

**核对 import 能否解析。** 本代码按交付的 IMPL SPEC 签名写,但我无法验证它与已入库 manifest 完全一致。逐个确认:

```python
# 在 hicode 环境跑,确认无 ImportError:
from oservi import assemble, ServiceManifest
from oprim import llm_call, file_read, bash_exec, ...   # assembly.py 顶部全部
from oskill import code_search
from omodul import process_prompt, execute_tool, run_subagent_task, ...
from obase.provider import ProviderRegistry, CostTracker
from obase.lsp import LspManager
from obase.mq import EventBus
```

任何名称/签名对不上,**以库 manifest 为准**改本代码。常见出入:
- tool oprim 的确切函数名(`file_read` vs `read_file`)
- `assemble` / `ServiceManifest` 的确切参数名
- `CostTracker` 的方法名(`.add()` / `.total_usd()`)
- 引擎的 `run_turn` / `run_squad` 方法名

## 还需 CC 补的 layer4 文件(本代码 import 了但未给)

这些是 assembly/coordinator 依赖的 layer4 内部模块,CC 需补:

```
layer4/agents/__init__.py        resolve_persona(persona) -> Persona(.tool_names)
layer4/agents/{build,plan,research}.py   三种 persona 的 tool 白名单
layer4/hooks/registry.py         build_hook_chain(point, persona) / build_coordinator_hooks()
layer4/hooks/types.py            HookInput / HookOutput(见 M6 SPEC)
layer4/hooks/builtin/            H4 测试门控 / H2 权限 / H3 脱敏(调已有库元素)
layer4/config/loader.py          load_config(path) -> dict
layer4/config/permissions.py     load_permission_rules(persona)
layer4/registries/tools.py       tool 注册(本代码用 _ALL_TOOLS 内联,可迁此)
```

## 装配关系图

```
headless.main()
  └─ Infra.init(config)              # 初始化 obase 基础设施单例
  └─ coordinator.handle(command)
       ├─ _decompose → SquadPlan      # 拆 research/plan/execute
       ├─ assemble_orchestrator(...)  # E-5,注入 run_subagent_task + 协调 hook
       └─ _run_squads
            └─ orchestrator.run_squad → run_squad_headless
                 └─ assemble_main_agent(persona)  # E-1,每分队一个
                      └─ engine.run_turn → process_prompt(M-01) → tools/code_search
                                          → H2/H3/H4 hook 切点
```

## 关键正确性点(已在代码体现,CC 勿改坏)

- **cost 跨引擎传播**:`coordinator` 建一个 `CostTracker()`,传给 `assemble_orchestrator` 和每个 `assemble_main_agent`(共享同一对象,§5.6 C1)。分队 cost 累加到父。**不要每分队新建 tracker。**
- **R1 edit 检索**:`assemble_main_agent` 注入了 `code_search` 作 retrieval。edit 前先跑它注入 hits 的逻辑在引擎/execute_tool 内,不在这里。
- **persona 决定 tool 子集**:`_resolve_tools(persona)` —— research/plan 只读,build 全集。白名单在 layer4/agents。
- **headless 退出码**:success→0,failed→1,供上游 CI/脚本判断。

## 阶段 1 验收(接通主 loop)

补完上述 layer4 内部模块后:

```bash
echo '{"text": "把 README.md 第一行改成 # hicode"}' | python -m layer4.cli.headless
# 期望:JSON 输出,status=success,output 含 diff,真实改了文件
```

跑通 = 库元素接口被验证,阶段 2-5 平铺。

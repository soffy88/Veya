## Veya Bot Product Shell

状态：`VEYA_BOT_PRODUCT_PHASE_STARTED`

本阶段把已经冻结的底座装配成一个可长期使用的默认 Bot。Product Shell 是
Layer4 产品配置与 UI，不是新的 runtime、memory、governance、computer、browser、
provider、tool/MCP 或 Workbench framework。

### 默认实例

默认实例的稳定 ID 是 `veya-default`，名称是 `Veya Bot`。配置仍写入已有的
`~/.veya/config.json`；`veya init` 产生的有效旧配置不需要 migration，即可被
Product Shell 识别为已完成初始化。

生命周期由现有配置和运行条件派生：

- `uninitialized`：尚未完成首次配置。
- `ready`：onboarding 已完成，provider/model、credential/reference 和 workspace 都可用。
- `degraded`：配置记录存在，但 provider 或 workspace 尚未就绪。

服务运行状态不是 Bot 的执行 authority。MasterAgent、GoalRun、现有 ActionGateway、
Computer/Browser、Memory/Skill 和 Workbench 继续保有各自的 canonical authority。

### API 与安全边界

`GET /api/v1/bot` 返回 secret-free 的 identity、readiness、capability bindings 和
恢复入口。`POST /api/v1/bot/onboarding` 只接收 provider/model/workspace 和
`credential_ref`；没有 `api_key` 参数。浏览器沿用现有 `apiKeyStore` 保存和发送
credential，onboarding API 只登记 non-secret reference。

前端默认主页提供：

- 首次配置提示和现有模型设置入口；
- 真实“New Task”输入入口：先创建 canonical session/task，再把同一个
  `task_id` 交给现有 `MasterCoordinator.chat_stream`，成功接受后进入
  `/workbench/:task_id`；
- 任务历史 / Resume 入口，跳转现有 TaskCenter/Workbench；
- 真实的 memory/skill、tool/MCP、computer/browser 绑定说明。

会话、任务、artifact、审批和 human takeover 均由现有 canonical backend API 恢复，
Product Shell 不复制其 payload，也不在前端计算事实状态。

### Product Phase 2：真实任务入口

`POST /api/v1/bot/tasks` 是 Layer4 的产品入口适配器，不是新的 task executor：

1. 创建持久 session history 与 TaskStore projection，并记录 `session.created`、
   `product.task_submitted`；
2. 在后台调用现有 `MasterCoordinator.chat_stream`，传入同一个 `session_id`、
   `task_id` 和 `trace_id`；
3. MasterAgent 继续负责语义决策，现有 GoalRun、ActionGateway、tool/MCP、
   computer/browser、verification 和 artifact 链路保持不变；
4. Workbench 只读取 canonical TaskStore/EventStore 和已有的 GoalRun/artifact
   投影，因此刷新或重新连接时可以按 task id 恢复。

Provider `config` 仅作为本次请求的运行时参数传递给 MasterAgent，不写入 task、
canonical event、transcript、artifact 或产品 API 响应。产品入口不自动执行任何
额外 remote side effect；工具自身仍使用既有 approval/ActionGateway 规则。

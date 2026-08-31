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
- 单一“新建任务”入口，进入现有 MasterAgent 对话链；
- 任务历史 / Resume 入口，跳转现有 TaskCenter/Workbench；
- 真实的 memory/skill、tool/MCP、computer/browser 绑定说明。

会话、任务、artifact、审批和 human takeover 均由现有 canonical backend API 恢复，
Product Shell 不复制其 payload，也不在前端计算事实状态。

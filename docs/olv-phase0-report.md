# Open-LLM-VTuber (OLV) Phase 0 集成报告

- 日期：2026-10-22
- 分支：`feat/olv-phase0`（worktree：`~/projects/veya-olv`）
- 基线：`main`（`cb03ebfe`）
- 依据：`docs/open-llm-vtuber-integration.md` 第七节「建议的 Phase 0」
- 原则：默认关闭、只读模块、旁路接入；不 merge、不 push、不部署、不重启服务；
  不引入 OLV 的 MCP/记忆；不打包 Live2D 模型；不打印任何密钥。

---

## 一、改了什么（4 项，对应文档 §7.2 / §7.3）

| # | 改动 | 文件 | 默认行为 |
|---|---|---|---|
| 1 | 默认关闭开关 `VEYA_OLV` | `server/feature_flags.py`（表尾追加，不重排） | OFF |
| 2 | 只读装配模块 `status()` / `config_template()` | `server/vtuber_bridge.py`（新增） | 关闭时 `status()` 只回 `{"enabled": False}` |
| 3 | 可选路由 `GET /api/v1/vtuber/status` | `server/routes/vtuber.py`（新增）+ `server/app.py` 挂载 | OFF 时 404 |
| 4 | OLV 配置模板（LLM 指向 Veya 网关） | `config/olv.conf.yaml`（新增） | 纯文件，无代码路径 |

`git diff` 规模：2 个既有文件共 8 行新增，4 个新文件。`pyproject.toml` /
`uv.lock` / 依赖树零改动。

### 1.1 开关

`server/feature_flags.py` 表尾追加（保持既有"追加不重排"约定）：

```python
# Open-LLM-VTuber (OLV) Phase 0 — default OFF bypass integration. Gates only
# a read-only status module and an optional status route; no tool surface,
# no process spawn, no OLV dependency (docs/open-llm-vtuber-integration.md).
FeatureFlag("VEYA_OLV", False, "runtime", "2027-01-01"),
```

走既有 `server.feature_flags.enabled()` 单一权威（runtime > env > file > default），
未设 → registry 默认 `False`；`0/false/no/off` 拼写同样关闭；只有
`1/true/yes/on` 打开。

### 1.2 只读模块 `server/vtuber_bridge.py`

按文档 §7.2，模块只暴露两个纯函数，**不 spawn 进程、不注册工具、不写任何持久状态、
不 import 任何 OLV 代码、不新增 Veya 依赖**：

- `status()` — 只读投影。开关关闭时返回 `{"enabled": False}`（逐字节，无其他键）；
  开启时追加 `gateway_url`（指向 `VEYA_OLV_GATEWAY_PORT`，默认 8791）、
  `token_required`、`chat_completions_url`、`models_url`、`health_url`、
  `model` 与 `emotion_keys`。
- `config_template()` — 返回 OLV `conf.yaml` 所需的只读片段：LLM 指向 Veya
  网关、`use_mcpp: False`、ASR/TTS 选择、`live2d_expression_prompt` 开启状态、
  表情键清单、口语 persona_prompt。**不含任何 token 值**，`llm_api_key` 只有
  `<issued token>` 占位。

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `VEYA_OLV` | 关闭 | 打开后 `status()` 返回完整只读投影，路由才存在 |
| `VEYA_OLV_HOST` | `127.0.0.1` | 模板里 OLV 访问 Veya 网关所用的 host |
| `VEYA_OLV_GATEWAY_PORT` | `8791` | Veya LLM 网关端口（`scripts/veya_llm_gateway.py` 文档串的默认值） |
| `VEYA_OLV_MODEL` | `veya1.2` | 模板里填给 OLV 的模型名 |

### 1.3 可选路由

`server/routes/vtuber.py`，挂载于根 app（`server/app.py` 表尾追加 import +
include_router，与既有 NANO-W7 / W13 门控路由同一模式）：

- `GET /api/v1/vtuber/status`：开关关闭 → **404**（不是 409，避免向未启用方
  暴露能力存在性）；开启 → 200 + 只读投影，**不含 token 值**。
- 无写操作、无副作用端点、不注册工具、不进 `_PUBLIC_PREFIXES` 白名单
  （沿用 SEC H1 默认拒绝，需有效 token）。

### 1.4 OLV 配置模板 `config/olv.conf.yaml`

按文档 §7.3 生成的 overlay，键名与嵌套**逐条对照 OLV 上游 schema 校验**
（`src/open_llm_vtuber/config_manager/agent.py::AgentConfig` 与
`stateless_llm.py::OpenAICompatibleConfig`）：`llm_configs` 与 `agent_settings`
平级，`basic_memory_agent` 下只有 `llm_provider` / `faster_first_response` /
`segment_method` / `use_mcpp` / `mcp_enabled_servers` 五个合法键。

三项 LLM 指向 Veya 网关：

```yaml
character_config:
  agent_config:
    conversation_agent_choice: 'basic_memory_agent'
    agent_settings:
      basic_memory_agent:
        llm_provider: 'openai_compatible_llm'
        use_mcpp: False            # 不引入 OLV 的 MCP，避免第二套工具权威
        mcp_enabled_servers: []
    llm_configs:
      openai_compatible_llm:
        base_url: 'http://127.0.0.1:8791/v1'
        llm_api_key: '<issued token>'   # 由 veya_llm_gateway_tokens issue 签发
        model: 'veya1.2'
  asr_config:
    asr_model: 'sherpa_onnx_asr'   # CPU onnx，免密钥
  tts_config:
    tts_model: 'edge_tts'          # 免密钥；离线场景换 sherpa_onnx_tts
system_config:
  tool_prompts:
    live2d_expression_prompt: 'live2d_expression_prompt'   # 表情关键字依赖它
```

模板不含任何真实凭据，`llm_api_key` 只有占位；签发命令写进注释：

```
python -m scripts.veya_llm_gateway_tokens issue --name open-llm-vtuber --ttl-days 30
```

模板是纯 YAML 文件，Veya 运行时不会读取它，因此不构成代码路径。**未复制、
未引用任何 Live2D 模型文件**（只在上游 `model_dict.json` 的 `emotionMap` 里
记录表情键名）。

### 1.5 明确不做（与文档 §7.5 一致）

- 不桥接 OLV `/client-ws` 与 Veya `/api/v1/voice/ws`（JSON+base64 vs 二进制 PCM）。
- 不引入 OLV 的 `mcpp/` MCP 客户端，不把 `basic_memory_agent` 历史并入 Veya 记忆。
- 不 vendor `frontend/` 打包产物，不打包/修改任何 Live2D 模型资产。
- 不在 `pyproject.toml` / `uv.lock` 增加任何 OLV 依赖。

---

## 二、验收对照（文档 §7.4）

| 退出条件 | 状态 | 证据 |
|---|---|---|
| 开关关闭：`pytest` 相关测试全绿 | 通过 | §三 |
| 开关关闭：`/api/v1/vtuber/status` 不存在或返回 `enabled:false` | 通过（404） | 测试 `test_status_route_404_while_disabled` |
| 开关关闭：无新进程、无新端口 | 通过 | 模块无 spawn；测试断言无 `subprocess`/`Popen` 属性 |
| 开关关闭：工具面字节不变 | 通过 | `master_tools` schema JSON 的 sha256 与 `main` 完全一致，见 §3.4 |
| 开关关闭：路由面只多一条 | 通过 | 路由集合 diff：仅新增 `GET /api/v1/vtuber/status`，无删除 |
| 开关开启：OLV 页面能连上并得到可打断中文语音 | **未验证** | 需 OLV 独立进程 + Live2D 前端 + 麦克风，属部署期人工验收 |
| 网关侧观察 `/v1/chat/completions` 流式与 tools 降级 | **未验证** | 需真实 OLV 进程 |
| 断网/网关停机时 OLV 不崩 | **未验证** | 需真实 OLV 进程 |
| 资源记录（RSS / 首包延迟） | **未验证** | 需真实 OLV 进程 |

本轮把可自动化的部分全部自动化。需要真实 OLV 进程与麦克风的四条在报告里显式
标为未验证，不当成已过。

---

## 三、测试结果

执行环境：`python3 -m pytest`（Python 3.14.4，pytest 9.1.1），工作目录为隔离
worktree `/data/soffy/projects/veya-olv`。worktree 的 `platform/3O/*` 子模块
已 `git submodule update --init` 到 `main` 固定的 pin。

### 3.1 新测试 `tests/server/test_vtuber_bridge.py`（16 项）

覆盖：

- **开关**：默认 OFF；`0/false/no/off/空` 拼写仍 OFF；`1/true/yes/on` 才 ON；
  表项有 owner 与 removal_date。
- **关闭时零行为变化**：`status()` 逐字节等于 `{"enabled": False}`；
  `config_template()` 等于 `{"enabled": False}`；`master_tools` schema JSON
  字节不变；模块无 `subprocess`/`Popen`/`spawn`/`register` 属性。
- **开启时只读投影**：`gateway_url` / `chat_completions_url` / `models_url` /
  `health_url` 由 `VEYA_OLV_HOST` + `VEYA_OLV_GATEWAY_PORT` 组装；
  端口非数字或越界回退 8791。
- **模板内容**：LLM 指向 Veya 网关且 `base_url` 含 `/v1`；`use_mcpp` 为
  `False`；ASR/TTS 为 CPU/免密钥选择；`live2d_expression_prompt` 开启；
  表情键与 persona_prompt 一致；`token_issue_command` 含签发命令。
- **无密钥泄漏**：模块输出 JSON 不含 `vygw_` 前缀；`llm_api_key` 恒为占位符。
- **路由门控**：关闭 → 404；开启 → 200 且响应不含 token；匿名访问 401
  （未进公开白名单）；`/api/v1/vtuber` 下只有 `GET`，无写端点。
- **模板文件**：YAML 可解析；`llm_configs` 与 `agent_settings` 平级（对照上游
  schema）；`openai_compatible_llm` 键集合恰好是上游要求的 7 个；无 `vygw_`、
  无 `live2d-models/`、无 `.model3.json`。

```text
$ python3 -m pytest tests/server/test_vtuber_bridge.py -q
16 passed, 4 warnings in 19.82s
```

### 3.2 回归测试

```text
$ python3 -m pytest -q \
    tests/server/test_vtuber_bridge.py \
    tests/test_feature_flags.py \
    tests/test_config_authority.py \
    tests/server/test_server_boot_gate.py \
    tests/server/test_world_intel_mcp.py
71 passed, 4 warnings in 49.26s
```

`tests/server/` 全目录（含新测试）：

```text
141 passed, 1 failed, 4 warnings in 41.01s
```

唯一失败 `tests/server/test_wshobson_index.py::test_both_sources_enabled`
（`assert 185 > 200`）在 `main` 上同样失败，与本改动无关；单独跑该文件 4 项
全过（目录级运行时的顺序依赖，非本改动引入）。`tests/server/` 在 `main` 上
另有 2 条 `test_browser_run_backend.py` 失败（Playwright bundle 相关，本机
`deploy/ms-playwright/` 缺失），同样是基线状态。

### 3.3 Ruff

```text
$ ruff check server/vtuber_bridge.py server/routes/vtuber.py \
      server/feature_flags.py tests/server/test_vtuber_bridge.py
All checks passed!

$ ruff format --check server/vtuber_bridge.py server/routes/vtuber.py \
      server/feature_flags.py tests/server/test_vtuber_bridge.py
4 files already formatted
```

`server/app.py` 只新增 4 行（import + include_router + 注释），未触碰该文件
在 `main` 上既有的 2 处格式差异与既有 lint 记录（`ruff check server/` 在
`main` 与本分支同为 41 条既有错误，数量一致）。

### 3.4 关闭时工具面/路由面逐字节比对

同一进程内对 `main` 与分支分别序列化后比较：

```text
main   tools: 162  sha256: 645ab8c80de54124886d5f63ec1af14c2146dbfc5489ab847b54623baad63a3d
branch tools: 162  sha256: 645ab8c80de54124886d5f63ec1af14c2146dbfc5489ab847b54623baad63a3d
→ 完全一致，且 'vtuber' 不出现在工具 schema 中

routes: main 526 / branch 527
集合 diff: only in branch = ["['GET'] /api/v1/vtuber/status"], only in main = []
```

即：默认关闭时工具面逐字节不变，路由面只多出这一条受 flag 门控的只读端点。

---

## 四、风险与遗留（Phase 1 候选）

1. **tools 降级分支**：Veya 网关对 `tools` 字段的处理会在 OLV 首次连接时暴露，
   需真实进程观察（文档 §7.4 第三条）。
2. **`session_id` 映射**：OLV 每轮送完整历史，Veya 网关无状态转发，OLV 对话
   不进 Veya 记忆——Phase 1 议题。
3. **Live2D 资产许可**：模板只记录表情键名，未复制任何模型文件；对外分发前
   须替换模型（文档 §2 / §八）。
4. **凭据边界**：token 只存在于 OLV 侧 `conf.yaml`，Veya 侧不落盘、不回显、
   不经由任何端点返回。
5. **404 vs 409**：关闭时返回 404 而非 409，与既有 flag-gated 路由
   （NANO-W7 / W13）一致；若产品要求"可区分未启用"，可改 409 并在 report
   记录取舍。

---

## 五、提交

分支 `feat/olv-phase0`，工作树 `~/projects/veya-olv`。未 merge、未 push、
未部署、未重启任何服务。完成标记写入 `~/wim/all/olv.done`。

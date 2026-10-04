# HICODE_FAULT_DOMAIN — SF-HICODE P0.5 §9

> 决策来源：历史 provider 映射 + 本机 runtime 配置 + antigravity 现场绑定。
> 零源码改动。`fault_domain` 概念在本仓库**此前不存在**，本报告首次定义。

## 0. 结论

```text
fault_domain_id          = FD-GEMINI-UPSTREAM
shared_fault_domain      = true   (Hicode ↔ Antigravity)
selection_diversity_rule = 二者不得计为独立 failover candidate
D4 = DECIDED
```

## 1. `fault_domain` 在本仓库不存在

```text
grep -rn "fault_domain" --include=*.py --include=*.json veya/ server/ runtime/ config/
  → 无命中
```

因此这不是"沿用既有语义"，而是**首次引入**。任何声称沿用既有
fault-domain 语义的表述都是假的。

## 2. 两条绑定路径

### 2.1 显式历史映射（决定性证据）

`config/hicide_model_mapping.json`（提交于 `937184cd^` 之前，最后可用基线）：

```json
{
  "gemini-pro-agent": {
    "requested_model":    "gemini-pro-agent",
    "requested_provider": "cliproxy-google",
    "internal_provider":  "antigravity",
    "upstream_model":     "gemini-pro-default",
    "active": true
  }
}
```

**`internal_provider: antigravity`** 是显式声明：hicode 请求的
`gemini-pro-agent` 在内部**由 antigravity 承接**。这不是推测，是配置里写死的。

### 2.2 本机 runtime 配置（现场观察）

```toml
[[[providers]]]
name     = cliproxy-google
base_url = http://127.0.0.1:10100/v1
model    = gemini-pro-agent
```

### 2.3 Antigravity 当前绑定

```text
ExecutorRegistry.identity("antigravity")
  provider = gemini
  model    = gemini-2.5-pro
  capabilities = [supports_cancel, supports_file_effect, supports_idempotency,
                  supports_read_task, supports_receipts, supports_shell_effect,
                  supports_write_task]
  provider_capabilities = [stream, text, tool_use]
```

凭据文件全部 **NOT_FOUND**：

```text
~/.antigravity/config.json            NOT_FOUND
~/.gemini/credentials.json            NOT_FOUND
~/.config/antigravity/config.json     NOT_FOUND
```

## 3. 共享资源逐项判定（P0.5 §9 要求）

| 共享资源 | 是否共享 | 证据 |
|---|---|---|
| upstream model family | **是** | 均为 Gemini 系(gemini-pro-agent / gemini-2.5-pro) |
| internal provider | **是** | 映射显式 `internal_provider: antigravity` |
| network endpoint | **部分** | hicode → `127.0.0.1:10100`(bun.exe 存活)；antigravity 路由未验证 |
| credential | **未证实共享** | hicode 侧不持有；antigravity 侧无凭据文件 |
| provider quota | **推定共享** | 同一上游 model family |
| rate limit | **推定共享** | 同上 |
| authentication | **未证实共享** | 两侧证据都不足 |

**判定**：共享是**已证实**（internal provider + model family），
未证实项不影响结论方向 —— 证明独立需要正面证据，而当前正面证据为零。

```text
shared_fault_domain = true
```

依据强度：显式配置项 `internal_provider: antigravity`，
不是从命名或目录结构推断。

## 4. 为什么必须显式声明

P0.5 §9 指出的失效场景，在本仓库**已经发生过一次**：

```text
claude_code AUTH_FAILURE
    ↓ failover
Hicode(内部 antigravity → 同一 Gemini 上游)
    ↓ 若上游整体不可用
被统计为 "independent failover"
```

实际是**同一个故障域内的二次失败**。若不声明，
`Mission Failover` 的 receipt 会把一次上游故障计成两次独立事件，
receipt 的 `rejected_candidates` 理由也会误导后续判断。

## 5. selection diversity rule

```text
RULE FD-1
  Hicode 与 Antigravity 共享 FD-GEMINI-UPSTREAM。
  二者不得在同一 failover 链中被计为两个独立 candidate。

RULE FD-2
  selection 必须在剔除同 fault_domain 的已失败 candidate 后再选。
  若剔除后无可用 candidate,应报告 DOMAIN_EXHAUSTED,
  而不是继续在同域内尝试。

RULE FD-3
  receipt 必须记录 fault_domain_id。
  同一 fault_domain 内的多次失败不得累加为独立 failover 成功次数。

RULE FD-4
  shared_fault_domain 的 candidate 在 preferred_executor 解析中
  表达偏好,不表达唯一性 —— 与既有 preferred≠pin 语义一致。
```

## 6. `fault_domain_id` 取值

```text
FD-GEMINI-UPSTREAM   Hicode + Antigravity
FD-OPENCODE-GO       opencode 系(与 Gemini 域无共享)
FD-OPENAI-CHATGPT    codex
FD-ANTHROPIC         claude_code
FD-UNKNOWN           其余,默认视为独立但需标注未验证
```

**注意**：为其余 executor 赋 `fault_domain_id` 是本报告提出的**建议**，
当前无任何证据支持或反对。P0.5 不修改源码，故不落地；
若采纳需另立 implementation phase 并为每个域补证据。

## 7. 未验证项（§23 格式）

```text
UNVERIFIED  antigravity 的 provider "gemini" 实际路由到哪个 endpoint
UNVERIFIED  antigravity 是否也经由 127.0.0.1:10100(与 hicode 同 proxy)
UNVERIFIED  antigravity 的凭据从何处获取(无本地凭据文件)
UNVERIFIED  gemini-pro-agent 与 gemini-2.5-pro 是否共享配额桶
BLOCKED     runtime 不存在,无法用真实请求验证同域故障行为
```

第二条与第四条是本决策最需要的补充证据：前者决定 `network endpoint`
共享是否成立，后者决定"同域二次失败"是否真的会在真实链路上发生。
两者都要求 runtime 存在。
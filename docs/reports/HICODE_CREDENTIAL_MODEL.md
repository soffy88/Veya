# HICODE_CREDENTIAL_MODEL — SF-HICODE P0.5 §6–§8

> 决策来源：历史代码考古 + 本机 runtime 配置只读观察 + SF-CRED 既有裁决。
> 零源码改动。

## 0. 结论

```text
CREDENTIAL_TRUTH_CONTRACT = Model B  (Runtime-owned credential)

D2 = DECIDED
D3 = DECIDED
```

并附一条硬约束：**Hicode 不得复用 SF-CRED 的
`credential_present` / `credential_valid` 语义作为其认证真值。**
它属于 runtime-owned 模型，不是 registry-observable 模型。

## 1. 六个字段的定义与归属

| 字段 | Hicode 的取值来源 | 状态 |
|---|---|---|
| `credential_owner` | **runtime**，按 provider 分别声明 | OBSERVED |
| `credential_source` | `api_key_env` 环境变量名 / 本地 proxy | OBSERVED |
| `credential_present` | **NOT_APPLICABLE** —— 无 veya 侧凭据 | DECIDED |
| `credential_valid` | runtime 认证证据，**非文件系统证据** | DECIDED |
| `credential_fresh` | **UNVERIFIED** —— 需 runtime 暴露 | BLOCKED_BY_RUNTIME |
| `credential_transport` | runtime → proxy → provider | OBSERVED |

## 2. Q1–Q6 逐项回答

### Q1 credential 到底由谁持有？

**由 runtime 持有，且按 provider 分裂。** 证据来自
`/home/soffy/.veya/hicode-runtime/reasonix-home/.reasonix/config.toml`：

```toml
[[[providers]]]
name        = cliproxy-google
base_url    = http://127.0.0.1:10100/v1
# 无 api_key_env —— runtime 侧不持有任何凭据

[[[providers]]]
name        = opencode-go
base_url    = https://opencode.ai/zen/go/v1
api_key_env = <REDACTED len=16>   # 环境变量名，非密钥值
```

即 runtime 配置里**没有任何密钥**，只有**指向凭据的引用**。

### Q2 Hicode runtime 是否自行获取？

**分情况，且默认不自行获取。**

- `cliproxy-google`：**不获取**。无 `api_key_env`，runtime 不发 Authorization 头。
  历史代码注释明确写过这一设计意图：

  ```python
  # 不注入任何 api_key_env 占位值, Hicode 将不发 Authorization 头。
  # 若将来 provider 配了真实 api_key_env, 环境变量自然透传。
  ```

- `opencode-go`：**不获取，只透传**。`api_key_env` 指向一个环境变量名，
  凭据由进程环境提供，runtime 只读不存。

⇒ **历史上没有任何"runtime 自行 OAuth 登录获取凭据"的证据。**

### Q3 proxy 是否负责 authentication？

**对 `cliproxy-google` 是。** base_url 是 `http://127.0.0.1:10100/v1`，
一个本地 OpenAI-compatible 端点，当前由 `bun.exe` (pid 2966) 监听。
认证发生在 proxy 侧，veya 与 runtime 都不参与。

### Q4 ExecutorRegistry 能否观察 credential validity？

**不能。** 三重障碍：

1. runtime 不向 registry 暴露凭据状态，只有 identity 的
   `provider` / `model` 两个字符串；
2. `credential_present` 在 veya 侧无源可查 —— 旧实现刻意不注入 api_key_env；
3. runtime 目前**不存在**，无任何可调用面。

⇒ registry 能给出的最强陈述是 `credential_valid = None`（未探测），
且这个 `None` 的含义是"**不可观测**"，而非"尚未探测"。

### Q5 provider probe 是否必须经过 Hicode runtime？

**对 `cliproxy-google` 必须。** 该 provider 无独立凭据，绕过 runtime 的
probe 无法区分"凭据无效"与"runtime 未透传"。

**对 `opencode-go` 不必须。** 它是标准 env-var provider，SF-CRED 的
provider-native probe 机制原则上适用。

### Q6 Hicode 的 authenticated / valid 状态由谁证明？

**由 runtime 通过一次真实完成的请求证明。** 没有任何本地证据可以替代：

```text
可接受的证明:  runtime → proxy → provider  一次最小真实请求成功
不可接受的证明: provider/model 映射存在      ← 历史做法,已被 P0 判定为假阳性
不可接受的证明: 凭据文件存在                ← SF-CRED P0 已判定为假阳性
不可接受的证明: 未探测即视为已认证          ← SF-CRED §20 G2 明令禁止
```

## 3. 采用 Model B 的理由

P0.5 §7 给出三个候选。逐一评估：

```text
Model A  Registry-observable credential
        需要 registry 能做 provider-native probe 并获得真实证据。
        对 cliproxy-google 不成立(Q5)—— 无独立凭据。
        → REJECTED

Model B  Runtime-owned credential
        credential_valid = runtime authentication evidence
        与观测到的所有权结构一致(Q1/Q2/Q3),且不需要 registry 读取
        runtime 私有配置文件。
        → ADOPTED

Model C  Hybrid
        适用于"两个 provider 各有一套"的情形。
        实际观测: opencode-go 侧确与 Model A 同构,
        cliproxy-google 侧必须 Model B。
        → PARTIAL: 见 §4 的分层裁决
```

## 4. 分层裁决（Model B 为主，Model A 仅限可观测子集）

```text
Layer 1  registry / structural
         credential_present = NOT_APPLICABLE
         credential_valid   = None(永久,除非有 runtime 证据)
         理由: veya 侧无凭据源,任何本地推断都是编造

Layer 2  runtime authentication probe
         credential_valid = True  仅当 runtime 完成一次真实请求
         credential_valid = False 仅当 runtime 返回认证类失败
         未探测             = None
         理由: 唯一能证明的层

Layer 3  freshness
         credential_fresh = UNVERIFIED
         runtime 未暴露 expires/refresh 语义
         → BLOCKED_BY_RUNTIME
```

## 5. §8 D3 —— 与 SF-CRED 的兼容性

P0.5 §8 要求：Hicode 不得重新创建第三种
`credential-present-but-not-valid` 假阳性。

**历史事实**：Hicode 恰好就是第三种，而且比前两种更宽松：

| # | 假阳性类型 | 机制 | SF-CRED 状态 |
|---|---|---|---|
| 1 | 文件存在即已认证 | `claude_code` / `codex` | 已修(P2a 结构校验) |
| 2 | 缺省即无效 | `antigravity` | 已修(真实验收纠正) |
| 3 | **有 provider/model 映射即已认证** | **hicode** | **本次裁决** |

```python
# 937184cd^:veya/remote/executor_registry.py
elif executor_id == "hicode":
    provider, model, source = _hicode_config()
    auth = bool(provider and model)     # ← 与凭据毫无关系
```

### 最终 candidate eligibility 条件

P0.5 §8 要求 `credential truth + health truth + capability truth` 同时满足。
对 Hicode 具体化为：

```text
credential truth   Layer 2 的真实 runtime 认证证据(非映射存在性)
health truth       runtime health probe 通过(非 launcher 存在性)
capability truth   显式声明,UNKNOWN 不等于 READ
```

三者在 runtime 不存在时**全部无法满足**。这与 §12 的 R0 结论一致，
并构成 §27 STOP CONDITION 4(`credential validity 无法获得真实证据`)。

## 6. 禁止路径（§7 Forbidden 逐条确认）

```text
provider && model  → authenticated=True          禁止,且为历史实际做法
credential file exists → authenticated=True      禁止,且为 SF-CRED P0 实测假阳性
not probed → authenticated=True                  禁止,SF-CRED §20 G2
```

三条均写入 `HICODE_REACTIVATION_P0_5_DECISION.md` 的 D2/D3 裁决正文,
并落入 `DECISIONS.json` 的 `forbidden_paths` 字段。

## 7. 未验证项（§23 格式）

```text
UNVERIFIED  runtime 是否在任何位置暴露 expires_at / refresh_token 语义
UNVERIFIED  cliproxy-google (127.0.0.1:10100) 是否要求认证,还是纯本地可信端点
UNVERIFIED  opencode-go 的 api_key_env 指向的环境变量在 hicode 运行时是否被注入
BLOCKED     runtime 不存在,无法执行 Layer 2 探测
```

第三条尤其重要：若 hicode 进程环境未注入该变量，`opencode-go` provider
在 runtime 内同样不可用，而这**只有真实 runtime 探测能发现**。
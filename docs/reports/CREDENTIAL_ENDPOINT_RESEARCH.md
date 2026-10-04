# CREDENTIAL_ENDPOINT_RESEARCH — SF-CRED P2 前置调研

> 只读调研,**未修改任何源码**。基线 `c6bee266`(P1)
> 目的:为 P2 的真实探测确定每个 provider 最廉价的「已认证但不生成 token」的请求。

## 1. 结论先行

调研的结论**改变了 P2 的形态**。原 Spec 假设「每个 provider 一条最小真实探测」,
但结构检查显示:

```text
4 个声称已认证的 executor
  ├─ 2 个的凭据材料字面上是空的   → 零成本结构检查即可判无效
  └─ 2 个材料非空
       ├─ 1 个真实成功
       └─ 1 个真实 PROVIDER_UNAVAILABLE → 只有网络探测能判
```

因此 P2 应分层:**P2a 结构性校验(零网络成本)** → **P2b 真实探测(有网络成本)**。
先做 P2a 的理由不是便宜,而是它把两个假阳性在**不发起任何网络请求**的前提下消除。

## 2. 凭据材料实况(只列结构与空/非空,不含任何值)

| executor | env | 文件存在 | 材料内容 | registry 判定 | 真实调用 |
|---|---|---|---|---|---|
| `opencode` | 未设 | 是 | `type="api"`,`key` 67 字符 **非空** | authenticated=True | **成功** |
| `codex` | 未设 | 是 | `auth_mode="chatgpt"`,`tokens.access_token` **非空**,`OPENAI_API_KEY=null` | authenticated=True | **PROVIDER_UNAVAILABLE** |
| `claude_code` | 未设 | 是 | `accessToken=""`, `refreshToken=""`, `expiresAt=0` —— **全空** | authenticated=True | **AUTH_FAILURE** |
| `pi` | 未设 | 是 | `{}` —— **完全空** | authenticated=True | **PROVIDER_CONFIGURATION_FAILURE** |

全部 9 个相关 env 变量(`PI_API_KEY` / `ANTHROPIC_API_KEY` /
`ANTHROPIC_AUTH_TOKEN` / `OPENCODE_API_KEY` / `CODEX_API_KEY` / `OPENAI_API_KEY` /
`XAI_API_KEY` / `GEMINI_API_KEY` / `ANTIGRAVITY_API_KEY`)**均未设置**。

⇒ `_credential_present` 在这四个上全部靠**文件存在**返回 True,而其中两个文件
**没有任何凭据材料**。文件存在与凭据存在被当成了同一件事。

## 3. 为什么 claude_code 与 pi 无需网络探测即可判否

```text
claude_code  accessToken=""  refreshToken=""  expiresAt=0
pi           auth.json = {}
```

这两种情况对「凭据是否可用」是**确定性的否**:没有 token,不可能通过认证。
一个读取这些字段并检查非空的检查,零网络成本即可判定。

对比 `claude_code` 的真实调用耗时 **217.7s** —— 那次探测本可以完全避免。

## 4. 过期维度已可得

```text
claude_code.expiresAt        = 0        (0 表示无有效会话)
codex.last_refresh           = 2026-09-25T23:37:25Z
opencode-go                  = 静态 API key,无过期字段
```

即 credential 的**新鲜度**在部分 provider 上可从文件直接读出,
不需要网络。这支持原 Spec 的三分法,并给出了具体落点:

```text
credential_present  文件/env 在不在                     已有
credential_fresh    过期字段是否已过 / token 是否非空     可从文件读,零成本
credential_valid    真实调用是否成功                     需网络
```

## 5. 端点调研结果(为什么 P2b 不能靠猜)

| executor | contract endpoint | 可用于非生成式探测? |
|---|---|---|
| `opencode` | `https://opencode.ai/zen/v1` | **可能** —— `/v1/models` 已在 `veya/decision/policy.py:19` 与 `veya/obase/llm.py:230` 中被使用,是既有可复用模式 |
| `codex` | 未声明 endpoint(provider=openai) | **不可** —— 需自行确定 base_url 与 chatgpt OAuth 的探测方式 |
| `claude_code` | 未声明 endpoint(provider=anthropic) | **不可** —— 同上 |
| `pi` | 未声明 endpoint(provider=anthropic) | **不可** —— 同上 |

代码库中**没有**通用的非生成式探测端点;唯一的现成先例是 opencode 的
`/v1/models` 用法。

因此 P2b 若要覆盖 codex / claude_code / pi,必须先逐个确定其 base_url 与
OAuth token 的探测方式。**猜错的后果是把能用的 executor 判为 INVALID**,
比现状更糟。这正是「先调研」而非「直接实现」的价值。

## 6. 建议的分层实施

```text
P2a  结构性校验(零网络)
     ├─ token 字段非空
     ├─ 过期字段未过期(若存在)
     └─ auth.json 非空 dict
     预期效果:claude_code / pi 由 True 翻为 False,零网络请求

P2b  真实探测(有网络,懒加载 + TTL)
     ├─ 只探测 P2a 通过且被请求选择的 executor
     ├─ opencode 先落地(/v1/models 已有先例)
     └─ codex / claude_code / pi 待各自 base_url 与 OAuth 方式确认后再做
```

P2a 落地后需要重新评估:
**G2(未探测不得呈现为已认证)能否在 P2a 后就达成?**

我的判断是**可以**,但需要一条明确规则:

```text
结构上确定无凭据材料 → credential_valid = False   (已判定)
结构上有材料但未探测   → credential_valid = None   (未判定)
结构上有材料且探测成功 → credential_valid = True
```

`None` 仍然存在,所以严格的 G2(任何未探测都不得已认证)仍需 P2b 全部覆盖。
但 P2a 已经消除了本次观测到的两个假阳性,并让 `credential_valid=None`
的含义从「全是假的」变成「只剩真正未探测的」。

## 7. 未验证项

* `codex` 的 `PROVIDER_UNAVAILABLE` 是 token 过期还是 provider 侧不可达 —— 两者需要不同归因
* `claude_code` 是否可通过 refresh 恢复(其 `refreshToken` 也为空,当前无法刷新)
* `opencode` 的 `/v1/models` 是否真的能区分 401 与 200 —— 未实测
* `pi` 的 `PROVIDER_CONFIGURATION_FAILURE` 具体缺哪项配置(`auth.json` 为空是症状之一)
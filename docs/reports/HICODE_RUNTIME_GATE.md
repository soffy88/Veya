# HICODE_RUNTIME_GATE — SF-HICODE P0.5 §12–§17

> 只读发现。零源码改动，零安装，未尝试任何下载。
> 门状态定义来自 P0.5 §14。

## 0. 结论

```text
RUNTIME_GATE_STATE = R0  (NOT_FOUND)
G7 = BLOCKED
P1  = BLOCKED
```

P0.5 §14 规定 P1 前置为 `R4`。实测 `R0`。

## 1. 对继承断言的实测更正（重要）

P0 spec §1 与 P0 审计报告均记载：

```text
HICODE_MANAGED_PYTHON (宿主)   /home/soffy/.veya/hicode-runtime   不存在
```

**实测该目录存在。** P0.5 §23 禁止"应该存在 / 理论上可用"式断言，
因此该继承断言在此更正：

```text
OBSERVED: /home/soffy/.veya/hicode-runtime  存在(Sep 27 06:51)
```

但存在不等于可用。该目录**不含任何 runtime 产物**：

```text
/home/soffy/.veya/hicode-runtime/
├── state/                              空目录
└── reasonix-home/.reasonix/config.toml  唯一文件(2.4K)
```

无二进制、无 managed python、无 manifest。

**更正后的准确表述**：runtime 目录存在但**不含 runtime**。
R0 指 runtime 本身 NOT_FOUND，而非其父目录不存在。

## 2. 发现矩阵（§13 指定位置全覆盖）

| 位置 | 结果 | 证据 |
|---|---|---|
| `/home/soffy/.veya/hicode-runtime` | **EXISTS**，仅 config.toml + 空 state | `ls -laR`，`du -sh` = 20K |
| `/home/soffy/.veya/hicode-runtime-managed` | **EXISTS 但完全为空** | `find` 无任何条目，`du -sh` = 4.0K |
| `/home/soffy/.veya/reasonix-workspace` | 存在，但是**用户 git workspace**(非 runtime) | 含 `hello.py` / `host_test.py` / `monitor_server.py` / `.git`，716K |
| `/home/soffy/.cache/reasonix` | 存在，仅 environment probe 缓存(12K) | `probes-865a87c907f40e47.json` |
| `PATH` 上的 `hicode` / `reasonix` | **NOT_ON_PATH** | `command -v` 两者均无 |
| `/data/soffy/projects/*hicode*` `*reasonix*` | **NOT_FOUND** | glob 无匹配 |
| systemd user units | **无** hicode/reasonix unit | `systemctl --user list-units --all` 无匹配 |
| 容器文件系统 | 未探测 | 本阶段无运行中的 hicode 容器；`docker` 存在但无相关镜像证据 |

### 环境 probe 缓存内容

`/home/soffy/.cache/reasonix/environment/probes-865a87c907f40e47.json`
(stored_at `2026-09-21T07:27:09Z`) 探测了 11 个二进制：

```text
FOUND     docker, git, make, node, npm, python, python3, rg
NOT_FOUND cargo (timeout), go (not found), rustc (timeout)
```

**`reasonix` 从未出现在探测列表中** —— 该缓存是通用环境探测，不是 runtime 探测。

## 3. §15 Manifest Requirements 逐项

历史代码要求(`hicode_runtime.resolve_binary()`)：

```python
configured = os.environ.get("HICODE_MANAGED_PYTHON", "").strip()
if not all(reasonix_info.get(k) for k in ("version", "binary", "commit")):
    ...  # 不匹配 canonical managed manifest 即拒绝
raise HICODE_RUNTIME_INCOMPATIBLE: "Reasonix is not the canonical managed binary"
```

| 必需字段 | 状态 |
|---|---|
| `runtime_id` | **NOT_FOUND** |
| `runtime_version` | **NOT_FOUND** |
| `binary_path` | **NOT_FOUND** |
| `manifest_path` | **NOT_FOUND** |
| `supported_protocol` | **NOT_FOUND** |
| `proxy configuration` | 部分可从 config.toml 推断(见 §4)，但 manifest 缺失 |
| `provider binding` | 部分可从 config.toml 观察(见 §4) |
| `credential ownership` | **UNVERIFIED**(见 CREDENTIAL_MODEL 报告) |
| `health endpoint/probe` | **NOT_FOUND** |

manifest 搜索覆盖 `/home/soffy/.veya` 与 `/home/soffy/.cache/reasonix`(depth 4)，
命中的 `manifest.json` 全部无关：

```text
.veya/skills/spec-pack/manifest.json          技能包
.veya/skills/ecc_doc_updater/manifest.json    技能包
.veya/vision-artifacts/smoke-session/ocr_*/manifest.json   OCR 工件
.veya/.veya/runs/cap-probe-draft/artifact_manifest.json   探针草稿
```

按 §15：manifest 缺失即 `R1`，**不得自行推断**。
本例更进一步 —— 连 runtime 二进制都不存在，故为 `R0` 而非 `R1`。

## 4. §12 runtime gate 逐项核对

| 要求 | 状态 |
|---|---|
| runtime executable exists | **NOT_FOUND** |
| runtime version identifiable | **NOT_FOUND** |
| manifest exists | **NOT_FOUND** |
| manifest matches runtime | **NOT_FOUND**（无从比对） |
| runtime can launch | **NOT_TESTABLE**（无可执行体） |
| runtime can report health | **NOT_TESTABLE** |
| runtime can reach required proxy/provider | **NOT_TESTABLE** |

可执行文件搜索：`find /home/soffy/.cache/reasonix /home/soffy/.veya -maxdepth 4 -type f -executable`
唯一命中 `/home/soffy/.veya/bin/vtracer` —— 视觉追踪工具，与 reasonix 无关。

## 5. §16–§17 探测未执行的理由

§16 要求 runtime health probe 至少证明「进程可启动 / runtime 响应 /
可初始化 / 可抵达 proxy-provider 边界」，且**不得用 `binary --version` 单独充数**。

§17 要求 runtime → proxy → provider 至少一次最小真实请求，**NO MOCK / NO STUB /
NO SYNTHETIC SUCCESS**。

**两者均未执行**，因为不存在可启动的 runtime。伪造一次探测会直接违反
§2 约束 16 与 §23。

## 6. 唯一存活的历史 runtime 配置（只读观察）

`/home/soffy/.veya/hicode-runtime/reasonix-home/.reasonix/config.toml` 是本机
唯一残留的 runtime 侧配置。键值已脱敏：

```toml
[[agent]]
  reasoning_language = auto

  [[[providers]]]
  name        = cliproxy-google
  kind        = openai
  base_url    = http://127.0.0.1:10100/v1
  model       = gemini-pro-agent
  context_window = 1000000
  # 无 api_key_env → 认证委派给本地 proxy

  [[[providers]]]
  name        = opencode-go
  kind        = openai
  base_url    = https://opencode.ai/zen/go/go/v1   # 实际值 .../zen/go/v1
  model       = deepseek-v4-flash
  api_key_env = <REDACTED len=16>                  # 环境变量名，非密钥值
  context_window = 1000000

  [[environment]]
  enabled = true
```

**这份配置是 CREDENTIAL_MODEL 报告的主要证据**：runtime 侧的 credential
所有权按 provider 分别声明，一个显式引用环境变量名，一个完全不持有凭据。

## 7. proxy 端口现场状态

```text
127.0.0.1:10100   LISTEN   users:(("bun.exe",pid=2966))     ← config.toml 的 cliproxy base_url，当前活着
0.0.0.0:10101     LISTEN   users:(("python3",pid=2982))
10103             无监听                                     ← 历史 HICODE_PROXY_PORT
```

历史配置(来自 `937184cd^:server/hicode_runtime.py`)：

```text
HICODE_PROXY_PORT           = 10103                          当前无监听
HICODE_PROXY_UPSTREAM       = http://192.168.16.1:10101
HICODE_PROXY_UPSTREAM_HOST  = 127.0.0.1:10100
```

即 hicode 当年经**自身 proxy(10103)** → 宿主桥(192.168.16.1:10101) →
本地 cliproxy(10100) 取模型。10100 今天仍在监听(bun.exe)，但 **10103 不在**：
hicode 自己的 proxy 进程也不存在。

## 8. 门状态判定

```text
runtime 二进制            NOT_FOUND
managed python             NOT_FOUND
manifest                   NOT_FOUND
runtime health 能力        NOT_TESTABLE
provider 通路              仅 10100 存活,非 hicode 自有 proxy

RUNTIME_GATE_STATE = R0 (NOT_FOUND)
```

## 9. STOP

命中 P0.5 §27 STOP 条件：

```text
STOP CONDITION 1 — L2 runtime 不存在          成立
STOP CONDITION 2 — runtime manifest 不存在    成立
```

```text
P0.5 BLOCKED (runtime gate)
Reason:     L2 managed Reasonix runtime 与 manifest 均不存在,R0
Evidence:    本报告 §1–§4
Required external prerequisite:
            一个可执行的 managed Reasonix runtime + 其 canonical manifest,
            满足 §15 九项字段,置于 HICODE_MANAGED_PYTHON 指向的位置
```

STOP 不是 FAIL。架构裁决可在无 runtime 的前提下完成的部分继续推进
(见 `HICODE_REACTIVATION_P0_5_DECISION.md`)，但 `G7` 与 `P1` 不得放行。
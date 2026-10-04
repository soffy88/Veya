# CREDENTIAL_TRUTHFULNESS_INVENTORY — SF-CRED P0

> 只读盘点,**未修改任何源码**。
> 基线:`ae0a77a9`(SF-CRED / SF-HICODE 两份 Spec)
> 日期:2026-10-04

## 1. 结论

```text
声称已认证的 executor:      4  (pi, codex, claude_code, opencode)
其中真实调用成功:           1  (opencode)
其中真实调用失败:           3  (pi, codex, claude_code)
假阳性率:                   3/4
```

`ExecutorRuntimeIdentity.authenticated` 目前是**凭据文件存在性**的投影,
不是凭据有效性的投影。三个 executor 在 registry 快照里读起来 READY 且已认证,
被 selection 选中、被 admission 准入,然后在真实调用时失败。

## 2. 三元组现状

`credential_present` 与 `authenticated` 取自 registry 实际传给
`_credential_present` 的参数(通过在 `_discover` 上挂观测取得,不是读 contract —
registry 对部分 executor 使用的输入与 `executor_contract().auth_env` **不同**)。

| executor | env(实际) | file(实际) | present | authenticated | status | 真实调用 |
|---|---|---|---|---|---|---|
| `pi` | PI_API_KEY, ANTHROPIC_API_KEY | `auth.json` | True | **True** | READY | **FAILED** `PROVIDER_CONFIGURATION_FAILURE` |
| `codex` | CODEX_API_KEY, OPENAI_API_KEY | `auth.json` | True | **True** | READY | **FAILED** `PROVIDER_UNAVAILABLE` |
| `claude_code` | ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN | `.credentials.json` | True | **True** | READY | **FAILED** `AUTH_FAILURE` |
| `opencode` | OPENCODE_API_KEY | `auth.json` | True | **True** | READY | **OK** READ 任务 COMPLETED |
| `antigravity` | GEMINI_API_KEY, ANTIGRAVITY_API_KEY | — | False | False | DEGRADED | 未测(无凭据) |
| `dsh` | — | — | False | False | DEGRADED | 未测 |
| `grok` | XAI_API_KEY | — | False | False | UNAVAILABLE | 未测 |
| `acp` | — | — | False | False | UNAVAILABLE | 未测 |

`credential_valid` 一列在当前代码中**不存在** —— 这正是缺陷本身。

## 3. 根因

`veya/remote/executor_registry.py`:

```python
def _credential_present(names: list[str], files: list[Path]) -> bool:
    return any(bool(os.environ.get(name)) for name in names) or any(
        path.is_file() for path in files
    )
```

调用点把它的返回值直接当作认证事实:

```python
auth = _credential_present([...], [...])
authenticated = bool(auth)          # executor_registry.py::_discover
...
auth_state="AUTHENTICATED" if authenticated else "MISSING"
status = "READY" if reachable and authenticated else ...
```

函数名与它回答的问题一致(present),**使用方式与它能回答的问题不一致**(valid)。

## 4. 污染面

`authenticated` 参与以下判定,故污染不止一处:

| 消费点 | 位置 | 后果 |
|---|---|---|
| `ExecutorCandidate.qualified` | `veya/supervision/runner.py` | 不可达/未认证才算不合格 |
| `ExecutorCandidate.eligible` | 同上 | 死凭据 → eligible True |
| `ExecutorRuntimeIdentity.selectable` | `executor_registry.py` | 可被选中 |
| Mission failover 候选过滤 | `_rejection_reason` → `UNAUTHENTICATED` | 不会被 failover 排除 |
| health 状态机 | `status = READY if reachable and authenticated` | 报 READY |

最后一行解释了 P4 preflight 的现象:`pi` 报 `READY`,
真实调用却是 `PROVIDER_CONFIGURATION_FAILURE`。

## 5. 一个额外发现:registry 的凭据输入与 contract 不一致

观测显示 registry 实际使用的输入与 `executor_contract().auth_env` 不同:

```text
pi         contract auth_env = ['ANTHROPIC_API_KEY']
           registry 实际使用 = ['PI_API_KEY', 'ANTHROPIC_API_KEY'] + auth.json 文件
```

`_discover` 对 `pi` 有专门分支(env 名与文件列表都不同)。这意味着
**按 contract 推断凭据状态会得出错误结论** —— 我第一版盘点就是这么错的,
改为观测 registry 实际行为才拿到正确输入。

本 Spec 因此把「声明的凭据要求」与「registry 实际检查的凭据」分开记录,
避免下游再次按 contract 推断。

## 6. 探测条件(可复现)

```text
仓库:    /tmp/opencode/p4/repo(真实 git repo,首次 commit)
路径:    veya.remote.tool_adapter.RemoteToolAdapter.call → worker.dispatch
任务:    READ —— opencode 声明 supports_write_task=False,
         claude_code/codex/pi 需 WRITE 但该任务已足以暴露认证失败
超时:    单次 900s 上限,实测均在 13–220s 内返回
```

三个失败均在**真实调用**时暴露,不是探测推测:

```text
pi          PROVIDER_CONFIGURATION_FAILURE   12.7s
codex       PROVIDER_UNAVAILABLE             20.6s
claude_code AUTH_FAILURE                   217.7s
```

注意 `claude_code` 耗时 217.7s 才失败 —— 说明它在认证失败前做了大量工作。
这对 P1 的探测成本设计有直接影响(见下)。

## 7. 对 Spec 的修正

原 Spec §3.2 假设「每个 provider 一条最小探测」成本可控。实测表明:

```text
opencode     32.1s   成功
pi           12.7s   配置失败
codex        20.6s   provider 不可用
claude_code 217.7s   认证失败(耗时长)
```

因此 P2 必须:

1. **懒探测** —— 只探测被请求选择的 executor,不横扫;
2. **TTL 缓存** —— TTL 必须显著大于单次探测成本(claude_code 量级),
   否则探测本身成为负担;
3. **失败分类** —— `AUTH_FAILURE` / `PROVIDER_UNAVAILABLE` /
   `PROVIDER_CONFIGURATION_FAILURE` 三类语义不同,应分别归因
   (认证问题 vs provider 不可达 vs 配置缺失),不应合并为「凭据无效」。

第 3 点是对原 Spec §3 的补充:credential 无效本身还有子类。

## 8. 未验证项

* `antigravity` / `dsh` / `grok` / `acp` 的真实行为(无凭据,未探测)
* 凭据**过期**(文件存在但 token 过期)与凭据**无效**(token 被拒)是否可区分
* `pi` 的 `PROVIDER_CONFIGURATION_FAILURE` 具体缺失哪项配置
* 探测在并发下的成本(多个 executor 同时被选)

## 9. P1 的入口条件

已具备:

```text
✓ 根因定位到单一函数 _credential_present
✓ 污染面已枚举(5 个消费点)
✓ 真实调用证据齐备(4 个 executor)
✓ registry 实际凭据输入已观测,不靠 contract 推断
```

P1 可以开始:引入 `credential_present` / `credential_valid`,
并让未探测状态不呈现为已认证。
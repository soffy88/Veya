# LOCAL2-R2 — “不要动不动就拒绝” 审计与整改报告

分支 `fix/local2-less-deny`（基于 `fix/local2-usability` 2e802bc3）。
目标：用户自己的机器、用户自己授权的 ChatGPT。默认放行只读/普通读写/常见开发命令；
危险动作改为**审批**；只保留灾难性**硬拒**；拒绝时给清晰原因与可申请审批的路径。

> 生产服务 `veya-remote-mcp` 跑在 `/data/soffy/projects/veya-local2-fix`，本次**未改动**该目录，
> 也未重启任何服务。`veya/remote/{permission_engine,action_gateway,tool_adapter,policy_resolver}.py`
> 在 r2、`veya-local2-fix`、生产 `veya` 三处**逐字节相同**，故探针结论对生产同样成立。

---

## 1. 拒绝来源统计

数据源：`~/.veya/remote_audit.jsonl`（57k+ 行事件账本）。
`journalctl --user -u veya-remote-mcp --since "7 days ago" | grep -iE "deny|…"` 命中 **0** 行——
拒绝**不写 journal**，只落审计账本；故统计以账本为准。

### 1.1 全量状态分布（账本累计）

| 次数 | status | 说明 |
|---:|---|---|
| 4148 | auth_denied | 令牌/会话鉴权（另一问题域，非命令策略） |
| 618 | WORKSPACE_DENIED | 工作区/路径/身份不符 |
| 473 | EXECUTION_FAILED | 执行失败（非拒绝） |
| 208 | TOOL_DENIED | 工具名未注册 / 归属校验 |
| 208 | POLICY_BLOCKED | 策略硬拒 |
| 205 | APPROVAL_REQUIRED | 要求审批 |
| 40 | INVALID_APPROVAL | 布尔 approved / 审批单无效 |

### 1.2 前 20 类拒绝（按原因归类，样例命令）

| 次数 | 类别 | 样例 |
|---:|---|---|
| 358 | `process.status` 工作区身份不匹配 → WORKSPACE_DENIED | 轮询 `execution_id`（**只读**） |
| 119 | `sudo` 探针 → APPROVAL_REQUIRED | `sudo -n true` |
| 118 | 未知工具探针 → TOOL_DENIED | `definitely.not.a.tool`（自检探针） |
| 117 | 写 `/etc` 探针 → POLICY_BLOCKED | `file.write /etc/mcp_phase3_probe.txt`（自检探针） |
| 96 | 工作区身份不符 → WORKSPACE_DENIED | `workspace=/data/soffy/projects/hevi` |
| 90 | 只读/复合 shell → WORKSPACE_DENIED | `sh -lc 'grep -R …'` |
| 49 | `process.cancel` 授权 → TOOL_DENIED | 取消他人/其他会话的 execution |
| 49 | 网络探测 → POLICY_BLOCKED | `curl -s -o /dev/null -w %{http_code} http://127.0.0.1:18791/` |
| 40 | git 只读复合 → APPROVAL_REQUIRED | `git rev-parse --abbrev-ref HEAD && git rev-parse HEAD` |
| 31 | git 复合 → WORKSPACE_DENIED | `cd … && pwd && git rev-parse --show-toplevel` |
| 26 | 只读复合 → APPROVAL_REQUIRED | `ss -ltn \| grep -E ':(7890\|8790) ' \|\| true` |
| 19 | `hicode.execute`（执行器已退役）→ TOOL_DENIED | `hicode.execute` |
| 17 | 凭据/主机读探针 → WORKSPACE_DENIED | `../../../etc/passwd` |
| 14 | `systemctl` 读 → POLICY_BLOCKED | `systemctl --user show -p FragmentPath …` |
| 12 | `rm`（破坏性）→ POLICY_BLOCKED | `rm -f /tmp/…` |
| 11 | `systemctl` 读复合 → APPROVAL_REQUIRED | `systemctl --user list-units \| grep -E 'veya'` |
| 10 | 布尔 approved 探针 → INVALID_APPROVAL | `printf x > /etc/veya-probe.conf` |
| 10 | 只读复合 + 布尔 approved → INVALID_APPROVAL | `printf …` |
| 9 | `systemctl --user status` → INVALID_APPROVAL | `systemctl --user status veya-remote-mcp.service` |
| 8 | `docker ps`（只读）→ APPROVAL_REQUIRED | `docker ps --format …` |

### 1.3 三个系统性误拒根因（均已修复）

1. **工作区层把「工作区外」一律 DENY**（`policy_resolver.WorkspacePolicy`）。
   于是 `printf '…'; cat ~/.config/systemd/user/…conf`、`mkdir -p /tmp/…`、`touch ~/foo`
   这类**读/写用户自己家目录或 /tmp** 的普通操作被判 `DENY_SCOPE_ESCAPE`；
   复合命令只要**有一段**写到工作区外，整条就被拒（即使其余段全是只读）。
2. **`process.status` 的 workspace 身份二次校验**（`execution.py:status`）。
   令牌归属已证明调用者拥有该 execution，仍因 active workspace ≠ execution workspace 而
   `WORKSPACE_DENIED`——**358 次只读轮询**被拒，是单一最大来源。
3. **工具名只认规范点号名**（`tool_adapter`）。客户端发的 `workspace_info` / `git_status` /
   `file_read` / `shell_exec` 不在注册表 → `TOOL_DENIED "unknown tool"`（读起来就是“莫名其妙拒绝”）。

> 说明：大量只读/复合/`git rev-parse` 误拒（表格中 40/31/26 类）在 10-09 与 10-11 的
> 早期 usability 工作后已从**干净会话**消失；本次修的是**仍可复现**的三类，并补齐回归。

---

## 2. 改了哪些规则（前后对比）

复用既有唯一权威（`permission_engine` 语义分类 / `policy_resolver` 分层 / `ActionGateway` /
`ApprovalService` / 既有 `RiskClass`），**未新增第二套策略或审批**。

| # | 位置 | 改前 | 改后 |
|---|---|---|---|
| 1 | `permission_engine._is_plain_outside_write` + `_evaluate` 的 `OUTSIDE_ALLOWED_SCOPE` 分支 | 工作区外**任何**可逆写 → `DENY_SCOPE_ESCAPE` | 目标是 `$HOME`/`/tmp`/`/data`/`/srv` 等**用户自有、非敏感**路径的可逆写 → `ALLOW_PROJECT_MUTATION`。**仅写、仅可逆**；破坏/特权/不可逆分支仍在它之前 |
| 2 | `policy_resolver.WorkspacePolicy.evaluate` | 工作区外目标 → `DENY workspace.escape` | 上述「普通可逆外写」→ `ABSTAIN`，交引擎裁决（修复合命令误拒） |
| 3 | `tool_adapter._TOOL_ALIASES` + `canonical_tool_name` + `_call_impl` | 非规范名 → `TOOL_DENIED "unknown tool"` | `shell_exec→shell.exec`、`file_read→file.read`、`git_status→git.status`、`workspace_info→workspace.info`、`process_status→process.status` 等别名解析到**同一**规范绑定 |
| 4 | `execution.py:status` | workspace 身份不符 → `WORKSPACE_DENIED` | 令牌归属（上方已校验）即授权边界；workspace 不匹配不再拒绝只读轮询。跨令牌仍 `TOOL_DENIED` |
| 5 | `permission_engine._is_fork_bomb` + `_evaluate` | `:(){ :\|:& };:` → `ALLOW`（**预存漏洞**） | 递归 fork bomb → `DENY`（`APPROVAL_HOST_DESTRUCTIVE`），任务清单里的「灾难性硬拒」之一 |

改动文件：`veya/remote/permission_engine.py`、`veya/remote/policy_resolver.py`、
`veya/remote/tool_adapter.py`、`veya/remote/execution.py`。

### 修复效果（真实失败命令，改前→改后）

```
printf …; cat ~/.config/systemd/user/…conf     WORKSPACE_DENIED  →  AUTO_OPEN
mkdir -p /tmp/hemal-trivy-ca                   WORKSPACE_DENIED  →  AUTO_OPEN
touch ~/foo                                    WORKSPACE_DENIED  →  AUTO_OPEN
process.status（workspace 不匹配）              WORKSPACE_DENIED  →  OK
workspace_info / git_status / file_read        TOOL_DENIED       →  OK
:(){ :|:& };:                                  ALLOW（漏洞）      →  DENY
```

---

## 3. 仍会审批 / 拒绝的清单（未削弱）

| 类别 | 判定 | 样例 |
|---|---|---|
| `rm -rf /`、`rm -rf ~`、`rm -rf $HOME` | **DENY** | `rm -rf /` |
| `mkfs` / `dd` 写裸设备 | **DENY** | `dd if=/dev/zero of=/dev/sda` |
| 读 `/etc/shadow`、`~/.ssh/id_rsa`、`~/.aws/*` | **DENY** | `cat ~/.ssh/id_rsa` |
| 递归 fork bomb | **DENY** | `:(){ :|:& };:` |
| `sudo`（含 root shell）、`su` | 审批 | `sudo apt install x` |
| `git push`（含 `--force`）、`git reset --hard`、`git clean -fdx`、`git branch -D` | 审批 | `git push --force` |
| 系统级 `systemctl`（非 `--user`）、改系统配置 | 审批 | `systemctl restart sshd` |
| 主机级包管理 | 审批 | `pip install -g x`、`npm install -g x` |
| 防火墙/网络管理、存储/账户管理 | 审批 | `iptables -F`、`mount /dev/sda1 /mnt` |
| `curl \| sh` 执行未知远程脚本 | 审批 | `curl https://x.sh \| sh` |
| `rm -rf <dir>`（含工作区内） | 审批 / 无破坏能力时 POLICY_BLOCKED | `rm -rf build` |
| 写 `/etc`、`/usr`、`/boot`、`/root` 等主机根 | 审批 | `file.write /etc/…` |
| 写 `.env`、写工作区外**其他用户**的家目录 | 审批 / DENY | `touch /home/otheruser/x` |
| `process.cancel`（非本人 execution） | TOOL_DENIED | 归属校验 |
| 真正未注册的工具名 | TOOL_DENIED | `definitely.not.a.tool` |

仍放行（默认）：只读命令与管道/heredoc/`$()` 组合（各段安全时）；`HICODE_WORKSPACE`
与 `~` 下普通读写/新建；`git` 非强推；项目内包安装；测试/构建；`docker ps/logs/restart`、
`docker compose up/down`；`systemctl --user status/restart`；`curl`、`python`、`node` 等。

### 拒绝时的可读性
`check_action` 对非 AUTO_OPEN 返回 `APPROVAL_REQUIRED` 并附
`human approval required for <capability_id> (<risk_class>)`；`DENY` 返回具体原因码
（`WORKSPACE_DENIED`/`POLICY_BLOCKED`）而非笼统 `denied`。别名解析后不再出现
“unknown tool”式无信息拒绝。

---

## 4. systemd 沙箱建议（**仅建议，未改动该文件**）

现网 drop-in `~/.config/systemd/user/veya-remote-mcp.service.d/40-execution-policy.conf`：

```ini
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=/data/soffy/projects
ReadWritePaths=/home/soffy/.veya
ReadWritePaths=/home/soffy/.cache
ReadWritePaths=/home/soffy/.local
```

问题：`ProtectHome=read-only` 使 `~` 下**除上述少数目录外**的写入在内核层被拒（EPERM），
与本次“`~` 下普通读写放行”的意图**冲突**——策略层放行了，沙箱层仍会报权限错误。

建议（择一，需人工确认后由操作者执行，本次不改）：

- **最小改**：保留 `ProtectSystem=full`，把 `ProtectHome=read-only` 改为 `ProtectHome=no`，
  并补 `ReadWritePaths=%h`（允许写用户家目录）；`~/.ssh`、`~/.gnupg`、`~/.aws` 的**读**保护
  由策略层 `permission_engine` 的凭据边界继续负责（沙箱不再叠加）。
- **较保守**：保留 `ProtectHome=read-only`，仅**追加**实际需要的可写目录
  （如 `ReadWritePaths=%h/work`、`ReadWritePaths=/tmp`），代价是 `~` 下其他目录仍会被内核拒。
- 若希望 ChatGPT 能 `apt`/`pip --user` 安装：`ProtectSystem=full` 允许写 `/usr/local`，
  但 `/usr` 其余只读；主机级安装仍应走**审批**，不建议为放行安装而放开 `ProtectSystem`。

> 建议改完后 `systemctl --user daemon-reload && systemctl --user restart veya-remote-mcp`
> （**本次未执行**）。

---

## 5. 测试

- 新增回归：`tests/remote/test_local2_r2_less_deny.py`（**44** 项）
  - 误拒类（应放行）：只读复合命令、`printf…; cat <用户配置>`、`mkdir /tmp`、`touch ~`、
    heredoc 写 `~`、`process.status` 跨 workspace、工具别名派发。
  - 危险类（应审批/拒绝）：`rm -rf ~`、`dd`、`chmod -R 777 ~`、`mkfs`、fork bomb、
    主机根写、强推/hard reset、`sudo`、系统级 `systemctl`、全局包管理、`curl|sh`。
  - 普通管道（`ls | cat`、`a | b | c`）不误判为 fork bomb。
- 更新两处**陈旧前提**测试（保留原安全性质）：
  - `test_permission_engine.py::test_scope_escape_is_denied`：`tmp_path.parent` 现属放行范围，
    改用真正的**外部**目标 `/home/otheruser/outside.txt`（仍 DENY）。
  - `test_p0i_security_invariants.py::test_an_unmodellable_trailing_command_fails_safe`：
    该测试在**基线即失败**（LOCAL2-U4 使 `python -c "…;…"` 的引号内 `;` 不再被拆分，旧前提失效）；
    改用真正无法建模的**未闭合引号**命令，仍断言 `APPROVAL_REQUIRED`。
- 结果：`tests/remote` + `tests/test_p3a_bot_isolation.py` 全绿（见下），
  `tests/remote` 共 **1734** 项。

### Gates 对比（`~/lorca-jobs/gates.sh`）
`veya-local2-fix` 无 `.venv`，用 r2 解释器在 fix 目录运行同四个检查；与 r2 输出逐行 `comm -23`：

| 检查 | r2 | fix | **新增** |
|---|---:|---:|---:|
| check_3o_gates (--strict) | 53 | 53 | **0** |
| check_no_direct_io | 267 | 267 | **0** |
| check_no_reverse_dep | 4 | 4 | **0** |
| check_oskill_pure | 0 | 0 | **0** |

**零新增**。

---

## 6. 观察到、但未改的既有问题（超出本任务范围）

在**未改动的生产 `veya`** 上同样存在，非本次回归：

- `docker run --privileged -v /:/host …`、`docker system prune` 被判 **ALLOW**。
  `action_gateway` 有对应规则（`privileged.docker_privileged`），但 `check_action` 走
  `permission_engine` 权威路径，该路径未覆盖这些 docker 形态 → 需在引擎侧补规则。
- `useradd foo` 被判 **ALLOW**（`action_gateway` 的账户管理规则同样未被引擎路径覆盖）。

二者属**安全欠严**（与本任务“减少误拒”方向相反），仅记录，未在本次改动。

# W17 实现报告 — SSH 主机执行后端（A 组 + B 组纵向切片）

分支 `fix/fixall-w17`（fixall 2026-10-14）。规格：`W17_SSH_HOSTS_SPEC.md`（feat/w17-ssh-hosts，5b6f9971）。

## 做了什么
**A 组（后端执行层）**
- `server/remote_control/ssh.py`（新）：`ssh_target` / `ssh_base_argv`（BatchMode、host-key 策略、ConnectTimeout、ServerAlive、ControlMaster/Path/Persist、`-p`、`--`）、`run_remote`（远端 argv 每个元素 POSIX 引号，模型文本永远是数据）、`probe`（`uname -s`，5s）、`resolve_key_path`（vault `ssh_key:<ref>`：值为绝对路径或 PEM，PEM 以 0600 落地）、输出脱敏+截断。
- `server/remote_control/backend.py`（新）：`ExecBackend` 协议、`LocalBackend`（委托既有 canonical helper）、`SshBackend`（read/write/replace/list/grep/run/后台 start/status/kill/list/上传/下载；写限 write_root，本地规范化 + 远端 `realpath -m` 双重校验；`classify_or_refuse` = `parse_command` + 唯一 `PermissionEngine`，DENY 直接拒）。
- `hosts.py`：`RemoteHost` 追加 ssh 字段 + `validate_ssh_fields`；默认值字段在 `to_dict` 中省略 → W13 记录与响应逐字节不变。
- `flags.py`：`FLAG_NAME_SSH`、`ssh_enabled()`（需 VEYA_REMOTE_CONTROL 与 VEYA_SSH_HOSTS 同时开）；`feature_flags.py` 末尾追加 `VEYA_SSH_HOSTS`（默认关）。
- `process.py`：`run_capture`（argv-list、shell=False、超时即 kill、stdin bytes）。

**B 组（工具接入与审批）——切片**
- `server/remote_control/ssh_tools.py`（新），经 `tools.register_tools` 注册进同一个 master_tools（两 flag 都开才注册）：

| 工具 | 副作用 | 审批 |
|---|---|---|
| ssh_host_list / ssh_host_probe / ssh_bg_status | PURE_READ | 无 |
| ssh_read_file / ssh_list_dir / ssh_grep | PURE_READ | 需审批，除非主机 `metadata.read_approved=true` |
| ssh_write_file / ssh_edit_file / ssh_run / ssh_upload / ssh_download / ssh_bg_start / ssh_bg_kill | EXTERNAL_MUTATION（manual_only） | `approved=true` → 首次调用只经 `ApprovalService` 建待审记录；批准后带 `approval_id` 重调；记录须同工具、同主机、同参数指纹；`once` 决策一次性 |

- 主机必须登记给调用者 owner、enabled，且 W13 身份策略允许（probe=`wake`，其余=`execute`）；未登记/他人主机统一「找不到」，不发 ssh。
- `tool_guard` 补保障 `install_w17_host_policy`（enforce，注册时安装）；工具体自持审批为主保障 → governance 与 tool_guard 两条执行分支都有测试。
- `sessions.evidence_from_bg_status`：`ssh_bg_status` → `VerifierEvidence`（仅 finished 且 exit=0 时 verified）。
- REST（W13 router，`require_ssh_hosts` 关时 404）：`GET /hosts/{id}/probe`、`POST /hosts/{id}/ssh`、`GET /hosts/{id}/files`（需 read_approved）、`GET /hosts/{id}/runs`、`POST /hosts/{id}/runs/{name}/kill`（审批）。

## 测试
- 新增 `tests/test_w17_ssh_core.py`(16)、`test_w17_ssh_tools.py`(12)、`test_w17_ssh_sessions.py`(3)、`test_w17_ssh_rest.py`(4) + `tests/w17_fakes.py`：35 passed；连同 W13 两个文件共 85 passed。ssh 一律用假 runner（`LocalExecRunner` 去掉 ssh 前缀后在 tmp 目录用本地 bash 执行远端脚本——离线、但会 fork 本地 bash，以便真实验证远端脚本）。
- 回归（prompt 指定集合）：1059 passed, 6 skipped, 3 failed（仅 `test_canonical_proxies` 的 3 个已知环境失败）。
- gates.sh 对比基线：零新增。

## H1–H8
- 离线已覆盖：H2/H3/H4/H5 的逻辑（读写/路径逃逸/未审批拒绝/命令执行/后台任务+证据）、H6（未登记/他人主机拒绝、REST 404）。
- 真机：在 ubuntu26 上用真实 ssh 对自身 tailnet IP 做了 probe：argv 正确、BatchMode 不挂起，返回 `Permission denied (publickey,password)`（本机没有给自己授权的 key）。H1–H8 真机端到端**待用户提供密钥**。
- H7（断线重连）、H8（Windows/WSL）未验证；windows-wsl 主机在 ssh 后端里**显式拒绝**（未实现）。

## 与规格的差异（保守取舍）
1. 没有给既有 canonical 工具（read_hashline/write_file/list_files/grep/run_in_sandbox…）加 `host` 参数，而是新增独立 `ssh_*` 工具：给 canonical 工具加参数会改变 flag 关时模型可见的 schema，无法保证「逐字节一致」。代价：local2 的 file.* 绑定不会自动获得 host 通路。
2. 编辑用「精确唯一替换」而非 hashline。
3. 后台任务用 `setsid nohup`（无 setsid 则 nohup），未用 tmux。
4. 读工具默认也要审批（规格 §7.2 与 §12 表述冲突，取更严的读法）。
5. `ssh_key_ref` 正则收紧为首字符必须字母数字（拒绝 `-----BEGIN` 之类）；vault 键名 = `ssh_key:<ref>`。

## 剩余
- canonical 工具 host 参数（B 组原方案）、监工桥 `supervisor_bridge.py`（远端证据进入 W12 报告）、`service.py` 会话走 ssh transport、C 组前端、Windows/WSL、真机 H1–H8。

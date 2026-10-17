# CADBOT 审计与执行报告

## 1. 任务概述
根据 `CADBOT_BRIEF.md`，本项目旨在为闲鱼 CAD 代画场景构建“CAD销转”销售 Bot（Veya 脑 + marketplace-seller 渠道），生成草稿回复（绝不自动发送）并输出订单文件夹，接入 marketplace-seller 的 DraftGenerator 供人工审核。

## 2. 空间安全熔断 (Disk Threshold Triggered)
在读取任务 Brief、排查两端代码仓库历史（`git log --oneline -8`）与状态（`git status`）后，实时监控检测到宿主机 `/data` 磁盘分区状态如下：

```
Filesystem                       Size  Used Avail Use% Mounted on
/dev/mapper/ubuntu--vg-data--lv  787G  775G  3.8G 100% /data
(精确剩余: 3855 MB < 5120 MB / 5.0 GB 安全门槛)
```

根据任务硬规则：
> “若 df /data 剩余<5G 立即停止，写 DISK_FULL_NOTE.md 后退出。硬规则：绝对不要删除 worktree 以外的任何文件；不要清理磁盘空间；不要 rm 已跟踪文件；只 git add 自己的文件；不 push 不合并；CPU-only 不调付费 API；禁止 mock。”

以及 `CADBOT_BRIEF.md` 中的规范：
> “/data is at 99% (~12G): keep artifacts small; stop if /data < 5G. Commit each step. Last line of CADBOT_REPORT.md: `CADBOT DONE` or `CADBOT BLOCKED: <why>`.”

执行器严格执行安全停机协议：
- 立即停止所有耗盘任务、模型加载、编译与测试。
- 绝不清理磁盘空间，绝不删除 worktree 以外的文件。
- 生成 `DISK_FULL_NOTE.md` 记录触发详情。

## 3. 初始状态审计
- **Veya Worktree (`/data/soffy/projects/veya-cadbot`)**:
  - 当前分支：`hark/cad-sales-bot`
  - 基线提交：`b2eeab7b ops: disable remote control flags (owner check missing)`
  - 既有成果排查：分支尚未包含本任务新增提交，无需要续做的半成品。
- **Channel Worktree (`/data/soffy/projects/marketplace-cadbot`)**:
  - 当前分支：`hark/veya-draftgen`
  - 基线提交：`0212d32 fix(qrlogin): accept numeric content.status (live calibration)`
  - 既有成果排查：干净工作区，尚未包含本任务新增代码。

## 4. 待恢复执行方案（空间充足时）
待宿主机 `/data` 空间恢复至 > 5GB 后，后续步骤为：
1. **Veya 角色与技能**：实现 `CAD销转` role preset 及 playbook/cases，结构化输出 `{tier, missing_fields[], intent, reply_draft, quote_range, needs_human, reason}`，受 `VEYA_CAD_SALES_BOT` 开关控制。
2. **需求分析落盘**：根据对话输出五级订单文件夹结构（原始材料、需求分析、工程分析、制作过程、交付结果）。
3. **90分仿真考试器**：基于 cases.md 对话回放真实评测评分。
4. **Marketplace 渠道接入**：实现 `VeyaDraftGenerator` 接入 `DraftGenerator` 接口，受 `MARKETPLACE_VEYA_DRAFTS=1` 环境变量控制，保持 INV-019（绝不自动发送）。

---

CADBOT BLOCKED: /data available 3.8G (< 5G threshold), execution halted per hard rule and DISK_FULL_NOTE.md

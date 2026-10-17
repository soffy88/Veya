# DISK_FULL_NOTE: /data 空间不足 5GB 停止通告

## 触发原因
- 监控时间：2026-10-17 06:40 (UTC)
- 当前 `/data` 空间状态：
  ```
  Filesystem                       Size  Used Avail Use% Mounted on
  /dev/mapper/ubuntu--vg-data--lv  787G  775G  3.8G 100% /data
  (精确数值: 3855 MB < 5120 MB 熔断门槛)
  ```
- 宿主机多任务后台进程（包括外部模型推理、Docker 容器与测试进程）正在持续消耗 `/data` 空间，磁盘余量已跌破 5.0 GB 安全红线。

## 规则执行
严格遵守任务硬规则：
> “若 df /data 剩余<5G 立即停止，写 DISK_FULL_NOTE.md 后退出。硬规则：绝对不要删除 worktree 以外的任何文件；不要清理磁盘空间；不要 rm 已跟踪文件；只 git add 自己的文件；不 push 不合并；CPU-only 不调付费 API；禁止 mock。”

已立即终止所有耗盘操作，未进行任何外部文件删除或空间清理。

## 现场与代码库状态
1. **veya-cadbot**：
   - 路径：`/data/soffy/projects/veya-cadbot`
   - 分支：`hark/cad-sales-bot`
   - HEAD：`b2eeab7b ops: disable remote control flags (owner check missing)`
   - 状态：工作区除本通告及审计报告外无污染，未误提交非本任务文件。
2. **marketplace-cadbot**：
   - 路径：`/data/soffy/projects/marketplace-cadbot`
   - 分支：`hark/veya-draftgen`
   - HEAD：`0212d32 fix(qrlogin): accept numeric content.status (live calibration)`
   - 状态：干净，无修改。

## 后续恢复建议（不在此代理执行）
1. 等待宿主机后台任务释放 `/data` 分区空间至 > 5GB（或由运维在 worktree 外部安全清理过期的临时卷/历史测试产物）。
2. 空间恢复后，重新启动 CAD 销转 bot 的 4 步研发与 90 分仿真考试流程。

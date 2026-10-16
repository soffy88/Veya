# RKZ（借鉴 Rakazo）最小任务单 —— **待用户确认范围**（未确认前不合入 main）
来源: github.com/elie222/rakazo（Apache-2.0）。只借鉴思路，不拷代码，不访问外部服务。
## 范围（最小，仅后端）
借鉴 Rakazo "routines 存为可读 Markdown + 越界需审批" 的思路:
1. 新模块 veya/rkz/routines.py: routine = 一个 .md 文件（YAML front matter: name, description, schedule(cron 字符串, 仅存储不执行), requires_approval: bool, tools: [..]；正文=步骤）。存储目录 VEYA_RKZ_ROUTINES_DIR（默认 ~/.veya/rkz/routines，0700）。提供 list/get/create/update/delete(需 confirm)/validate；名字只允许 [a-z0-9-]，防路径穿越。
2. 新路由 server/routes/rkz.py，前缀 /api/v1/rkz/routines，全部 `Depends(require_owner)`（server/auth.py；未登录401、非owner403），**鉴权先于 flag**；flag VEYA_RKZ_ROUTINES 默认 OFF，OFF 时 owner 得 404。在 server/app.py 只新增 include_router 行。
3. 不实现调度执行、不接模型、不加前端。
4. 测试 tests/test_rkz_*.py: CRUD、front matter 解析/校验、路径穿越拒绝、0700、每个路由 401/403/flag-OFF 404、delete 需 confirm。
## 规则
只改本 worktree（/data/soffy/projects/veya-rkz，分支 feat/rkz-borrow）；main 已有代码只能新增行；不 push、不重启服务、不花钱、不访问 muse.ai；TMPDIR=/data/soffy/tmp/rkz；测试 `.venv/bin/python -m pytest tests/test_rkz_*.py tests/test_sec_*.py -q -p no:cacheprovider --basetemp=/data/soffy/tmp/rkz/pt`，SEC 不得新增失败。每项完成即 git commit。最后写 RKZ_REPORT.md（commit sha/测试数），末行 `RKZ DONE` 或 `RKZ BLOCKED: 原因`。

# AUD2：前端全面可用性审计并修复
目标：Veya 前端每个页面、每个主要按钮都真的能用。
1) 列出全部前端路由/页面/主要操作（侧边栏、设置、Bots、技能、审批、routines/事件触发、证据报告、群聊、MCP presets、Team、Projects 等），以及每个操作调用的后端 API。
2) 用 Playwright 登录后逐页真实操作，记录：报错、空白、死链、无法返回、提示需登录、按钮无反应、API 404/405/422/500、加载转圈不结束、移动端（iPhone 尺寸）布局坏。
3) 新功能（角色技能、教一次技能审批、事件触发 webhook、证据三段报告）若后端已有但前端没入口或没接线，补上最小可用 UI。
4) 修复所有能修的问题；Team/Projects 登录态和返回主页由 AUD1 负责，你不要改这两处鉴权逻辑，只在报告里记录现象。
临时后端端口 127.0.0.1:8798，容器名 veya-aud2。
交付 AUD2_REPORT.md：审计矩阵（页面|操作|API|修前|修后|证据截图），修复 commit 列表，测试/回归/构建原文，未修项与原因。末行严格 `AUD2 DONE` 或 `AUD2 BLOCKED: 原因`。
## 共同规则
- 基于最新 main（已含 GBP1-6、FIXA/B，生产 09:16 部署）。先读 OPS_DEPLOY.md、AGENTS.md、CLAUDE.md，搞清前端在哪（apps/ 或 web 前端）、怎么构建、生产如何托管（veya.aiinote.com）、鉴权方式（token/cookie/localStorage 字段名）。
- 用你自己的隔离临时环境验证：临时后端容器 veya-audN（端口见下，独立数据目录，参照 /data/soffy/projects/veya-acc 里 veya-acc 的建法），前端本地 dev/build 指向它。绝不能占用 8794（veya-openai-tunnel）、8793（stage）；不得停/杀任何系统服务、他人进程或容器；不碰 veya-backend、veya-stage、生产库；不 push/merge/deploy；DB 不改到 /tmp。
- 用 Playwright（无头 Chromium，没有就 pip/npx 装）真实登录后点击验证，截图存 evidence/。不准用 mock 冒充“能用”。
- 修 bug：先写失败测试（后端 pytest / 前端单测或 Playwright 用例）提交 RED，再 GREEN。
- 回归 `tests/test_w1*.py tests/test_nano_*.py tests/test_lorca_*.py tests/test_orca_*.py tests/test_p3a_bot_isolation.py tests/test_product_shell.py tests/test_gbp*.py tests/test_fix*.py`，基线约 1086 passed，不得新增失败；前端 build 必须通过。
- /data 剩余 <10G 立即停下写 BLOCKED。

# W10 分组 B 上下文/提示缓存/记忆（只做本组，其它组由别的工人并行完成）
规格：NANO_W10_SPEC.md（HARD RULES、3O 章节对所有组都适用）。
本组范围：主体 4（prompt-cache 纪律：固定顺序哈希分段、回合内冻结工具集、usage 事件记录 cached/uncached、performance 路由暴露命中率）、ADDENDUM M（context steward）、N（read_own_chat 工具，复用 W2 FTS）、主体 5（记忆分层 daily/scratch/core+gardening、memory_search）、O（记忆复审+团队记忆）、W1（persistent workers）、W2（compact & resume 带上 workers）、U（scripts/llm_tap.py + image-to-text）、S（usage 页 token 明细）。主要动 prompt 组装、server/bot_team/memory.py、usage 事件、会话 compact/handoff。
不要做其它组的条目，也不要改它们的主要文件；需要别组的能力时只留清晰接口/TODO。
测试文件：tests/test_nano_w10_b.py（只建自己的，不要建 tests/test_nano_w10_always_on.py）。
设计文档：docs/NANO_W10_B_DESIGN.md；报告：NANO_W10_B_REPORT.md。
共享文件（veya/*/__init__.py 的 re-export、server/feature_flags.py、tool registry）只做追加，不重排、不删别人的行，方便合并。

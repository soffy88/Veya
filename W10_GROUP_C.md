# W10 分组 C 安全与权限（只做本组，其它组由别的工人并行完成）
规格：NANO_W10_SPEC.md（HARD RULES、3O 章节对所有组都适用）。
本组范围：主体 3（命令自动审查：ActionGateway hook + 注入式 LLMCaller 审查模型，allow/ask/block，provenance 用 firewall.sanitize）、ADDENDUM P（群角色 + 权限矩阵 + reply mode）、Q（每 bot 密钥：request_secret 卡片、现有密钥存储、只注入 runner shell env、结果和模型请求里掩码）、主体 7a（工具错误对 agent 友好 + registry 守卫测试）、7b（失败→eval 案例导出）。主要动 server/action_gateway_adapter.py、群组/权限、密钥存储、tool registry 错误路径。
不要做其它组的条目，也不要改它们的主要文件；需要别组的能力时只留清晰接口/TODO。
测试文件：tests/test_nano_w10_c.py（只建自己的，不要建 tests/test_nano_w10_always_on.py）。
设计文档：docs/NANO_W10_C_DESIGN.md；报告：NANO_W10_C_REPORT.md。
共享文件（veya/*/__init__.py 的 re-export、server/feature_flags.py、tool registry）只做追加，不重排、不删别人的行，方便合并。

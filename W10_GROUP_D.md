# W10 分组 D 网页观察与读取（只做本组，其它组由别的工人并行完成）
规格：NANO_W10_SPEC.md（HARD RULES、3O 章节对所有组都适用）。
本组范围：主体 6（a11y-first 观察阶梯，flag VEYA_A11Y_FIRST）、ADDENDUM R（read_browser DOM→Markdown 读取器）、T（搜索回退阶梯）、V（阅读网页的频道阶梯 + 真实探测 + veya doctor channels + GET /api/v1/channels/status + 设置页小节）、主体 7c（W8 阶梯文档扩到 6 步）。主要动 hands/browser 适配、obase 探测、oskill 阶梯表、搜索适配。
不要做其它组的条目，也不要改它们的主要文件；需要别组的能力时只留清晰接口/TODO。
测试文件：tests/test_nano_w10_d.py（只建自己的，不要建 tests/test_nano_w10_always_on.py）。
设计文档：docs/NANO_W10_D_DESIGN.md；报告：NANO_W10_D_REPORT.md。
共享文件（veya/*/__init__.py 的 re-export、server/feature_flags.py、tool registry）只做追加，不重排、不删别人的行，方便合并。

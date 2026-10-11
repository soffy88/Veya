# W10 分组 A 回合运行核心（只做本组，其它组由别的工人并行完成）
规格：NANO_W10_SPEC.md（HARD RULES、3O 章节对所有组都适用）。
本组范围：主体 1（turn queue 优先级+合并+steer）、主体 2（wake router + worth-bugging 策略）、W3（GoalRun 的 Goal/Loop 模式）、W4（实时工具结果流 + 计划列表 + 运行中纠正框，纠正经第 1 项 steer 落地）、W5（完成前检查卡）。主要动 server/bot_team/turns.py、async_turns.py、GoalRun、server/events.py 及对应 UI。
不要做其它组的条目，也不要改它们的主要文件；需要别组的能力时只留清晰接口/TODO。
测试文件：tests/test_nano_w10_a.py（只建自己的，不要建 tests/test_nano_w10_always_on.py）。
设计文档：docs/NANO_W10_A_DESIGN.md；报告：NANO_W10_A_REPORT.md。
共享文件（veya/*/__init__.py 的 re-export、server/feature_flags.py、tool registry）只做追加，不重排、不删别人的行，方便合并。

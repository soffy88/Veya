"""server.goal_run.parallel_markers — tasks.md 的 [P] 并行标记提取(smart-ralph 内化)。

对标 tzachbon/smart-ralph 的 tasks.md 格式(见 memory project_veya_pi_gap_audit):
"[P] markers for low-conflict parallel tasks"——task-planner 显式声明哪些任务
互相不冲突、可以并发跑, 不是 veya 自己去猜。

刻意不改 oskill.dag_compiler(3O 主库, 见 platform/3O/oskill/oskill/
dag_compiler.py): 那个解析器只管 Spec Kit 原生的 checkbox/Depends/Accept
语法, [P] 是 veya 这边另加的约定, 不属于要"贡献回主库"的通用能力——按
§1.4 单源纪律, 不在 veya 层重新实现 dag_compiler 已经做的事(标题/依赖/验收
解析), 只做它没做的事(提取 [P] 标记), 结果按任务 id 跟 dag_compiler 的输出
对齐。

约定: `[P]` 紧跟在 checkbox 之后、任务 id 之前, 例如:
    - [ ] [P] T2: Build API endpoint
    - [ ] [P] T3.1: Sub-task title
      Depends: T1
纯函数, 无 I/O。
"""

from __future__ import annotations

import re

_CHECK_WITH_P = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+\[P\]\s*(?P<id>T\d+(?:\.\d+)*)\b")


def extract_parallel_task_ids(tasks_md: str) -> set[str]:
    """扫描 tasks.md 正文, 返回所有标了 [P] 的任务 id 集合。空/无匹配返回空集合。"""
    if not tasks_md or not tasks_md.strip():
        return set()
    ids: set[str] = set()
    for line in tasks_md.splitlines():
        match = _CHECK_WITH_P.match(line)
        if match:
            ids.add(match.group("id"))
    return ids

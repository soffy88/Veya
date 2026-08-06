"""pytest 共享配置: 消除主库数据类被误收集的告警。

主库 omodul.code_reliability_loop.TestResult 是 dataclass (非测试类),
经测试模块导入后 pytest 会尝试按测试类收集 —— 标记 __test__ = False 豁免。
"""

from __future__ import annotations

import veya_loop

veya_loop.TestResult.__test__ = False  # type: ignore[attr-defined]

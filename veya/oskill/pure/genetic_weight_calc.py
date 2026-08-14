"""3O-PURE — genetic_weight_calc: 预留元素（先空实现，确定性占位）。

迁移计划：阶段 3+ 接入遗传权重自适应（工具选择/质量门控的权重演化）。
当前占位实现满足：
- 纯函数（无 I/O、无全局可变状态、无随机）；
- 确定性：相同输入必得相同输出；
- 接口形态即未来形态（fitness_history 输入 → 权重 dict 输出），
  届时把内部算法替换为遗传演化即可，调用方不变。
"""

from __future__ import annotations

from typing import Any

_DEFAULT_WEIGHTS: dict[str, float] = {
    "success_rate": 1.0,
    "avg_duration_ms": 0.5,
    "reliability": 1.0,
}


def default_weights() -> dict[str, float]:
    """返回默认权重的拷贝（确定性，调用方修改不影响全局）。"""
    return dict(_DEFAULT_WEIGHTS)


def calc_weights(fitness_history: list[dict[str, Any]]) -> dict[str, float]:
    """根据历史适应度计算权重（预留：当前为确定性移动平均占位）。

    输入: [{metric: value, ...}, ...]（每轮工具执行后的适应度记录）
    输出: {metric: weight, ...}
    """
    if not fitness_history:
        return default_weights()
    # 确定性移动平均: 每个 metric 取历史均值, 缺失 metric 用默认权重
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for record in fitness_history:
        if not isinstance(record, dict):
            continue
        for metric, value in record.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                sums[metric] = sums.get(metric, 0.0) + float(value)
                counts[metric] = counts.get(metric, 0) + 1
    if not sums:
        return default_weights()
    out = dict(_DEFAULT_WEIGHTS)
    for metric, total in sums.items():
        out[metric] = total / counts[metric]
    return out


__all__ = ["calc_weights", "default_weights"]

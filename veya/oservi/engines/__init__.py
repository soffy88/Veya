"""veya/oservi.engines — 引擎骨架注册表 (SPEC §7.2 依赖倒置)。

骨架 = 无状态机制 (调度/生命周期/重试), 业务实现全部经 ServiceManifest
注入。注册表只登记骨架, 不硬编码具体元素 import。
"""

from __future__ import annotations

from typing import Any, Callable

_ENGINES: dict[str, type] = {}


def register_skeleton(name: str, cls: type) -> type:
    _ENGINES[name] = cls
    return cls


def get_skeleton(name: str) -> type:
    if name not in _ENGINES:
        raise KeyError(f"骨架 {name!r} 未注册 — 可用: {sorted(_ENGINES)}")
    return _ENGINES[name]


def list_skeletons() -> list[str]:
    return sorted(_ENGINES)


class Injection:
    """注入点声明: kind + cardinality。"""

    def __init__(self, kind: str, cardinality: str = "one",
                 required: bool = True) -> None:
        self.kind = kind
        self.cardinality = cardinality
        self.required = required


class EngineSkeleton:
    """引擎骨架基类 — 无状态机制, 业务零逻辑。

    子类只实现机制 (循环/重试/调度), 业务函数经 manifest.inject 注入。
    """

    skeleton_name: str = "base"

    async def run(self, inject: dict[str, Any], trigger: dict[str, Any],
                  config: dict[str, Any]) -> Any:
        raise NotImplementedError

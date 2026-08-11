"""veya/oservi.manifest — ServiceManifest: 有状态引擎装配契约。

机制/业务分离: manifest 只声明 注入点(kind+cardinality) + 触发 + 配置,
不含业务实现。引擎骨架零业务逻辑 (SPEC §7.2 依赖倒置)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ManifestValidationError(ValueError):
    """Manifest 不合法 (注入点缺失 / kind 未注册 / cardinality 超限)。"""


@dataclass
class Injection:
    """注入点声明: kind + cardinality (与 oservi 主库注入契约对齐)。"""

    kind: str
    cardinality: str = "one"  # one | many
    required: bool = True


@dataclass
class ServiceManifest:
    """有状态引擎装配声明 — 业务零代码, 纯数据。"""

    name: str
    skeleton: str
    inject: dict[str, list[Any] | Any] = field(default_factory=dict)
    trigger: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.name or not self.name.strip():
            raise ManifestValidationError("manifest.name 不能为空")
        if not self.skeleton or not self.skeleton.strip():
            raise ManifestValidationError("manifest.skeleton 不能为空")
        if not self.inject:
            raise ManifestValidationError(
                f"manifest({self.name}) 未声明注入点 — 骨架不允许硬编码业务实现"
            )

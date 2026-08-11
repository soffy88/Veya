"""veya/oservi.assembler — 引擎骨架装配器 (SPEC §7.2 依赖倒置)。

把 ServiceManifest 与注册的引擎骨架组装为可运行 Service。
骨架 registry 只认注入点, 不认具体元素 — 业务实现全部经 manifest 注入。
"""

from __future__ import annotations

from typing import Any

from veya.oservi.manifest import ManifestValidationError, ServiceManifest


def _infer_kind(obj: Any) -> str:
    """注入对象 kind 推断: 可调用 → function, 否则 type(obj).__name__。"""
    if callable(obj):
        return "function"
    return type(obj).__name__


def validate_manifest(
    manifest: ServiceManifest, skeleton_kinds: dict[str, list[str]] | None = None
) -> list[str]:
    """校验 manifest: 注入点契约 (kind + cardinality) + 骨架注册。"""
    errors: list[str] = []
    manifest.validate()
    for slot, impls in manifest.inject.items():
        items = impls if isinstance(impls, list) else [impls]
        for it in items:
            kind = _infer_kind(it)
            if kind not in ("function", "str"):
                errors.append(
                    f"注入点 {slot!r} 的 {kind} 不是可装配实现 (function|str)"
                )
    if skeleton_kinds is not None and manifest.skeleton not in skeleton_kinds:
        errors.append(
            f"骨架 {manifest.skeleton!r} 未注册 — 可用: {sorted(skeleton_kinds)}"
        )
    return errors


class Service:
    """装配后的有状态服务实例 — 状态只在运行期 (无状态骨架定义)。"""

    def __init__(self, manifest: ServiceManifest, skeleton: Any) -> None:
        self.manifest = manifest
        self.skeleton = skeleton

    async def run(self) -> Any:
        runner = getattr(self.skeleton, "run", None)
        if runner is None:
            raise RuntimeError(f"骨架 {self.manifest.skeleton!r} 无 run()")
        return await runner(self.manifest.inject, self.manifest.trigger,
                            self.manifest.config)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Service {self.manifest.name} skeleton={self.manifest.skeleton}>"


def assemble(manifest: ServiceManifest) -> Service:
    """装配: 校验 manifest → 解析骨架 → 构建 Service 实例。"""
    from veya.oservi.engines import get_skeleton

    errors = validate_manifest(manifest, get_skeleton)
    if errors:
        raise ManifestValidationError("; ".join(errors))
    return Service(manifest, get_skeleton(manifest.skeleton))

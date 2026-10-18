"""Veya 使用的 harness2 surface.

harness2 的 proposer prompt 只认识 ``opencode`` / ``codex`` 两个 surface 名
（``core/prompts.py::_st``），所以 Phase 0 复用 ``OpencodeSubstrate`` 的
``SurfaceSpec``（name="opencode"）与它定义的 c1..c8 文件树形状，而不新造一个
harness2 不认识的 surface 名。Veya 的实际内容由导出器写进同一形状。
"""

from __future__ import annotations

from server.harness._harness2 import import_harness2_core


def veya_substrate():
    """返回 harness2 的 ``OpencodeSubstrate`` 实例（只用作 surface 提供方）。"""
    import_harness2_core()
    from harness2.substrate.opencode import OpencodeSubstrate  # type: ignore

    return OpencodeSubstrate()

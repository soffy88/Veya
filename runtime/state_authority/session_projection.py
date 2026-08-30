"""Thin, namespace-aware mutation adapter for the existing SessionTreeMgr.

The tree remains the physical projection store.  This adapter is deliberately
not a second projector engine: it only centralises the ownership check at the
two existing mutation call sites.
"""

from __future__ import annotations

from typing import Any

from .ownership import StateNamespace, assert_session_id, assert_writer


class SessionProjectionWriter:
    """Guarded access to one logical session-tree projection namespace."""

    def __init__(self, tree: Any, namespace: StateNamespace, writer: str) -> None:
        assert_writer(namespace, writer)
        self._tree = tree
        self.namespace = namespace
        self.writer = writer

    def ensure_session(self, sid: str, **kwargs: Any) -> str:
        assert_session_id(self.namespace, sid)
        return self._tree.ensure_session(sid, **kwargs)

    def append(self, sid: str, **kwargs: Any) -> str:
        assert_session_id(self.namespace, sid)
        return self._tree.append(sid, **kwargs)

    def branch(self, sid: str, **kwargs: Any) -> str:
        assert_session_id(self.namespace, sid)
        return self._tree.branch(sid, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Read operations (path, leaf, snapshot, ...) remain on the existing
        # tree; only mutation methods above are ownership-guarded.
        return getattr(self._tree, name)

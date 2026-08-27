"""loop-plane deps — store / audit / registry 单例（进程内共享）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import Settings
from app.infra.event_store import AuditLog, EventStore

_store: EventStore | None = None
_audit: AuditLog | None = None
_settings: Settings | None = None


def configure(
    settings: Settings, *, store: EventStore | None = None, audit: AuditLog | None = None
) -> None:
    """测试/装配用：替换全局单例（可注入实例以共享内存索引）。"""
    global _store, _audit, _settings  # noqa: PLW0603
    _settings = settings
    settings.ensure_dirs()
    _store = store or EventStore(settings.data_dir, tenant_id=settings.default_tenant)
    _audit = audit or AuditLog(settings.data_dir, tenant_id=settings.default_tenant)


def get_settings() -> Settings:
    global _settings  # noqa: PLW0603
    if _settings is None:
        _settings = Settings.from_env()
        _settings.ensure_dirs()
    return _settings


def get_store() -> EventStore:
    global _store  # noqa: PLW0603
    if _store is None:
        _store = EventStore(get_settings().data_dir, tenant_id=get_settings().default_tenant)
    return _store


def get_audit() -> AuditLog:
    global _audit  # noqa: PLW0603
    if _audit is None:
        _audit = AuditLog(get_settings().data_dir, tenant_id=get_settings().default_tenant)
    return _audit


def reset() -> None:
    """测试隔离：清空单例。"""
    global _store, _audit, _settings  # noqa: PLW0603
    _store = _audit = _settings = None


__all__ = ["configure", "get_audit", "get_settings", "get_store", "reset"]

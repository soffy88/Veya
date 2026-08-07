"""veya_loop.obase.loop_event_store — 长程任务事件溯源存储 (单一来源转发)。

转发 obase.loop_event_store: AppendOnlyEventStore (JSONL 事件流 + 链式
checksum + flock 并发安全 + dedupe + schema 迁移) 与 QuotaTracker
(goal 级预算治理 + 超支暂停/充值恢复 + 事件化)。
"""

from .._assembly import obase as _load_obase

_obase = _load_obase()

AppendOnlyEventStore = _obase.loop_event_store.AppendOnlyEventStore
QuotaTracker = _obase.loop_event_store.QuotaTracker
VerifyResult = _obase.loop_event_store.VerifyResult
LoopStoreError = _obase.loop_event_store.LoopStoreError
EVENT_SCHEMA_VERSION = _obase.loop_event_store.EVENT_SCHEMA_VERSION

__all__ = [
    "EVENT_SCHEMA_VERSION",
    "AppendOnlyEventStore",
    "LoopStoreError",
    "QuotaTracker",
    "VerifyResult",
]

"""Runtime lifecycle for a discovered free-model pool.

The gateway supplies provider-specific discovery and probe callbacks.  This
module only owns the provider-independent state machine: persistence,
catalog-based removal, consecutive-failure handling, and atomic application of
the active route list.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PoolEntry = dict[str, str]
Probe = Callable[[PoolEntry], Awaitable[tuple[bool, str]]]


def _model_id(entry: Mapping[str, Any]) -> str:
    """Return the upstream model id used by a provider catalog."""
    model = str(entry.get("model") or "")
    source = str(entry.get("source") or entry.get("provider") or "")
    if source == "opencode-go" and model.startswith("opencode-go/"):
        return model.split("/", 1)[1]
    return model


def entry_key(entry: Mapping[str, Any]) -> str:
    """Stable identity across a gateway restart and provider aliases."""
    source = str(entry.get("source") or entry.get("provider") or "").lower()
    return f"{source}/{_model_id(entry).lower()}"


@dataclass(frozen=True)
class FreePoolSnapshot:
    """One provider-catalog observation.

    ``available`` contains every model returned by a healthy source, while
    ``entries`` contains only models that passed the source's free/text filter.
    Keeping both lets the lifecycle remove a model that is still online but no
    longer free.
    """

    entries: tuple[PoolEntry, ...]
    available: Mapping[str, frozenset[str]]
    healthy_sources: frozenset[str]
    errors: Mapping[str, str]


class FreePoolLifecycle:
    """Persist and reconcile the active free-model routes."""

    def __init__(
        self,
        seed_pool: list[PoolEntry],
        *,
        state_path: str | Path,
        failure_threshold: int = 3,
        max_active: int = 32,
        inactive_retry_per_source: int = 4,
    ) -> None:
        self._seed_pool = [dict(entry) for entry in seed_pool]
        self._seed_keys = {entry_key(entry) for entry in self._seed_pool}
        self._state_path = Path(state_path).expanduser()
        self._failure_threshold = max(1, int(failure_threshold))
        self._max_active = max(0, int(max_active))
        self._inactive_retry_per_source = max(1, int(inactive_retry_per_source))
        self._lock = asyncio.Lock()
        self._records: dict[str, dict[str, Any]] = self._load_state()
        self.last_refresh_at = ""
        self.last_error = ""

    @property
    def state_path(self) -> Path:
        return self._state_path

    @property
    def active_pool(self) -> list[PoolEntry]:
        records = [r for r in self._records.values() if r.get("active")]
        records.sort(key=lambda item: int(item.get("order", 10**9)))
        return [dict(r["entry"]) for r in records]

    def status(self) -> dict[str, Any]:
        records = list(self._records.values())
        return {
            "last_refresh_at": self.last_refresh_at,
            "last_error": self.last_error,
            "state_path": str(self._state_path),
            "active": len([r for r in records if r.get("active")]),
            "known": len(records),
            "models": [
                {
                    "provider": r["entry"].get("provider"),
                    "model": r["entry"].get("model"),
                    "source": r["entry"].get("source"),
                    "active": bool(r.get("active")),
                    "status": r.get("status", "unknown"),
                    "consecutive_failures": int(r.get("consecutive_failures", 0)),
                    "last_error": r.get("last_error", ""),
                }
                for r in sorted(records, key=lambda item: int(item.get("order", 10**9)))
            ],
        }

    async def reconcile(
        self,
        snapshot: FreePoolSnapshot,
        probe: Probe,
    ) -> list[PoolEntry]:
        """Reconcile one catalog snapshot and return the new active pool."""
        async with self._lock:
            now = time.time()
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            self.last_refresh_at = now_iso
            self.last_error = "; ".join(
                f"{source}: {message}" for source, message in snapshot.errors.items()
            )

            # Seed order is stable.  A discovered entry with the same source
            # and model replaces the seed transport details in-place.
            candidates: OrderedDict[str, PoolEntry] = OrderedDict()
            for entry in self._seed_pool:
                candidates[entry_key(entry)] = dict(entry)
            for entry in snapshot.entries:
                candidates[entry_key(entry)] = dict(entry)

            # A healthy catalog is authoritative for both seed and discovered
            # entries.  Remove a seed before probing it when the upstream no
            # longer advertises the model; otherwise a successful probe could
            # accidentally resurrect a deleted route.
            for key, entry in list(candidates.items()):
                source = str(entry.get("source") or entry.get("provider") or "").lower()
                if source not in snapshot.healthy_sources:
                    continue
                available = snapshot.available.get(source, frozenset())
                if key.split("/", 1)[-1] not in available:
                    record = self._records.setdefault(
                        key,
                        {"entry": dict(entry), "active": False, "consecutive_failures": 0},
                    )
                    record["entry"] = dict(entry)
                    self._mark_inactive(record, "not present in provider catalog")
                    candidates.pop(key, None)

            # Carry forward models from a previous healthy route only when its
            # source was not successfully catalogued this time.  A successful
            # catalog is authoritative: missing means gone or no longer free.
            for key, record in list(self._records.items()):
                entry = record.get("entry") or {}
                source = str(entry.get("source") or entry.get("provider") or "").lower()
                if source not in snapshot.healthy_sources:
                    if record.get("active"):
                        candidates.setdefault(key, dict(entry))
                    continue
                available = snapshot.available.get(source, frozenset())
                if key.split("/", 1)[-1] not in available:
                    self._mark_inactive(record, "not present in provider catalog")
                elif key not in candidates and key not in self._seed_keys:
                    # Present upstream but no longer passes the free/text
                    # filter, so it must not silently remain in the pool.
                    self._mark_inactive(record, "no longer free or text-compatible")

            # Active routes and newly discovered models are checked every day.
            # Previously failed models are retried in a rotating batch, so a
            # provider with hundreds of free catalog entries cannot monopolize
            # the gateway's sockets during the daily refresh.
            to_probe: list[PoolEntry] = []
            inactive_by_source: dict[str, list[PoolEntry]] = {}
            for entry in candidates.values():
                key = entry_key(entry)
                record = self._records.get(key)
                if record is None or record.get("active") or key in self._seed_keys:
                    to_probe.append(entry)
                else:
                    source = str(entry.get("source") or entry.get("provider") or "").lower()
                    inactive_by_source.setdefault(source, []).append(entry)
            retry_day = int(now // 86400)
            for source, entries in inactive_by_source.items():
                batch_size = self._inactive_retry_per_source
                batch_count = max(1, (len(entries) + batch_size - 1) // batch_size)
                batch = retry_day % batch_count
                start = batch * batch_size
                to_probe.extend(entries[start : start + batch_size])

            # 分批并发探测 (BATCH_SIZE=8): 避免 asgiogather 一次性发起 50+ 请求造成
            # RSS 峰值 (单次刷新曾达 780MB, 触发 systemd-oomd kill 网关)。8 个连接
            # 既能保持 pool wait 不阻塞后续批次, 又把峰值控制在 ~200MB 以内。
            BATCH_SIZE = 8
            probe_by_key: dict[str, tuple[bool, str]] = {}
            for start in range(0, len(to_probe), BATCH_SIZE):
                batch = to_probe[start:start + BATCH_SIZE]
                results = await asyncio.gather(
                    *(self._probe_one(entry, probe) for entry in batch)
                )
                for entry, result in zip(batch, results, strict=True):
                    probe_by_key[entry_key(entry)] = result
            for order, entry in enumerate(candidates.values()):
                key = entry_key(entry)
                result = probe_by_key.get(key)
                if result is None:
                    continue
                ok, error = result
                key = entry_key(entry)
                record = self._records.setdefault(
                    key,
                    {
                        "entry": dict(entry),
                        "active": False,
                        "consecutive_failures": 0,
                        "order": order,
                    },
                )
                record["entry"] = dict(entry)
                record["order"] = order
                record["last_checked_at"] = now_iso
                if ok:
                    record["active"] = True
                    record["status"] = "healthy"
                    record["consecutive_failures"] = 0
                    record["last_error"] = ""
                    record["last_success_at"] = now_iso
                else:
                    failures = int(record.get("consecutive_failures", 0)) + 1
                    record["consecutive_failures"] = failures
                    record["last_error"] = error[:300]
                    if record.get("active") and failures < self._failure_threshold:
                        record["status"] = "cooldown"
                    else:
                        record["active"] = False
                        record["status"] = "unhealthy"

            # Keep route fan-out bounded.  All discovered records stay in the
            # state file and can enter on a later refresh if earlier routes
            # disappear.
            active_keys = [
                key for key, record in sorted(
                    self._records.items(), key=lambda item: int(item[1].get("order", 10**9))
                )
                if record.get("active")
            ]
            if self._max_active:
                for key in active_keys[self._max_active :]:
                    self._records[key]["active"] = False
                    self._records[key]["status"] = "capacity"
                active_keys = active_keys[: self._max_active]
            active = [dict(self._records[key]["entry"]) for key in active_keys]
            self._save_state(now_iso)
            return active

    async def _probe_one(self, entry: PoolEntry, probe: Probe) -> tuple[bool, str]:
        try:
            result = await probe(entry)
        except Exception as exc:  # probe failure is a state transition, not a crash
            result = False, f"{type(exc).__name__}: {exc}"
        return result

    @staticmethod
    def _mark_inactive(record: dict[str, Any], reason: str) -> None:
        record["active"] = False
        record["status"] = "removed"
        record["last_error"] = reason
        record["consecutive_failures"] = 0

    def _load_state(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        records = data.get("models") if isinstance(data, dict) else None
        if not isinstance(records, dict):
            return {}
        return {
            str(key): value
            for key, value in records.items()
            if isinstance(value, dict) and isinstance(value.get("entry"), dict)
        }

    def _save_state(self, now_iso: str) -> None:
        payload = {
            "version": 1,
            "updated_at": now_iso,
            "models": self._records,
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_name(f".{self._state_path.name}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError as exc:
            self.last_error = "; ".join(filter(None, [self.last_error, f"state: {exc}"]))

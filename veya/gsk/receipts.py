"""Metadata-only, hash-chained egress receipts anchored in Veya's execution-event store."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from veya.gsk import get_repo_root


_EMPTY_PAYLOAD_HASH = hashlib.sha256(b"").hexdigest()


def _canonical(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EgressReceiptLedger:
    def __init__(self, path: Optional[Path] = None, event_root: Optional[Path] = None):
        self.root = get_repo_root()
        configured_event_root = os.getenv("VEYA_GSK_EVENT_ROOT", "").strip()
        self.event_root = Path(event_root or configured_event_root or Path.home()).expanduser()
        configured_path = os.getenv("VEYA_GSK_EGRESS_RECEIPTS_PATH", "").strip()
        self.path = path or (
            Path(configured_path).expanduser()
            if configured_path
            else Path.home() / ".veya" / "gsk" / "egress_receipts.jsonl"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _event_store(self):
        from veya.remote.execution_events import ExecutionEventStore

        return ExecutionEventStore.shared(self.event_root, "gsk-egress-receipts")

    def _entry_fields(
        self,
        *,
        destination: str,
        payload_type: str,
        payload_size: int,
        payload_hash: str,
        timestamp: float,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "destination": destination,
            "payload_type": payload_type,
            "payload_size": payload_size,
            "payload_hash": payload_hash,
            "timestamp": timestamp,
            "metadata": metadata,
        }

    def log_egress(
        self,
        destination: str,
        payload_type: str,
        payload_size: int,
        metadata: Optional[Dict[str, Any]] = None,
        payload_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        previous = self.list_receipts()
        previous_hash = previous[-1].get("entry_hash", "0" * 64) if previous else "0" * 64
        timestamp = time.time()
        resolved_metadata = dict(metadata or {})
        fields = self._entry_fields(
            destination=destination,
            payload_type=payload_type,
            payload_size=int(payload_size),
            payload_hash=payload_hash or _EMPTY_PAYLOAD_HASH,
            timestamp=timestamp,
            metadata=resolved_metadata,
        )
        content_hash = hashlib.sha256(_canonical(fields).encode("utf-8")).hexdigest()
        entry_hash = hashlib.sha256(f"{previous_hash}:{content_hash}:{timestamp}".encode("utf-8")).hexdigest()
        record = {
            "prev_hash": previous_hash,
            "content_hash": content_hash,
            "entry_hash": entry_hash,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical(record) + "\n")
        from veya.remote.execution_events import ExecutionEventType

        self._event_store().append(
            ExecutionEventType.GSK_EGRESS_RECEIPT_RECORDED,
            payload={
                "entry_hash": entry_hash,
                "receipt_path": str(self.path),
                "destination": destination,
                "payload_type": payload_type,
            },
        )
        return record

    def verify(self) -> Dict[str, Any]:
        if not self.path.exists():
            try:
                from veya.remote.execution_events import ExecutionEventType

                anchored = [
                    event
                    for event in self._event_store().replay_after(0)
                    if event.event_type == ExecutionEventType.GSK_EGRESS_RECEIPT_RECORDED
                    and event.payload.get("receipt_path") == str(self.path)
                ]
                if anchored:
                    return {"valid": False, "count": 0, "error": "Receipt ledger file is missing but event anchors exist"}
            except Exception as exc:
                return {"valid": False, "count": 0, "error": str(exc)}
            return {"valid": True, "count": 0, "error": None}

        count = 0
        expected_previous = "0" * 64
        records: List[Dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("prev_hash") != expected_previous:
                        return {"valid": False, "count": count, "error": f"Chain broken at line {line_number}: prev_hash mismatch"}
                    fields = self._entry_fields(
                        destination=str(data.get("destination", "")),
                        payload_type=str(data.get("payload_type", "")),
                        payload_size=int(data.get("payload_size", -1)),
                        payload_hash=str(data.get("payload_hash") or _EMPTY_PAYLOAD_HASH),
                        timestamp=float(data.get("timestamp")),
                        metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
                    )
                    expected_content = hashlib.sha256(_canonical(fields).encode("utf-8")).hexdigest()
                    if expected_content != data.get("content_hash"):
                        return {"valid": False, "count": count, "error": f"Content hash mismatch at line {line_number}"}
                    expected_entry = hashlib.sha256(
                        f"{data.get('prev_hash')}:{data.get('content_hash')}:{data.get('timestamp')}".encode("utf-8")
                    ).hexdigest()
                    if expected_entry != data.get("entry_hash"):
                        return {"valid": False, "count": count, "error": f"Entry hash mismatch at line {line_number}"}
                    expected_previous = str(data.get("entry_hash"))
                    records.append(data)
                    count += 1

            from veya.remote.execution_events import ExecutionEventType

            event_path = self.event_root / ".veya" / "execution-events" / "gsk-egress-receipts.jsonl"
            if event_path.exists():
                anchored_hashes = [
                    event.payload.get("entry_hash")
                    for event in self._event_store().replay_after(0)
                    if event.event_type == ExecutionEventType.GSK_EGRESS_RECEIPT_RECORDED
                    and event.payload.get("receipt_path") == str(self.path)
                ]
                recorded_hashes = [record.get("entry_hash") for record in records]
                if anchored_hashes != recorded_hashes:
                    return {"valid": False, "count": count, "error": "Receipt ledger differs from canonical execution-event anchors"}
            return {"valid": True, "count": count, "error": None}
        except Exception as exc:
            return {"valid": False, "count": count, "error": str(exc)}

    def list_receipts(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        records: List[Dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        records.append(value)
        return records

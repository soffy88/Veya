from __future__ import annotations

import os
import time
import json
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

def _get_receipts_path() -> Path:
    return Path(os.environ.get("GSTACK_RECEIPTS_PATH", ".gsk-data/egress_receipts.jsonl"))

def log_egress(destination: str, egress_type: str, size: int, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not os.environ.get("VEYA_GSK_EGRESS_RECEIPTS"):
        return {"status": "disabled"}

    path = _get_receipts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    
    prev_hash = "GENESIS"
    if path.is_file():
        try:
            lines = path.read_text().splitlines()
            if lines:
                last_record = json.loads(lines[-1])
                prev_hash = last_record.get("record_hash", "GENESIS")
        except Exception:
            pass

    content_dict = {
        "destination": destination,
        "type": egress_type,
        "size": size,
        "timestamp": time.time(),
        "metadata": metadata or {},
    }
    content_str = json.dumps(content_dict, sort_keys=True)
    content_hash = hashlib.sha256(content_str.encode("utf-8")).hexdigest()

    record_to_hash = f"{prev_hash}:{content_hash}"
    record_hash = hashlib.sha256(record_to_hash.encode("utf-8")).hexdigest()

    entry = {
        "prev_hash": prev_hash,
        "content_hash": content_hash,
        "record_hash": record_hash,
        "destination": destination,
        "type": egress_type,
        "size": size,
        "timestamp": content_dict["timestamp"],
    }

    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")

    return {"status": "logged", "record_hash": record_hash}

def verify_receipts() -> Dict[str, Any]:
    if not os.environ.get("VEYA_GSK_EGRESS_RECEIPTS"):
        return {"status": "disabled"}

    path = _get_receipts_path()
    if not path.is_file():
        return {"valid": True, "count": 0, "entries": []}

    lines = path.read_text().splitlines()
    expected_prev = "GENESIS"
    entries = []

    for i, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except Exception:
            return {"valid": False, "error": f"Invalid JSON at line {i+1}"}

        if entry.get("prev_hash") != expected_prev:
            return {"valid": False, "error": f"Chain broken at line {i+1}"}

        # Recompute content_hash from fields to detect field tampering
        content_dict = {
            "destination": entry.get("destination"),
            "type": entry.get("type"),
            "size": entry.get("size"),
            "timestamp": entry.get("timestamp"),
            "metadata": entry.get("metadata", {}),
        }
        content_str = json.dumps(content_dict, sort_keys=True)
        recomputed_content_hash = hashlib.sha256(content_str.encode("utf-8")).hexdigest()

        if recomputed_content_hash != entry.get("content_hash"):
            return {"valid": False, "error": f"Content hash mismatch / tampering detected at line {i+1}"}

        record_to_hash = f"{entry.get('prev_hash')}:{entry.get('content_hash')}"
        computed_hash = hashlib.sha256(record_to_hash.encode("utf-8")).hexdigest()
        if computed_hash != entry.get("record_hash"):
            return {"valid": False, "error": f"Record hash mismatch at line {i+1}"}

        expected_prev = entry.get("record_hash")
        entries.append(entry)

    return {"valid": True, "count": len(entries), "entries": entries}

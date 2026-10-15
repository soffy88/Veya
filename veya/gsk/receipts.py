from pathlib import Path
import hashlib
import json
import os
import time
from typing import Dict, Any, List, Optional
from veya.gsk import get_repo_root, is_enabled

class EgressReceiptLedger:
    def __init__(self, path: Optional[Path] = None):
        self.root = get_repo_root()
        configured_path = os.getenv("VEYA_GSK_EGRESS_RECEIPTS_PATH", "").strip()
        self.path = path or (
            Path(configured_path).expanduser()
            if configured_path
            else Path.home() / ".veya" / "gsk" / "egress_receipts.jsonl"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log_egress(self, destination: str, payload_type: str, payload_size: int, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        prev_hash = "0" * 64
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    lines = [l.strip() for l in f if l.strip()]
                    if lines:
                        last_entry = json.loads(lines[-1])
                        prev_hash = last_entry.get("entry_hash", prev_hash)
            except Exception:
                pass

        timestamp = time.time()
        content_raw = f"{destination}:{payload_type}:{payload_size}:{timestamp}"
        content_hash = hashlib.sha256(content_raw.encode("utf-8")).hexdigest()
        
        entry_raw = f"{prev_hash}:{content_hash}:{timestamp}"
        entry_hash = hashlib.sha256(entry_raw.encode("utf-8")).hexdigest()

        record = {
            "prev_hash": prev_hash,
            "content_hash": content_hash,
            "entry_hash": entry_hash,
            "destination": destination,
            "payload_type": payload_type,
            "payload_size": payload_size,
            "timestamp": timestamp,
            "metadata": metadata or {}
        }

        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return record

    def verify(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"valid": True, "count": 0, "error": None}
        
        expected_prev = "0" * 64
        count = 0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for idx, line in enumerate(f):
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("prev_hash") != expected_prev:
                        return {"valid": False, "count": count, "error": f"Chain broken at line {idx+1}: prev_hash mismatch"}
                    
                    content_raw = f"{data.get('destination')}:{data.get('payload_type')}:{data.get('payload_size')}:{data.get('timestamp')}"
                    calc_content_hash = hashlib.sha256(content_raw.encode("utf-8")).hexdigest()
                    if calc_content_hash != data.get("content_hash"):
                        return {"valid": False, "count": count, "error": f"Content hash mismatch at line {idx+1}"}

                    entry_raw = f"{data.get('prev_hash')}:{data.get('content_hash')}:{data.get('timestamp')}"
                    calc_entry_hash = hashlib.sha256(entry_raw.encode("utf-8")).hexdigest()
                    if calc_entry_hash != data.get("entry_hash"):
                        return {"valid": False, "count": count, "error": f"Entry hash mismatch at line {idx+1}"}

                    expected_prev = data.get("entry_hash")
                    count += 1
            return {"valid": True, "count": count, "error": None}
        except Exception as e:
            return {"valid": False, "count": count, "error": str(e)}

    def list_receipts(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        res = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        res.append(json.loads(line))
                    except Exception:
                        pass
        return res

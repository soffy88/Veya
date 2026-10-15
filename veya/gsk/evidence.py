from pathlib import Path
import subprocess
import hashlib
import json
import os
import time
from typing import Dict, Any, Optional
from veya.gsk import get_repo_root, is_enabled

def _git_status() -> tuple[str, str, str]:
    root = get_repo_root()
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        head = "unknown"
    try:
        porcelain = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=2).stdout
    except Exception:
        porcelain = ""
    try:
        diff = subprocess.run(["git", "diff"], cwd=root, capture_output=True, text=True, timeout=2).stdout
    except Exception:
        diff = ""
    diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    return head, porcelain.strip(), diff_hash

def compute_workspace_fingerprint() -> Dict[str, Any]:
    head, porcelain, diff_hash = _git_status()
    raw = f"{head}:{porcelain}:{diff_hash}"
    fp_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return {
        "head": head,
        "porcelain": porcelain,
        "diff_hash": diff_hash,
        "fingerprint": fp_hash,
        "timestamp": time.time()
    }

class EvidenceLedger:
    def __init__(self, ledger_path: Optional[Path] = None):
        self.root = get_repo_root()
        configured_path = os.getenv("VEYA_GSK_EVIDENCE_PATH", "").strip()
        self.ledger_path = ledger_path or (
            Path(configured_path).expanduser()
            if configured_path
            else Path.home() / ".veya" / "gsk" / "evidence_ledger.jsonl"
        )
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

    def record_run(self, label: str, command: str, exit_code: int, stdout_summary: str) -> Dict[str, Any]:
        fp = compute_workspace_fingerprint()
        entry = {
            "label": label,
            "command": command,
            "exit_code": exit_code,
            "stdout_summary": hashlib.sha256(stdout_summary.encode("utf-8")).hexdigest(),
            "fingerprint": fp,
            "recorded_at": time.time()
        }
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def check(self, label: str) -> str:
        if not self.ledger_path.exists():
            return "MISSING"
        latest = None
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    if data.get("label") == label:
                        latest = data
                except Exception:
                    pass
        if not latest:
            return "MISSING"
        
        current_fp = compute_workspace_fingerprint()
        recorded_fp = latest.get("fingerprint", {}).get("fingerprint")
        if current_fp["fingerprint"] == recorded_fp:
            return "FRESH"
        else:
            return "STALE"

    def get_record(self, label: str) -> Optional[Dict[str, Any]]:
        if not self.ledger_path.exists():
            return None
        latest = None
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    if data.get("label") == label:
                        latest = data
                except Exception:
                    pass
        return latest

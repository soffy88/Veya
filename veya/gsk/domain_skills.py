from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

def _get_path() -> Path:
    return Path(os.environ.get("GSTACK_DOMAIN_SKILLS_PATH", ".gsk-data/domain_skills.json"))

def _load_store() -> Dict[str, Any]:
    path = _get_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

def _save_store(store: Dict[str, Any]) -> None:
    path = _get_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2))

def get_skill(domain: str) -> Optional[Dict[str, Any]]:
    if not os.environ.get("VEYA_GSK_DOMAIN_SKILLS"):
        return None
    store = _load_store()
    return store.get(domain)

def record_skill_use(domain: str, content: str, success: bool = True) -> Dict[str, Any]:
    if not os.environ.get("VEYA_GSK_DOMAIN_SKILLS"):
        return {"status": "disabled"}

    store = _load_store()
    entry = store.get(domain)

    if not entry:
        entry = {
            "domain": domain,
            "content": content,
            "status": "quarantine",
            "success_count": 0,
            "failure_count": 0,
        }
    else:
        # update content if provided
        entry["content"] = content

    if success:
        entry["success_count"] += 1
        if entry["status"] == "quarantine" and entry["success_count"] >= 3:
            entry["status"] = "active"
    else:
        entry["failure_count"] += 1
        if entry["failure_count"] >= 2:
            entry["status"] = "demoted"

    store[domain] = entry
    _save_store(store)
    return {"status": "updated", "domain": domain, "skill_status": entry["status"], "success_count": entry["success_count"]}

def get_active_domain_skills() -> List[str]:
    if not os.environ.get("VEYA_GSK_DOMAIN_SKILLS"):
        return []
    store = _load_store()
    active = []
    for domain, entry in store.items():
        if entry.get("status") == "active":
            active.append(f"Domain [{domain}]: {entry.get('content')}")
    return active

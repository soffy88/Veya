import json
from pathlib import Path
from typing import Dict, Any, List, Optional
from veya.gsk import get_repo_root, is_enabled
from veya.gsk.envelope import sanitize_and_wrap

class DomainSkillManager:
    def __init__(self, storage_path: Optional[Path] = None):
        self.root = get_repo_root()
        self.storage_path = storage_path or (self.root / ".gsk-tmp" / "domain_skills.json")
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if self.storage_path.exists():
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}
        else:
            self.data = {}

    def _save(self):
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def register_skill(self, domain: str, skill_content: str) -> Dict[str, Any]:
        if domain not in self.data:
            self.data[domain] = []
        
        skill_entry = {
            "content": skill_content,
            "status": "quarantine", # quarantine, active, degraded
            "success_count": 0,
            "failure_count": 0
        }
        self.data[domain].append(skill_entry)
        self._save()
        return skill_entry

    def record_usage(self, domain: str, index: int, success: bool):
        if domain not in self.data or index >= len(self.data[domain]):
            return
        skill = self.data[domain][index]
        if success:
            skill["success_count"] += 1
            if skill["status"] == "quarantine" and skill["success_count"] >= 3:
                skill["status"] = "active"
            elif skill["status"] == "degraded" and skill["success_count"] >= 2:
                skill["status"] = "active"
        else:
            skill["failure_count"] += 1
            if skill["failure_count"] >= 2:
                skill["status"] = "degraded"
        self._save()

    def get_context_skills(self, domain: str) -> List[str]:
        if not is_enabled("VEYA_GSK_DOMAIN_SKILLS"):
            return []
        if domain not in self.data:
            return []
        
        active_skills = []
        for skill in self.data[domain]:
            if skill.get("status") == "active":
                wrapped = sanitize_and_wrap(skill.get("content", ""), source_type=f"domain_skill_{domain}")
                active_skills.append(wrapped)
        return active_skills

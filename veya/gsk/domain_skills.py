import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from typing import Dict, Any, List, Optional
from veya.gsk import get_repo_root, is_enabled
from veya.gsk.envelope import sanitize_and_wrap

class DomainSkillManager:
    def __init__(self, storage_path: Optional[Path] = None):
        self.root = get_repo_root()
        configured_path = os.getenv("VEYA_GSK_DOMAIN_SKILLS_PATH", "").strip()
        self.storage_path = storage_path or (
            Path(configured_path).expanduser()
            if configured_path
            else Path.home() / ".veya" / "gsk" / "domain_skills.json"
        )
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

    def get_active_context_entries(self, domain: str) -> List[tuple[int, str]]:
        """Return active skill text with stable store indexes for usage accounting."""
        if not is_enabled("VEYA_GSK_DOMAIN_SKILLS") or domain not in self.data:
            return []

        active_skills: List[tuple[int, str]] = []
        for index, skill in enumerate(self.data[domain]):
            if skill.get("status") == "active":
                wrapped = sanitize_and_wrap(
                    skill.get("content", ""), source_type=f"domain_skill_{domain}"
                )
                active_skills.append((index, wrapped))
        return active_skills

    def get_context_skills(self, domain: str) -> List[str]:
        return [content for _index, content in self.get_active_context_entries(domain)]


def extract_domain(task_text: str) -> str | None:
    """Extract the first explicit URL host from task text; never guess a domain."""
    candidates = re.findall(r"https?://[^\s<>\"']+|www\.[^\s<>\"']+", task_text, re.IGNORECASE)
    for candidate in candidates:
        candidate = candidate.rstrip(".,;:!?)]}")
        parsed = urlsplit(candidate if candidate.lower().startswith(("http://", "https://")) else f"https://{candidate}")
        host = (parsed.hostname or "").lower().rstrip(".")
        if host.startswith("www."):
            host = host[4:]
        if host:
            return host
    return None


def inject_domain_skills(context: Any, task_text: str, manager: DomainSkillManager | None = None) -> str | None:
    """Append active per-domain notes to Veya's existing selected-skills context."""
    if not is_enabled("VEYA_GSK_DOMAIN_SKILLS"):
        return None
    domain = extract_domain(task_text)
    if not domain:
        return None
    manager = manager or DomainSkillManager()
    context.project_context["_gsk_domain"] = domain
    existing_ids = {str(item.get("skill_id", "")) for item in context.selected_skills}
    for index, content in manager.get_active_context_entries(domain):
        skill_id = f"gsk-domain:{domain}:{index}"
        if skill_id in existing_ids:
            continue
        context.selected_skills.append(
            {
                "skill_id": skill_id,
                "version": "gsk",
                "trigger_reason": "active domain skill",
                "level2": content,
                "_gsk_domain": domain,
                "_gsk_domain_index": index,
            }
        )
    return domain


def record_domain_usage(domain: str | None, success: bool, manager: DomainSkillManager | None = None) -> None:
    """Count completed tasks for a URL domain; three successful tasks activate notes."""
    if not domain or not is_enabled("VEYA_GSK_DOMAIN_SKILLS"):
        return
    manager = manager or DomainSkillManager()
    for index in range(len(manager.data.get(domain, []))):
        manager.record_usage(domain, index, success)

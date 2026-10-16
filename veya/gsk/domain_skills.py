import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from typing import Dict, Any, List, Optional
from veya.gsk import get_repo_root, is_enabled
from veya.gsk.envelope import sanitize_and_wrap

class DomainSkillManager:
    """Site-note lifecycle persisted as SKILL.md documents under the existing skills tree.

    Passing ``storage_path`` is a deterministic JSON adapter for unit tests. Production
    defaults to per-note SKILL.md files, avoiding a second skill registry or callable tool.
    """

    _META_PREFIX = "<!-- gsk-domain-note "
    _META_SUFFIX = " -->"

    def __init__(self, storage_path: Optional[Path] = None):
        self.root = get_repo_root()
        configured_path = os.getenv("VEYA_GSK_DOMAIN_SKILLS_PATH", "").strip()
        explicit_json_path = storage_path or (Path(configured_path).expanduser() if configured_path else None)
        self.storage_path = explicit_json_path
        self._json_adapter = explicit_json_path is not None
        if self._json_adapter:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            self.storage_dir = None
        else:
            skills_root = Path(os.getenv("VEYA_SKILLS_DIR", str(Path.home() / ".veya" / "skills"))).expanduser()
            self.storage_dir = skills_root / "gsk-domain-notes"
            self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.data: Dict[str, List[Dict[str, Any]]] = {}
        self._load()

    @staticmethod
    def _domain_key(domain: str) -> str:
        return hashlib.sha256(domain.encode("utf-8")).hexdigest()[:24]

    def _note_path(self, domain: str, index: int) -> Path:
        assert self.storage_dir is not None
        return self.storage_dir / self._domain_key(domain) / f"note-{index:04d}" / "SKILL.md"

    @classmethod
    def _encode_note(cls, metadata: Dict[str, Any], content: str) -> str:
        meta = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"{cls._META_PREFIX}{meta}{cls._META_SUFFIX}\n\n{content.rstrip()}\n"

    @classmethod
    def _decode_note(cls, path: Path) -> tuple[Dict[str, Any], str] | None:
        try:
            raw = path.read_text(encoding="utf-8")
            first, _, rest = raw.partition("\n")
            if not first.startswith(cls._META_PREFIX) or not first.endswith(cls._META_SUFFIX):
                return None
            metadata = json.loads(first[len(cls._META_PREFIX):-len(cls._META_SUFFIX)])
            content = rest.lstrip("\n").rstrip("\n")
            if not isinstance(metadata, dict):
                return None
            return metadata, content
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _load(self) -> None:
        if self._json_adapter:
            if self.storage_path and self.storage_path.exists():
                try:
                    value = json.loads(self.storage_path.read_text(encoding="utf-8"))
                    self.data = value if isinstance(value, dict) else {}
                except (OSError, ValueError, json.JSONDecodeError):
                    self.data = {}
            return
        assert self.storage_dir is not None
        indexed: Dict[str, Dict[int, Dict[str, Any]]] = {}
        for path in sorted(self.storage_dir.rglob("SKILL.md")):
            decoded = self._decode_note(path)
            if decoded is None:
                continue
            metadata, content = decoded
            domain = str(metadata.get("domain") or "")
            try:
                index = int(metadata.get("index"))
            except (TypeError, ValueError):
                continue
            if not domain or index < 0:
                continue
            indexed.setdefault(domain, {})[index] = {
                "content": content,
                "status": str(metadata.get("status", "quarantine")),
                "success_count": int(metadata.get("success_count", 0)),
                "failure_count": int(metadata.get("failure_count", 0)),
            }
        for domain, entries in indexed.items():
            if entries:
                values: List[Dict[str, Any]] = []
                for index in range(max(entries) + 1):
                    values.append(entries.get(index, {"content": "", "status": "degraded", "success_count": 0, "failure_count": 2}))
                self.data[domain] = values

    def _save(self) -> None:
        if self._json_adapter:
            assert self.storage_path is not None
            self.storage_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        for domain, entries in self.data.items():
            for index, skill in enumerate(entries):
                path = self._note_path(domain, index)
                path.parent.mkdir(parents=True, exist_ok=True)
                metadata = {
                    "domain": domain,
                    "index": index,
                    "status": skill.get("status", "quarantine"),
                    "success_count": int(skill.get("success_count", 0)),
                    "failure_count": int(skill.get("failure_count", 0)),
                }
                path.write_text(self._encode_note(metadata, str(skill.get("content", ""))), encoding="utf-8")

    def register_skill(self, domain: str, skill_content: str) -> Dict[str, Any]:
        if domain not in self.data:
            self.data[domain] = []
        skill_entry = {
            "content": skill_content,
            "status": "quarantine",
            "success_count": 0,
            "failure_count": 0,
        }
        self.data[domain].append(skill_entry)
        self._save()
        return skill_entry

    def record_usage(self, domain: str, index: int, success: bool) -> None:
        if domain not in self.data or index < 0 or index >= len(self.data[domain]):
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
        if not is_enabled("VEYA_GSK_DOMAIN_SKILLS") or domain not in self.data:
            return []
        active_skills: List[tuple[int, str]] = []
        for index, skill in enumerate(self.data[domain]):
            if skill.get("status") == "active":
                wrapped = sanitize_and_wrap(skill.get("content", ""), source_type=f"domain_skill_{domain}")
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

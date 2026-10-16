from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any
import yaml

def _get_routines_dir() -> Path:
    d = Path(os.environ.get("VEYA_RKZ_ROUTINES_DIR", str(Path.home() / ".veya" / "rkz" / "routines")))
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    except Exception:
        pass
    return d

def _validate_name(name: str) -> str:
    if not re.match(r"^[a-z0-9-]+$", name):
        raise ValueError(f"Invalid routine name '{name}': only [a-z0-9-] allowed")
    return name

def _get_file_path(name: str) -> Path:
    _validate_name(name)
    d = _get_routines_dir()
    p = (d / f"{name}.md").resolve()
    if not p.is_relative_to(d.resolve()):
        raise ValueError("Path traversal detected")
    return p

def validate_routine_content(content: str) -> dict[str, Any]:
    if not content.startswith("---"):
        raise ValueError("Routine must start with YAML front matter")
    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError("Invalid YAML front matter structure")
    try:
        meta = yaml.safe_load(parts[1])
    except Exception as exc:
        raise ValueError(f"Invalid YAML front matter: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError("Front matter must be a dictionary")
    
    for req in ["name", "description", "schedule", "requires_approval", "tools"]:
        if req not in meta:
            raise ValueError(f"Missing required front matter field: {req}")
    
    _validate_name(str(meta["name"]))
    if not isinstance(meta["description"], str):
        raise ValueError("description must be a string")
    if not isinstance(meta["schedule"], str):
        raise ValueError("schedule must be a string")
    if not isinstance(meta["requires_approval"], bool):
        raise ValueError("requires_approval must be a boolean")
    if not isinstance(meta["tools"], list):
        raise ValueError("tools must be a list")
    
    body = parts[2].strip()
    return {"meta": meta, "body": body}

def list_routines() -> list[dict[str, Any]]:
    d = _get_routines_dir()
    res = []
    for p in sorted(d.glob("*.md")):
        try:
            content = p.read_text(encoding="utf-8")
            parsed = validate_routine_content(content)
            res.append(parsed["meta"])
        except Exception:
            pass
    return res

def get_routine(name: str) -> dict[str, Any]:
    p = _get_file_path(name)
    if not p.exists():
        raise FileNotFoundError(f"Routine '{name}' not found")
    content = p.read_text(encoding="utf-8")
    parsed = validate_routine_content(content)
    return {"meta": parsed["meta"], "body": parsed["body"], "content": content}

def save_routine(content: str) -> dict[str, Any]:
    parsed = validate_routine_content(content)
    name = str(parsed["meta"]["name"])
    p = _get_file_path(name)
    p.write_text(content, encoding="utf-8")
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    return parsed["meta"]

def delete_routine(name: str, confirm: bool = False) -> bool:
    if not confirm:
        raise ValueError("Delete requires confirm=True")
    p = _get_file_path(name)
    if not p.exists():
        raise FileNotFoundError(f"Routine '{name}' not found")
    p.unlink()
    return True

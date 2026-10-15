from __future__ import annotations

import os
import re
from typing import Optional

def check_careful_command(cmd: str) -> Optional[str]:
    """Check command against dangerous patterns (rm -rf /, DROP TABLE, git push --force, etc.)."""
    if not os.environ.get("VEYA_GSK_CAREFUL"):
        return None

    norm = cmd.strip()

    # 1. rm -rf checking (matches any combination of r/f flags with rm)
    if re.search(r"\brm\s+.*-[rR][fF]|\brm\s+.*-[fF][rR]|\brm\s+.*--recursive\s+--force|\brm\s+.*--force\s+--recursive|\brm\s+-[rR]\s+-[fF]|\brm\s+-[fF]\s+-[rR]|\brm\s+-[rf]+\b", norm, re.IGNORECASE):
        if re.search(r"\b(?:/|\$ROOT|\$HOME|\*)\b", norm) or norm.endswith(" /") or " /" in norm or "/home" in norm:
            return "Destructive rm -rf command detected targeting root or broad path."

    # 2. SQL DROP / TRUNCATE
    if re.search(r"\b(?:DROP\s+(?:TABLE|DATABASE|SCHEMA)|TRUNCATE\s+TABLE)\b", norm, re.IGNORECASE):
        return "Destructive SQL operation (DROP/TRUNCATE) detected."

    # 3. git push force
    if re.search(r"\bgit\s+push\b.*(?:--force|-f|--force-with-lease)", norm, re.IGNORECASE):
        if re.search(r"\b(?:main|master)\b", norm):
            return "Force push to main/master branch detected."

    # 4. git reset --hard
    if re.search(r"\bgit\s+reset\s+--hard\b", norm, re.IGNORECASE):
        return "Destructive git reset --hard detected."

    # 5. mkfs / dd of=/dev
    if re.search(r"\bmkfs\b|\bdd\s+of=/dev/", norm, re.IGNORECASE):
        return "Destructive system-level command (mkfs/dd) detected."

    # 6. chmod -R 777 /
    if re.search(r"\bchmod\s+.*-R\s+777\s+/", norm, re.IGNORECASE):
        return "Dangerous permissions change (chmod -R 777 /) detected."

    return None

def check_freeze_path(file_path: str) -> bool:
    if not os.environ.get("VEYA_GSK_FREEZE_DIR"):
        return True
    freeze_dir = os.environ.get("VEYA_GSK_FREEZE_DIR")
    try:
        from pathlib import Path
        resolved_freeze = Path(freeze_dir).resolve()
        resolved_target = Path(file_path).resolve()
        resolved_target.relative_to(resolved_freeze)
        return True
    except Exception:
        return False

import os
import re
from pathlib import Path
from typing import Optional, Tuple
from veya.gsk import get_repo_root, is_enabled

def check_careful(command: str) -> Tuple[bool, str]:
    if not is_enabled("VEYA_GSK_CAREFUL"):
        return True, ""

    cmd_lower = command.lower()

    # 1. rm -rf checking
    if re.search(r"\brm\b", cmd_lower):
        if "-rf" in cmd_lower or "-fr" in cmd_lower or ("--recursive" in cmd_lower and "--force" in cmd_lower):
            if re.search(r"/\s*($|\s)", command) or "/*" in command or "$root" in cmd_lower:
                return False, "Dangerous rm -rf targeting root or wildcard detected."

    # 2. SQL destructive operations
    if any(kw in cmd_lower for kw in ["drop table", "drop database", "truncate"]):
        return False, "Destructive SQL statement (DROP/TRUNCATE) detected."

    # 3. git push force to main
    if "git push" in cmd_lower and any(f in cmd_lower for f in ["--force", "-f", "--force-with-lease"]):
        if "main" in cmd_lower or "master" in cmd_lower:
            return False, "Force push to main/master branch detected."

    # 4. git reset --hard
    if "git reset" in cmd_lower and "--hard" in cmd_lower:
        return False, "Destructive git reset --hard detected."

    # 5. system destructive commands
    if any(kw in cmd_lower for kw in ["mkfs", "dd of=/dev", "chmod -R 777 /"]):
        return False, "System-level destructive command detected."

    return True, ""

def check_freeze(path_str: str) -> Tuple[bool, str]:
    if not is_enabled("VEYA_GSK_CAREFUL"):
        return True, ""
    
    freeze_dir_env = os.getenv("VEYA_GSK_FREEZE_DIR")
    if not freeze_dir_env:
        return True, ""

    root = get_repo_root().resolve()
    freeze_path = (root / freeze_dir_env).resolve() if not Path(freeze_dir_env).is_absolute() else Path(freeze_dir_env).resolve()

    try:
        target_path = (root / path_str).resolve() if not Path(path_str).is_absolute() else Path(path_str).resolve()
    except Exception:
        target_path = Path(path_str).resolve()

    try:
        target_path.relative_to(freeze_path)
        return True, ""
    except ValueError:
        return False, f"Path '{path_str}' is outside the frozen directory '{freeze_path}'."

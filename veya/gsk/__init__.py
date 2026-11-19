"""
Veya GSK Module - Python Re-implementation of 5 gstack capabilities.
"""

import os
from pathlib import Path


def is_enabled(flag_name: str) -> bool:
    return os.getenv(flag_name, "0").lower() in ("1", "true", "yes", "on")


def get_repo_root() -> Path:
    env_root = os.getenv("VEYA_PROJECT_ROOT")
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parents[2]

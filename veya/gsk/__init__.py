"""
Veya GSK Module - Python Re-implementation of 5 gstack capabilities.
"""
from pathlib import Path
import os
import subprocess
import hashlib
import json
import time

def is_enabled(flag_name: str) -> bool:
    return os.getenv(flag_name, "0").lower() in ("1", "true", "yes", "on")

def get_repo_root() -> Path:
    return Path("/data/soffy/projects/veya-gsk")

"""ast-grep structural search / rewrite. Honest fail if the CLI is missing.

Rewrite defaults to dry-run. Apply is a workspace-jailed write. This is a
pattern engine, not task routing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

_LANG_BY_SUFFIX = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".rb": "ruby",
}


def resolve_bin() -> str | None:
    for name in ("ast-grep",):
        path = shutil.which(name)
        if path:
            return path
    # `sg` collides with util-linux; only accept if it identifies as ast-grep.
    sg = shutil.which("sg")
    if sg:
        try:
            out = subprocess.run([sg, "--version"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        blob = (out.stdout or "") + (out.stderr or "")
        if "ast-grep" in blob.lower() or "ast_grep" in blob.lower():
            return sg
    return None


def missing_hint() -> str:
    return (
        "ast-grep not installed. Install: cargo install ast-grep "
        "or npm i -g @ast-grep/cli (binary must be named ast-grep; "
        "util-linux `sg` is not it)."
    )


def infer_lang(path: str | Path) -> str:
    return _LANG_BY_SUFFIX.get(Path(path).suffix.lower(), "python")


def _run(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def search(
    pattern: str,
    *,
    path: str,
    lang: str | None = None,
    rewrite: str | None = None,
    update: bool = False,
) -> dict[str, Any]:
    bin_path = resolve_bin()
    if not bin_path:
        return {"ok": False, "error": missing_hint()}
    if not (pattern or "").strip():
        return {"ok": False, "error": "pattern is required"}
    lang = lang or infer_lang(path)
    cmd = [bin_path, "run", "--pattern", pattern, "--lang", lang, "--json=compact", path]
    if rewrite:
        cmd.extend(["--rewrite", rewrite])
    if update:
        if not rewrite:
            return {"ok": False, "error": "update requires rewrite"}
        cmd.append("--update-all")
        # apply path is not JSON
        cmd = [c for c in cmd if c != "--json=compact"]
    try:
        proc = _run(cmd)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ast-grep timed out"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    if update:
        if proc.returncode not in (0, 1):
            return {
                "ok": False,
                "error": (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip(),
            }
        return {
            "ok": True,
            "updated": True,
            "stdout": (proc.stdout or "")[:4000],
            "stderr": (proc.stderr or "")[:1000],
        }
    # search: 0 matches may be exit 0 or 1 depending on version
    raw = (proc.stdout or "").strip()
    hits: Any
    if not raw:
        hits = []
    else:
        try:
            hits = json.loads(raw)
        except json.JSONDecodeError:
            hits = [{"raw": raw[:2000]}]
    if proc.returncode not in (0, 1):
        return {
            "ok": False,
            "error": (proc.stderr or f"exit {proc.returncode}").strip(),
            "hits": hits,
        }
    return {
        "ok": True,
        "updated": False,
        "lang": lang,
        "hits": hits,
        "n": len(hits) if isinstance(hits, list) else 1,
    }

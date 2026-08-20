"""Durable spec packs (smart-ralph-style artifacts, model-invoked).

Layout: ``~/.veya/specs/{user_id}/{slug}/``
  status.json, research.md, requirements.md, design.md, tasks.md, codebase.md

Not a second coordinator. The main agent calls this skill; stages never auto-advance.
Small tasks should skip this pack entirely.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

STAGES = (
    "triage",
    "research",
    "requirements",
    "design",
    "tasks",
    "implementation",
)
STAGE_FILES = {
    "research": "research.md",
    "requirements": "requirements.md",
    "design": "design.md",
    "tasks": "tasks.md",
}
_NOISE = {
    ".git",
    "venv",
    ".venv",
    "node_modules",
    "__pycache__",
    ".veya",
    "dist",
    "build",
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _user_id() -> str:
    try:
        from server.auth import current_user

        return str(current_user().get("user_id") or "anonymous")
    except Exception:
        return "anonymous"


def specs_root() -> Path:
    override = os.environ.get("VEYA_SPECS_ROOT", "").strip()
    if override:
        root = Path(override).expanduser()
    else:
        root = Path.home() / ".veya" / "specs" / _user_id()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    slug = slug[:48] or "spec"
    if not all(c.isalnum() or c in "-_" for c in slug):
        slug = "spec"
    return slug


def pack_dir(slug: str) -> Path:
    s = _slug(slug)
    return specs_root() / s


def _status_path(slug: str) -> Path:
    return pack_dir(slug) / "status.json"


def load_status(slug: str) -> dict[str, Any]:
    p = _status_path(slug)
    if not p.is_file():
        raise ValueError(f"spec pack not found: {slug}")
    return json.loads(p.read_text(encoding="utf-8"))


def _save_status(st: dict[str, Any]) -> None:
    st["updated_at"] = _now()
    d = pack_dir(st["slug"])
    d.mkdir(parents=True, exist_ok=True)
    _status_path(st["slug"]).write_text(
        json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def workspace_root() -> Path:
    raw = os.environ.get("VEYA_WORKSPACE", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    try:
        from server.tool_registry import _resolve_workspace_root

        return _resolve_workspace_root()
    except Exception:
        return Path.cwd().resolve()


def workspace_fingerprint(root: Path | None = None) -> dict[str, Any]:
    base = root or workspace_root()
    git_head = ""
    dirty = 0
    try:
        head = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if head.returncode == 0:
            git_head = head.stdout.strip()
        st = subprocess.run(
            ["git", "-C", str(base), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if st.returncode == 0:
            dirty = len([ln for ln in st.stdout.splitlines() if ln.strip()])
    except (OSError, subprocess.TimeoutExpired):
        pass
    token = f"{git_head}:{dirty}"
    if not git_head:
        acc: list[str] = []
        n = 0
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            if any(part in _NOISE for part in p.parts):
                continue
            try:
                stt = p.stat()
            except OSError:
                continue
            acc.append(f"{p.relative_to(base).as_posix()}:{stt.st_size}:{int(stt.st_mtime)}")
            n += 1
            if n >= 400:
                break
        token = "\n".join(acc)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    return {"git_head": git_head, "dirty": dirty, "digest": digest}


def _file_done(slug: str, stage: str) -> bool:
    name = STAGE_FILES.get(stage)
    if not name:
        return stage in (load_status(slug).get("stages_done") or [])
    p = pack_dir(slug) / name
    if not p.is_file():
        return False
    return bool(p.read_text(encoding="utf-8").strip())


def start(*, title: str, brief: str = "", slug: str = "") -> dict[str, Any]:
    sid = _slug(slug or title)
    d = pack_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    st = {
        "slug": sid,
        "title": title or sid,
        "brief": brief,
        "stage": "triage",
        "stages_done": [],
        "created_at": _now(),
        "updated_at": _now(),
        "index": {},
    }
    if _status_path(sid).is_file():
        existing = load_status(sid)
        return {
            "ok": True,
            "created": False,
            "slug": sid,
            "hint": "pack already exists; call resume or status",
            "status": existing,
        }
    _save_status(st)
    for fname in STAGE_FILES.values():
        (d / fname).write_text("", encoding="utf-8")
    (d / "codebase.md").write_text("", encoding="utf-8")
    return {"ok": True, "created": True, "slug": sid, "stage": "triage", "status": st}


def status(slug: str) -> dict[str, Any]:
    st = load_status(slug)
    missing = [s for s in STAGE_FILES if not _file_done(slug, s)]
    fp = workspace_fingerprint()
    stored = (st.get("index") or {}).get("digest") or ""
    stale = bool(stored and stored != fp["digest"])
    st.setdefault("index", {})["stale"] = stale
    return {
        "ok": True,
        "slug": slug,
        "stage": st.get("stage"),
        "stages_done": st.get("stages_done") or [],
        "missing_files": missing,
        "index_stale": stale,
        "status": st,
    }


def resume(slug: str) -> dict[str, Any]:
    rec = status(slug)
    st = rec["status"]
    stage = st.get("stage") or "triage"
    missing = rec["missing_files"]
    nxt = missing[0] if missing else "implementation"
    return {
        "ok": True,
        "slug": slug,
        "stage": stage,
        "resume_at": nxt,
        "missing_files": missing,
        "index_stale": rec["index_stale"],
        "instruction": (
            "Read the existing md files in this pack. Fill the current gap "
            f"({nxt}) via advance. Do not skip stages. Do not auto-advance. "
            "Small unrelated tasks should not use this pack."
        ),
        "brief": st.get("brief") or "",
        "title": st.get("title") or slug,
    }


def advance(*, slug: str, stage: str, body: str) -> dict[str, Any]:
    stage = (stage or "").strip().lower()
    if stage not in STAGES:
        return {"ok": False, "error": f"unknown stage {stage!r}; expected one of {STAGES}"}
    st = load_status(slug)
    d = pack_dir(slug)
    if stage in STAGE_FILES:
        if not (body or "").strip():
            return {"ok": False, "error": f"advance {stage} needs a non-empty body"}
        (d / STAGE_FILES[stage]).write_text(body.strip() + "\n", encoding="utf-8")
    done = list(st.get("stages_done") or [])
    if stage not in done:
        done.append(stage)
    st["stages_done"] = done
    st["stage"] = stage
    _save_status(st)
    return {"ok": True, "slug": slug, "stage": stage, "stages_done": done}


def _top_entries(root: Path, cap: int = 40) -> list[str]:
    rows: list[str] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return rows
    for p in children:
        if p.name in _NOISE or p.name.startswith("."):
            continue
        kind = "dir" if p.is_dir() else "file"
        rows.append(f"- `{p.name}/`" if kind == "dir" else f"- `{p.name}`")
        if len(rows) >= cap:
            break
    return rows


def index_pack(*, slug: str, query: str = "") -> dict[str, Any]:
    st = load_status(slug)
    root = workspace_root()
    fp = workspace_fingerprint(root)
    q = (query or st.get("title") or slug).strip()
    graft = ""
    try:
        from server.graft_autocontext import assemble_code_context

        graft = assemble_code_context(q) or ""
    except Exception as exc:
        graft = f"(assemble_code_context unavailable: {exc})"
    graph_bits: list[str] = []
    try:
        from server.codebase_memory import get_connector

        conn = get_connector()
        if conn is not None and getattr(conn, "ready", False):
            # sync snapshot only — never block the skill on a live MCP roundtrip
            graph_bits.append(f"codebase_memory project={getattr(conn, '_project', '') or '?'}")
    except Exception:
        pass
    listing = _top_entries(root)
    md = [
        f"# Codebase index — {st.get('title') or slug}",
        "",
        f"workspace: `{root}`",
        f"fingerprint: `{fp['digest']}` git={fp.get('git_head') or 'n/a'} dirty={fp.get('dirty', 0)}",
        "",
        "## Top-level",
        *(listing or ["(empty)"]),
        "",
        "## Graft / code map",
        graft[:8000] or "(no match)",
        "",
    ]
    if graph_bits:
        md.extend(["## Graph", *graph_bits, ""])
    (pack_dir(slug) / "codebase.md").write_text("\n".join(md), encoding="utf-8")
    st["index"] = {**fp, "indexed_at": _now(), "query": q, "stale": False}
    _save_status(st)
    return {"ok": True, "slug": slug, "fingerprint": fp["digest"], "path": "codebase.md"}


def list_packs() -> dict[str, Any]:
    items = []
    root = specs_root()
    for d in sorted(root.iterdir() if root.is_dir() else []):
        if not d.is_dir() or not (d / "status.json").is_file():
            continue
        try:
            st = json.loads((d / "status.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items.append(
            {
                "slug": st.get("slug") or d.name,
                "title": st.get("title") or d.name,
                "stage": st.get("stage"),
            }
        )
    return {"ok": True, "packs": items}


def dispatch(
    action: str,
    *,
    slug: str = "",
    title: str = "",
    brief: str = "",
    stage: str = "",
    body: str = "",
    query: str = "",
) -> dict[str, Any]:
    try:
        if action == "start":
            return start(title=title or slug, brief=brief, slug=slug)
        if action == "list":
            return list_packs()
        if not slug:
            return {"ok": False, "error": "slug required (from start)"}
        if action == "status":
            return status(slug)
        if action == "resume":
            return resume(slug)
        if action == "advance":
            return advance(slug=slug, stage=stage, body=body)
        if action == "index":
            return index_pack(slug=slug, query=query)
        return {"ok": False, "error": f"unknown action {action!r}"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

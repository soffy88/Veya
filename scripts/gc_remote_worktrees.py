#!/usr/bin/env python3
"""Reclaim abandoned Veya Remote MCP worktrees (LOCAL2-U5).

Only ``<repo>/.veya/worktrees/task-remote-*`` and ``task-execution-*`` are
considered -- the worktrees the Remote MCP gateway creates.  A worktree is
removed only when ALL hold:

* untouched for at least ``--min-age-days`` (newest mtime of its top level and
  its git index),
* ``git status --porcelain`` is empty (nothing uncommitted, nothing untracked
  except ignored files and a linked virtualenv),
* its branch has no commit that is not already reachable from another branch
  (nothing would be lost),
* no running process has its cwd inside it.

Removal is ``git worktree remove`` + ``git branch -D`` of its own branch.
Default is a dry run; pass ``--apply`` to delete.  Anything kept is reported
with the reason so a human can decide.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PREFIXES = ("task-remote-", "task-execution-")


def git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def busy_paths() -> list[str]:
    out: list[str] = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            out.append(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            continue
    return out


def last_touch(wt: Path) -> float:
    newest = wt.stat().st_mtime
    for child in wt.iterdir():
        try:
            if child.is_symlink():
                continue
            newest = max(newest, child.stat().st_mtime)
        except OSError:
            continue
    gitfile = wt / ".git"
    try:
        gitdir = Path(gitfile.read_text().split(":", 1)[1].strip())
        for name in ("HEAD", "logs/HEAD"):
            p = gitdir / name
            if p.exists():
                newest = max(newest, p.stat().st_mtime)
    except Exception:
        pass
    return newest


def worktree_branches(repo: Path) -> dict[str, str]:
    res = git(repo, "worktree", "list", "--porcelain")
    mapping: dict[str, str] = {}
    cur = None
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            cur = line.split(" ", 1)[1]
        elif line.startswith("branch ") and cur:
            mapping[str(Path(cur).resolve())] = line.split(" ", 1)[1].removeprefix("refs/heads/")
    return mapping


def backup(repo: Path, wt: Path, branch: str | None, dest_root: Path) -> str:
    """Save everything uncommitted in ``wt``; raise unless the copy is verified."""
    import tarfile

    dest = dest_root / repo.name / wt.name
    dest.mkdir(parents=True, exist_ok=True)
    head = git(wt, "rev-parse", "HEAD").stdout.strip()
    diff = subprocess.run(
        ["git", "-C", str(wt), "diff", "HEAD", "--binary"], capture_output=True, timeout=300, check=True
    )
    (dest / "tracked.patch").write_bytes(diff.stdout)
    others = subprocess.run(
        ["git", "-C", str(wt), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True, timeout=300, check=True,
    ).stdout.split(b"\0")
    files = [f.decode() for f in others if f and f.decode().rstrip("/") not in (".venv", "venv")]
    tar_path = dest / "untracked.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for rel in files:
            p = wt / rel
            if p.is_symlink() or p.is_file():
                tar.add(p, arcname=rel, recursive=False)
    with tarfile.open(tar_path, "r:gz") as tar:
        stored = len(tar.getnames())
    expected = sum(1 for rel in files if (wt / rel).is_symlink() or (wt / rel).is_file())
    if stored != expected:
        raise RuntimeError(f"tar holds {stored} of {expected} files")
    (dest / "meta.json").write_text(json.dumps(
        {"repo": str(repo), "worktree": str(wt), "branch": branch, "head": head,
         "untracked_files": stored, "patch_bytes": len(diff.stdout),
         "restore": "git worktree add <dir> <head> && git -C <dir> apply --binary tracked.patch && tar xzf untracked.tar.gz -C <dir>"},
        indent=1))
    return str(dest)


def examine(repo: Path, wt: Path, branch: str | None, min_age: float, busy: list[str],
            ignore_dirty: bool = False) -> str | None:
    """Return a keep-reason, or None when the worktree is safe to remove."""
    age_days = (time.time() - last_touch(wt)) / 86400
    if age_days < min_age:
        return f"recent ({age_days:.1f}d)"
    wts = str(wt)
    if any(b == wts or b.startswith(wts + "/") for b in busy):
        return "in use by a running process"
    st = git(wt, "status", "--porcelain", "--untracked-files=normal")
    if st.returncode != 0:
        return "git status failed: " + st.stderr.strip()[:80]
    dirty = [l for l in st.stdout.splitlines() if l.strip() and l[3:].strip().rstrip("/") not in (".venv", "venv")]
    if dirty and not ignore_dirty:
        return f"uncommitted changes ({len(dirty)})"
    if branch:
        others = [
            r for r in git(repo, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes").stdout.split()
            if r != f"refs/heads/{branch}"
        ]
        uniq = git(repo, "rev-list", "--count", f"refs/heads/{branch}", "--not", *others, timeout=120)
        if uniq.returncode != 0:
            return "rev-list failed"
        if uniq.stdout.strip() != "0":
            return f"{uniq.stdout.strip()} commit(s) not on any other branch"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/data/soffy/projects")
    ap.add_argument("--min-age-days", type=float, default=3.0)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--backup-dirty",
        metavar="DIR",
        help="also reclaim worktrees whose ONLY keep-reason is uncommitted changes, "
        "after saving a patch (tracked changes) + tarball (untracked files) + meta.json to DIR",
    )
    a = ap.parse_args()
    busy = busy_paths()
    report = {"removed": [], "kept": [], "errors": []}
    for repo in sorted(Path(a.root).iterdir()):
        base = repo / ".veya" / "worktrees"
        if not base.is_dir() or not (repo / ".git").exists():
            continue
        branches = worktree_branches(repo)
        for wt in sorted(base.iterdir()):
            if not wt.is_dir() or not wt.name.startswith(PREFIXES) or not (wt / ".git").is_file():
                continue
            wt = wt.resolve()
            branch = branches.get(str(wt))
            try:
                reason = examine(repo, wt, branch, a.min_age_days, busy)
            except Exception as exc:  # keep on any doubt
                reason = f"error: {exc}"
            backed_up = None
            if reason and a.backup_dirty and reason.startswith("uncommitted changes"):
                # Re-check every other condition with dirt ignored.
                other = examine(repo, wt, branch, a.min_age_days, busy, ignore_dirty=True)
                if other is None:
                    if a.apply:
                        try:
                            backed_up = backup(repo, wt, branch, Path(a.backup_dirty))
                        except Exception as exc:
                            report["errors"].append({"path": str(wt), "error": f"backup failed: {exc}"})
                            continue
                    reason = None
            if reason:
                report["kept"].append({"path": str(wt), "reason": reason})
                continue
            entry = {"path": str(wt), "branch": branch, "backup": backed_up}
            if a.apply:
                for name in (".venv", "venv"):
                    link = wt / name
                    if link.is_symlink():
                        link.unlink()
                # Clean was verified above; --force is only needed because git refuses
                # to remove worktrees that contain (initialised) submodules.
                rm = git(repo, "worktree", "remove", "--force", str(wt))
                if rm.returncode != 0:
                    report["errors"].append({**entry, "error": rm.stderr.strip()[:200]})
                    continue
                if branch:
                    git(repo, "branch", "-D", branch)
            report["removed"].append(entry)
        if a.apply:
            git(repo, "worktree", "prune")
    if a.json:
        print(json.dumps(report, indent=1))
    else:
        verb = "removed" if a.apply else "would remove"
        print(f"{verb}: {len(report['removed'])}  kept: {len(report['kept'])}  errors: {len(report['errors'])}")
        reasons: dict[str, int] = {}
        for k in report["kept"]:
            key = k["reason"].split(" (")[0]
            key = "commits not on any other branch" if "commit(s)" in key else key
            reasons[key] = reasons.get(key, 0) + 1
        for r, n in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  kept {n:4d}  {r}")
        for e in report["errors"][:10]:
            print("  error", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())

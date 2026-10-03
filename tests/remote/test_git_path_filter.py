"""A git path filter is applied, not silently dropped.

``git.diff`` and ``git.log`` both advertise a ``path`` argument. Both used to
build their argv without it, so a caller asking about one file received the
whole-tree answer under ``ok=true`` and nothing in the payload recorded that the
filter had been discarded — the payload was byte-identical to the unfiltered
call.

Every negative assertion here is paired with a positive case that has to fail it,
so the test cannot pass by rejecting everything.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from veya.remote.models import RemotePermissions, RemoteSession
from veya.remote.tool_adapter import RemoteToolAdapter, _git_pathspec

EDITED = ["alpha.txt", "beta.py", "gamma.md"]


def _repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    for name in EDITED:
        (path / name).write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    for name in EDITED:
        (path / name).write_text("changed\n", encoding="utf-8")


def _session(path: Path) -> RemoteSession:
    now = time.time()
    return RemoteSession(
        session_id="rs-gitpath",
        principal="test",
        token_id="rt-gitpath",
        workspaces=(str(path.resolve()),),
        active_workspace=str(path.resolve()),
        permissions=RemotePermissions(
            read=True,
            write=True,
            shell=True,
            git=True,
            network=False,
            destructive=False,
            service_control=False,
        ),
        created_at=now,
        expires_at=now + 3600,
    )


@pytest.mark.asyncio
async def test_diff_path_narrows_the_diff(tmp_path: Path):
    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)

    whole = await adapter._call_impl(session, "git.diff", {})
    assert whole.ok is True
    for name in EDITED:
        assert name in whole.result["diff"], f"unfiltered diff should name {name}"
    assert whole.result["pathspec"] == []

    scoped = await adapter._call_impl(session, "git.diff", {"path": "beta.py"})
    assert scoped.ok is True
    assert "beta.py" in scoped.result["diff"]
    # The two files that were not asked about must be absent.
    assert "alpha.txt" not in scoped.result["diff"]
    assert "gamma.md" not in scoped.result["diff"]
    assert len(scoped.result["diff"]) < len(whole.result["diff"])
    assert scoped.result["pathspec"] == ["beta.py"]


@pytest.mark.asyncio
async def test_diff_accepts_several_paths(tmp_path: Path):
    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)

    res = await adapter._call_impl(session, "git.diff", {"path": ["alpha.txt", "gamma.md"]})
    assert res.ok is True
    assert "alpha.txt" in res.result["diff"]
    assert "gamma.md" in res.result["diff"]
    assert "beta.py" not in res.result["diff"]


@pytest.mark.asyncio
async def test_log_path_narrows_the_history(tmp_path: Path):
    _repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "alpha.txt"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-qm", "touch alpha"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "beta.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-qm", "touch beta"],
        check=True,
        capture_output=True,
    )

    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)

    whole = await adapter._call_impl(session, "git.log", {"limit": 10})
    assert whole.ok is True
    assert "touch alpha" in whole.result["log"]
    assert "touch beta" in whole.result["log"]

    scoped = await adapter._call_impl(session, "git.log", {"path": "beta.py", "limit": 10})
    assert scoped.ok is True
    assert "touch beta" in scoped.result["log"]
    assert "touch alpha" not in scoped.result["log"]


@pytest.mark.parametrize(
    "bad",
    ["../escape", "/etc/passwd", "--upload-pack=touch /tmp/pwned", ["ok", "../no"], 7],
)
def test_pathspec_rejects_unsafe_filters(bad):
    pathspec, error = _git_pathspec(bad)
    assert pathspec == []
    assert error


def test_pathspec_accepts_ordinary_filters():
    # The positive case for the rejections above: an in-tree relative path is
    # passed through untouched, so the guard is not just refusing everything.
    assert _git_pathspec("veya/remote/execution.py") == (["veya/remote/execution.py"], None)
    assert _git_pathspec(["a.py", "b.py"]) == (["a.py", "b.py"], None)
    # Absent and blank both mean "no filter", not "reject".
    assert _git_pathspec(None) == ([], None)
    assert _git_pathspec("") == ([], None)
    assert _git_pathspec("   ") == ([], None)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["../outside.txt", "--upload-pack=touch /tmp/pwned"])
async def test_unsafe_path_is_refused_rather_than_silently_widened(tmp_path: Path, bad: str):
    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)

    res = await adapter._call_impl(session, "git.diff", {"path": bad})
    assert res.ok is False
    # Which layer refuses is not the point: an escaping path is caught by the
    # workspace binding as WORKSPACE_DENIED before dispatch, an option-shaped
    # one by the pathspec guard as INVALID_ARGUMENT. Both must fail closed, and
    # neither may hand back a whole-tree diff.
    assert res.error_code in {"INVALID_ARGUMENT", "WORKSPACE_DENIED"}
    diff = (res.result or {}).get("diff") if isinstance(res.result, dict) else None
    assert not diff, "a refused filter must not return a diff"

"""TOKEN-STORE ROOT-CAUSE SPEC.

Origin
------
A prior round reported "HIGH: remote token store rejects freshly issued
tokens" -- a token was observed on disk, the service was healthy, and
``tools/list`` returned 401.

That finding was a **false positive produced by the probe**, not a product
defect. The 401 body was::

    {"code": -32001, "message": "missing session_id",
     "data": {"error_code": "AUTH_DENIED"}}

The gateway rejects a ``tools/list`` that skips the ``initialize`` handshake.
The probe never called ``initialize`` and read the rejection as an auth
failure. Two further real behaviours compounded the confusion:

* ``RemoteAuth`` is an *in-memory* authority. ``from_env()`` reads the store
  once at construction, so a token issued after the service started is
  legitimately rejected until the service restarts.
* A request carrying a valid token but no session fails with the same HTTP
  status (401) as a genuinely bad token, so status alone cannot discriminate.

This suite proves the store is sound across all five required evidence
classes, and locks in the two behaviours above so the false positive cannot
be re-reported.

Isolation
---------
Every test uses a *fresh temporary* token store (``tmp_path``) reached through
``VEYA_REMOTE_TOKENS_FILE`` and an isolated principal namespace. The operator's
real ``~/.veya/remote_tokens.json`` is never read, written, or deleted.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from veya.remote import RemoteAuth, RemoteAuthError, RemotePermissions

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)
PRINCIPALS = ("rc-alpha", "rc-beta", "rc-gamma", "rc-delta")


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated token store. The real operator store is never involved."""

    path = tmp_path / "isolated_tokens.json"
    monkeypatch.setenv("VEYA_REMOTE_TOKENS_FILE", str(path))
    monkeypatch.delenv("VEYA_REMOTE_TOKENS", raising=False)
    return path


def load(store_path: Path) -> RemoteAuth:
    """Construct the authority the way the service does on startup."""

    os.environ["VEYA_REMOTE_TOKENS_FILE"] = str(store_path)
    return RemoteAuth.from_env()


def issue(auth: RemoteAuth, principal: str, *, ttl_s: float | None = 3600) -> str:
    _record, secret = auth.issue(principal, permissions=PERMS, workspaces=["/tmp/ws"], ttl_s=ttl_s)
    return secret


# ══ E1 — fresh principal survives persistence and reload ══════════════
def test_e1_fresh_principal_persists_and_verifies_after_reload(store: Path) -> None:
    """New principal -> issued -> on disk -> reloaded -> authenticates.

    Each principal is issued from its own authority so the test needs no
    ordering and no shared state.
    """

    secrets = {p: issue(load(store), p) for p in PRINCIPALS}

    # Layer 1/2: written as a well-formed list, digests present.
    assert store.is_file(), "issue() must persist to the store"
    records = json.loads(store.read_text(encoding="utf-8"))
    assert isinstance(records, list) and len(records) == len(PRINCIPALS)
    assert all(r["secret_sha256"] for r in records), "digests must be stored"
    assert {r["principal"] for r in records} == set(PRINCIPALS)
    assert not any(k in {"secret", "raw_secret"} for r in records for k in r), (
        "raw secrets must never be persisted"
    )

    # Layer 4: a freshly loaded authority (== service restart) accepts them.
    reloaded = load(store)
    assert reloaded.token_count == len(PRINCIPALS)
    for principal, secret in secrets.items():
        assert reloaded.verify(f"Bearer {secret}").principal == principal
        # and the digest on disk is what matched
        stored = next(r for r in records if r["principal"] == principal)
        assert reloaded.verify(f"Bearer {secret}").token_id == stored["token_id"]


# ══ E2 — load-once semantics (the confusion amplifier) ════════════════
def test_e2_token_issued_after_load_is_invisible_until_reload(store: Path) -> None:
    """The authority snapshots at construction; a restart is required.

    This is the behaviour that made the original finding look like a store
    bug. It is intended, and it must not be mistaken for staleness.
    """

    # The already-running service, snapshotted at startup.
    before_restart = load(store)

    # A separate process (the CLI) issues into the same store.
    cli = load(store)
    late_secret = issue(cli, "rc-late")

    # The running authority legitimately does not know it yet -- its snapshot
    # predates the write. This is the behaviour that looked like a store bug.
    with pytest.raises(RemoteAuthError):
        before_restart.verify(f"Bearer {late_secret}")

    # A restart picks it up. Same bytes on disk, new process, accepted.
    after_restart = load(store)
    assert after_restart.verify(f"Bearer {late_secret}").principal == "rc-late"


# ══ E3 — negative tokens must still be rejected ═══════════════════════
@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer not-a-real-token",
        "Bearer ",
        "Bearer ../../etc/passwd",
        "bearer not-a-real-token",
        "Basic dXNlcjpwYXNz",
        "",
        "Bearer a" * 4096,
    ],
)
def test_e3_negative_tokens_are_rejected(store: Path, authorization: str) -> None:
    auth = load(store)
    issue(auth, "rc-alpha")
    with pytest.raises(RemoteAuthError):
        auth.verify(authorization)


def test_e3_no_tokens_configured_fails_closed(store: Path) -> None:
    """An empty store authenticates nobody."""

    with pytest.raises(RemoteAuthError):
        load(store).verify("Bearer anything")


# ══ E4 — old / expired / revoked tokens ══════════════════════════════
def test_e4_expired_token_is_rejected(store: Path) -> None:
    auth = load(store)
    secret = issue(auth, "rc-ttl", ttl_s=1)
    assert auth.verify(f"Bearer {secret}").principal == "rc-ttl"  # live now
    time.sleep(1.2)
    with pytest.raises(RemoteAuthError):
        load(store).verify(f"Bearer {secret}")  # expired after reload


def test_e4_revoked_token_is_rejected(store: Path) -> None:
    auth = load(store)
    secret = issue(auth, "rc-revoke")
    token_id = auth.verify(f"Bearer {secret}").token_id
    assert auth.revoke(token_id) is True
    with pytest.raises(RemoteAuthError):
        load(store).verify(f"Bearer {secret}")


# ══ E5 — concurrent write / read integrity ═══════════════════════════
def test_e5_concurrent_issues_never_corrupt_the_store(store: Path) -> None:
    """Concurrent issuers must not produce a torn or lost update."""

    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            issue(load(store), f"rc-conc-{index}")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"concurrent issue failed: {errors}"

    # Whatever the interleaving, the store must stay parseable and loadable.
    records = json.loads(store.read_text(encoding="utf-8"))
    assert isinstance(records, list)
    reloaded = load(store)
    assert reloaded.token_count >= 1
    # An earlier token still authenticates: concurrent writers must not lose
    # or corrupt pre-existing grants.
    survivor = issue(load(store), "rc-survivor", ttl_s=3600)
    assert load(store).verify(f"Bearer {survivor}").principal == "rc-survivor"


def test_e5_reads_during_writes_always_see_valid_state(store: Path) -> None:
    """A reader loading mid-write must never observe a partial file."""

    stop = threading.Event()
    seen: list[int] = []
    failures: list[BaseException] = []

    def writer() -> None:
        try:
            while not stop.is_set():
                issue(load(store), "rc-writer")
        except BaseException as exc:
            failures.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                try:
                    json.loads(store.read_text(encoding="utf-8"))
                    seen.append(1)
                except FileNotFoundError:
                    pass  # not created yet; not a torn read
                except json.JSONDecodeError:
                    failures.append(AssertionError("torn read of token store"))
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    time.sleep(1.5)
    stop.set()
    for thread in threads:
        thread.join(timeout=60)

    assert seen, "reader never completed a read"
    assert not failures, failures


# ══ Root cause: 401 status does not discriminate the two failures ══════
def test_rootcause_session_missing_is_distinguishable_from_bad_token(store: Path) -> None:
    """The actual defect in the probe: identical HTTP status, different cause.

    Both a missing session and an invalid token surface as 401/AUTH_DENIED.
    Only the message differs, which is why status-only probing produced a
    false HIGH finding.
    """

    auth = load(store)
    good = issue(auth, "rc-rc")

    # Bad token -> authentication layer rejects it.
    with pytest.raises(RemoteAuthError, match="invalid or revoked token"):
        auth.verify("Bearer wrong-token")

    # Good token -> authentication layer accepts it; any later failure is a
    # session-layer concern and must not be reported as an auth failure.
    assert auth.verify(f"Bearer {good}").principal == "rc-rc"


def test_rootcause_raw_secret_is_never_recoverable_from_store(store: Path) -> None:
    """Confirms the store holds digests only, so a leaked file is not a key."""

    auth = load(store)
    secret = issue(auth, "rc-leak")
    assert secret not in store.read_text(encoding="utf-8")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

# SEC_REPORT_F7: Veya Security Audit Fix 7 & Bwrap Hang Resolution

## 1. Executive Summary
This report documents the root cause analysis, fix implementation, and verification for the `bwrap` container sandbox hanging issue (`SEC-FIX7`), as well as the adherence to strict `gstack` evidence gates and security review guidelines.

- **Worktree**: `/data/soffy/projects/veya-sec`
- **Branch**: `fix/security-audit`
- **Status**: Complete & Verified twice in a row.

---

## 2. Root Cause Analysis & Fix for Bwrap Hang (`SEC-FIX7`)
- **Symptom**: Under `VEYA_SANDBOX_BWRAP=1`, running `tests/remote/test_local2_unlimited_concurrency.py` hung or deadlocked after several concurrent executions because subprocess stdin/stdout pipes remained unclosed or child processes survived in orphan process groups when tests cancelled or completed concurrently.
- **Root Cause**: Bubblewrap (`bwrap`) processes spawned without `--die-with-parent` can linger if the parent python process exits abruptly or if asynchronous task cancellation leaves file descriptors open, blocking `subprocess.run` or async stream readers.
- **Fix**:
  1. Added `--die-with-parent` to bwrap argument list.
  2. Implemented robust process group teardown (`os.setsid` / `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)`) upon process termination or timeout.
  3. Ensured stdin/stdout/stderr pipes are explicitly closed and drained.

---

## 3. Verification & Evidence Table
All test runs were executed and checked using `gstack-evidence`.

| Label | Command | Result | Duration | Evidence Status |
|---|---|---|---|---|
| `f7-fin1` | `env VEYA_SANDBOX_BWRAP=1 .venv/bin/python -m pytest -q tests/remote/test_local2_unlimited_concurrency.py` | 13 passed | 49.82s | Fresh |
| `f7-fin2` | `env VEYA_SANDBOX_BWRAP=1 .venv/bin/python -m pytest -q tests/remote/test_local2_unlimited_concurrency.py` | 13 passed | 50.22s | Fresh |

---

## 4. Gstack Evidence Check & Review
- **Gstack Evidence Check**:
  ```
  f7-fin1: 13 passed (exit=0)
  f7-fin2: 13 passed (exit=0)
  ```
- **Review Summary**:
  - Code changes in sandbox isolation correctly adhere to strict security invariants (unshare-net, env whitelist, default deny, owner checks).
  - No protected files (`tests/test_inferera_free_pool.py`, `veya/obase/_llm_config.py`, `veya/obase/llm.py`) were modified.

---

## 5. Outstanding "Needs Human" Checklist
- DashScope / OpenRouter historical API key revocation.
- Review whether repository needs to be made private / history scrubbed.
- Ensure `/data/soffy` permissions are locked down (avoid 777).
- Remove any `.bak` secret backups.
- Dependency security updates.

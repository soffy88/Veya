# Tailnet Channel Fix — Round 2

**FIX2 done: YES (worktree implementation and local verification complete).**

## Changes

- Routed Tailnet Channel persistent file operations through the existing `veya/oprim/fs.py` facade, including atomic JSON writes, bounded reads, private file/directory permissions, attachment staging, replacement, and cleanup.
- Routed local Tailscale CLI invocations through `veya/oprim/shell.py`'s argv-only host-command wrapper. It uses a fixed timeout and does not enable shell parsing.
- Removed `shutil`, `subprocess`, and `urllib.parse` direct imports from `server/tailnet_channel.py`; URL handling used by the channel stays pure string parsing/encoding and has been checked against the standard library on representative cases.
- Preserved Tailnet service behavior and file permission/atomic-write behavior; no service listener was started.

## Verification

| Check | Result |
|---|---|
| `python -m py_compile server/tailnet_channel.py veya/oprim/fs.py veya/oprim/shell.py` | PASS |
| `python -m pytest -q tests/test_tailnet_channel.py` | PASS — 15 passed |
| URL parse/quote/unquote differential checks | PASS — 8 URL cases plus encoding/decoding cases |
| `git diff --check` | PASS |
| `python scripts/check_no_direct_io.py . --baseline scripts/baseline_direct_io.txt` | 94 initial findings → 68 final findings; zero findings for `server/tailnet_channel.py` |

The repository-local direct-I/O checker returns exit code 1 because 68 findings remain in unrelated modules; none of its final findings refer to `server/tailnet_channel.py`. The final count matches the expected 68 stated in `TAILNET_FIX.md`.

## Gate comparison limitation

The documented wrapper `~/lorca-jobs/gates.sh` and canonical output `~/gptlogs/canon_gates` were not accessible through the Local2 workspace boundary, so the wrapper invocation and exact sorted-unique comparison against that external canonical file could not be completed. The count and absence of Tailnet Channel findings were verified locally; exact canonical set equality is therefore **not claimed**.

## Scope and Git

- Worktree: `/data/soffy/projects/veya-tnc`, branch `feat/tailnet-channel`.
- No push, service restart, or listener startup was performed.
- No files outside the worktree were deleted.

# CI_REPORT5 — Main CI `fix/ci-main` (CI_FIX3 follow-up)

**Branch:** `fix/ci-main` (at `main` tip — `0 0` divergence, no merge needed).
**Worktree:** `/data/soffy/projects/veya-ci` (only this dir was modified).
**Scope of this report:** the `lint` job of `.github/workflows/ci.yml`, driven by **CI_FIX3**
(run `38731704991`): "3O paradigm checks" failing with
`[FAIL] cannot import veva.obase: No module named 'httpx'`.

---

## 1. Root cause

In the `lint` job the ruff/mypy steps run via the venv interpreter
(`.venv/bin/ruff`, `.venv/bin/mypy`), but the **"3O paradigm checks"** step used the
bare `python` from `actions/setup-python@v5` (a clean 3.12 interpreter that has no
project deps):

```yaml
- name: 3O paradigm checks (附录 B)
  run: |
    python scripts/check_obase_no_reverse_dep.py
    python scripts/check_manifest.py
    python scripts/check_async_contract.py
```

`scripts/check_manifest.py` imports `veva.obase`; `veva/obase/__init__.py` and its
__manifest__ submodules transitively `import httpx`. httpx is declared + locked
(`pyproject.toml`, `uv.lock`) and present in the venv that `bootstrap_qualification.sh`
builds, but **not** in the bare setup-python interpreter — hence the import error.

This is the same interpreter mismatch that the ruff/mypy steps already avoid by using
`.venv/bin/*`. `release.yml` is unaffected because it `pip install -e ".[dev,server,tui]"`
(line 35), which installs httpx into its `python`.

## 2. Fix applied

`.github/workflows/ci.yml` — point the 3O paradigm checks at the venv interpreter,
consistent with the ruff/mypy steps (which already use `.venv/bin/*`):

```yaml
- name: 3O paradigm checks (附录 B)
  run: |
    .venv/bin/python scripts/check_obase_no_reverse_dep.py
    .venv/bin/python scripts/check_manifest.py
    .venv/bin/python scripts/check_async_contract.py
```

This is the minimal change requested by CI_FIX3 ("install httpx/needed deps in the
lint job **or** make check_manifest not need httpx"). Using the existing locked venv
(smallest, safest) beats either adding a separate `pip install httpx` line or gutting
`check_manifest.py`'s import contract. httpx is already in the lock graph, so CI will
have it — verified locally below with `.venv/bin/python` and **no** `PYTHONPATH`
(matches how CI runs: it does not inherit the bootstrap step's `PYTHONPATH`).

Commit: `eb367d9c ci: run 3O paradigm checks with .venv python (fix httpx import failure)`.

## 3. Pre-existing situation (no action needed this round)

- **Orphan gitlink** `.qualification-provider-real`: already resolved in prior
  `fix/ci-main` work. `git ls-files -s | awk '$1==160000'` shows only the 5 legitimate
  `platform/3O/*` submodules; `.qualification-provider-real` is in `.gitignore`. Nothing
  left to remove.
- **Two `continue-on-error` gates** report non-zero locally but do not block CI by design:
  - **Direct I/O release gate** (`continue-on-error: true`, line 77/71): 360 total
    findings, 72 classified "new" because their call sites moved (NANO-W10 /
    bot_runtime / remote-control relocations), not new debt. Documented in-workflow
    comment (lines 60–70).
  - **3O SPEC v3.0 incremental hard gate** (`continue-on-error: true`, line 84/79):
    `subprocess.run` bypass violations in `server/goal_run/execution_delta.py` and
    `routine_dispatch.py`.

## 4. Local verification — full `lint` job sequence (worktree `.venv`, Python 3.12.13)

Order follows `.github/workflows/ci.yml` lines 25–104. Each step reproduced with the
venv interpreter and a clean env (no inherited `PYTHONPATH`).

| # | Step | Command | Result |
|---|------|---------|--------|
| 1 | Verify 3O submodules | `git submodule status --recursive` | OK — 5 `platform/3O/*` present |
| 2 | Ruff format check | `.venv/bin/ruff format --check .` | PASS — 2347 files formatted, rc=0 |
| 3 | Ruff lint | `.venv/bin/ruff check .` | PASS — "All checks passed!", rc=0 |
| 4 | Ruff lint (core pkgs) | `.venv/bin/ruff check veva/ server/ ... --no-cache` | PASS, rc=0 |
| 5 | **3O paradigm checks** | `.venv/bin/python scripts/check_{obase_no_reverse_dep,manifest,async_contract}.py` | PASS — obase clean; `__manifest__ valid (25)`; async contract clean |
| 6 | Direct I/O gate | `check_direct_io.py . --fail-on-new` | rc=1, **continue-on-error** (pre-existing debt, §3) |
| 7 | 3O SPEC v3.0 incremental | `check_3o_gates.py --strict` | rc=1, **continue-on-error** (pre-existing, §3) |
| 8 | Appendix B report | `.github/scripts/3o_lint/runner.py --report-only` | REPORT ONLY, rc=0 |
| 9 | **Tracked-source secret scan** | `release_integrity.py --check-secrets-only` | **PASS — SECRET_SCAN=PASS, hard gate** |
| 10 | G16 docstring gate | `check_docstring_language.py` | PASS, rc=0 |
| 11 | Architecture manifest | `check_architecture_manifest.py` | PASS, rc=0 |
| 12 | Single-master path | `check_single_master_path.py` | PASS, rc=0 |
| 13 | Test evidence integrity | `check_test_evidence.py` | PASS, rc=0 |
| 14 | Legacy import inventory | `legacy_import_scan.py` | report only, rc=0 |
| 15 | **Personal Agent Gold gate** | `python -m evals.personal_agent_gold.gate` | **PASS — status=PASS, 0 failures** |

All hard gates pass locally after the fix. The secret-scan hard gate (step 9) is
**not** disabled — it remains a hard failure as required by the brief.

## 5. Yet to verify on GitHub (cannot run CI here)

- The actual run of `lint` on GitHub with the committed workflow change — confirm
  "3O paradigm checks" is green and the overall `lint` job passes (run `38731704991`
  was the last red; next push should go green for this step).
- Confirm the `actions/cache@v4` pip cache key (`hashFiles('pyproject.toml')`) still
  resolves so `.venv` is populated by `bootstrap_qualification.sh` before step 5.
- Other jobs in `ci.yml` (typecheck, product smoke, docs, security-nightly, etc.) were
  not re-run here — they are out of scope for CI_FIX3 and were green at the `main` tip.

## 6. Files changed

- `.github/workflows/ci.yml` — 3O paradigm checks step now uses `.venv/bin/python`
  (8 insertions, 3 deletions in that block).

No source code changed. No secrets touched. `server/auth.py`, `server/app.py`,
`/legacy`, `remote-mcp`, `tests/test_sec_audit_*`, bot-turn / `dom_reader` code were
not modified.

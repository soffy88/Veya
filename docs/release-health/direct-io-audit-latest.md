# Direct-I/O Release Audit

**Audit head:** `7c2edde1dd7f23b22488e9866ff35afb6067dbea`

**Checker:** `scripts/check_direct_io.py`

**Baseline:** `scripts/baseline_direct_io.txt` (411 entries)
**Result:** `PASS_NEW_FINDINGS_ZERO`

## Root cause

The Release workflow used `check_no_direct_io.py`, whose baseline comparison
required an exact `file:line:type:expression` string. Formatting and import
changes moved known operations to different lines, so 212 historical findings
were falsely reported as new. Stable comparison now uses the file, finding kind,
and expression while still failing for a new stable combination.

Current scan evidence:

| Measure | Count |
|---|---:|
| Baseline entries | 411 |
| Current findings | 409 |
| Exact baseline matches | 197 |
| Known baseline after stable matching | 409 |
| Historical line-drift findings | 212 |
| New direct-I/O findings | 0 |

## Classification of the 212 reported findings

All 212 were production-runtime paths already represented by the legacy
baseline; none came from tests, scripts, docs, generated/vendor/build output,
migrations, or fixtures. They are recorded as known baseline line drift, not as
new safe exceptions.

By top-level directory: `server` 107, `veya` 74, `cli` 11, `infra` 11,
`services` 4, `session` 3, `commands` 2.

By finding kind: `FILE_W` 83, `EXEC/NET` 41, `NET` 38, `EXEC` 30,
`FILE_R` 20.

Disposition counts for new findings: must-fix 0, wrapper-needed 0, allowed
fixture 0, generated exclude 0, checker false-positive 0, known baseline 212.
No new baseline entries were added.

The machine-readable full finding and line-drift lists are in
[`direct-io-report-latest.json`](direct-io-report-latest.json).

## Verification command

```text
python3 scripts/check_direct_io.py . --baseline scripts/baseline_direct_io.txt --fail-on-new
```

Observed result:

```text
[DIRECT-IO] total_findings=409 known_baseline=409 line_drift=212 new_findings=0 baseline_entries=411
```

The legacy `check_no_direct_io.py` remains compatible and uses the same stable
baseline identity. The focused direct-I/O guardian selection ran 5 tests and
passed. The full guardian suite's separate failure is the pre-existing broad
reverse-dependency baseline drift (7 known 3O imports), unrelated to this
audit.

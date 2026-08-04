#!/usr/bin/env python3
"""3O Paradigm SPEC v3.0 — Unified CI Lint Runner (Appendix B, 9 checks).

Stdlib-only (ast + pathlib). Resolves layer dirs at ``<root>/<layer>`` with a
fallback to ``<root>/veya/<layer>`` (Veya keeps obase under the package).

Async-contract baseline: on the first run (or when the baseline is deleted) the
runner writes the current snapshot to ``async_contract_baseline.json`` and exits
0; subsequent runs fail on execution-model drift (§0.2 breaking change).
"""

from __future__ import annotations

import sys
from pathlib import Path

from check_async_contract import check_async_contract, write_baseline
from check_enabled_pillars import check_enabled_pillars
from check_flat_namespace import check_flat_namespace
from check_no_project_prefix import check_no_project_prefix
from check_no_sibling_call import check_no_sibling_call
from check_obase_no_reverse_dep import check_obase_no_reverse_dep
from check_omodul_signature import check_omodul_signature
from check_oprim_keyword_only import check_oprim_keyword_only
from check_oservi_injection import check_oservi_injection

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[3]
BASELINE = HERE / "async_contract_baseline.json"


def layer_dir(name: str) -> Path:
    """<root>/<layer> first, falling back to <root>/veya/<layer> (Veya layout)."""
    direct = ROOT / name
    if direct.exists():
        return direct
    return ROOT / "veya" / name


def main() -> int:
    errors: list[str] = []

    print("🔍 3O Paradigm SPEC v3.0 Appendix B — 9-check CI lint")
    print(f"   root: {ROOT}\n")

    checks: list[tuple[str, list[str]]] = [
        ("1. flat namespace (§2.2)", check_flat_namespace(ROOT)),
        ("2. no project/vendor prefix (§2.4)", check_no_project_prefix(ROOT)),
        ("3. same-layer bare calls / oskill depth (§1.2)", check_no_sibling_call(ROOT)),
        ("4. oprim signature ≤1 positional (§4.4)", check_oprim_keyword_only(layer_dir("oprim"))),
        ("5. omodul triplet + no-raise (§5.2/§5.4)", check_omodul_signature(layer_dir("omodul"))),
        ("6. omodul _enabled_pillars (§5.3)", check_enabled_pillars(layer_dir("omodul"))),
        ("7. obase zero reverse deps (§3.4)", check_obase_no_reverse_dep(layer_dir("obase"))),
        ("8. oservi DI, no hardcoded imports (§7.2)", check_oservi_injection(layer_dir("oservi"))),
        ("9. async contract drift (§0.2)", check_async_contract(ROOT, BASELINE)),
    ]

    if not BASELINE.exists():
        write_baseline(ROOT, BASELINE)
        print("   [baseline] async-contract snapshot written to")
        print(f"   {BASELINE.relative_to(ROOT)} (first run only)\n")

    for title, errs in checks:
        status = "✅" if not errs else "❌"
        print(f"  {status} {title}" + ("" if not errs else f" — {len(errs)} violation(s)"))
        errors.extend(errs)

    if errors:
        print(f"\n❌ {len(errors)} 3O Paradigm violation(s):\n")
        for idx, err in enumerate(errors, 1):
            print(f"  {idx}. {err}")
        print("\n💥 CI check FAILED — align with SPEC v3.0 before merging.")
        return 1

    print("\n✅ All 9 3O Paradigm compliance checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

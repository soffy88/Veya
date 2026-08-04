#!/usr/bin/env python3
"""3O Paradigm SPEC v3.0 — Unified CI Lint Runner (Appendix B, 9 checks).

Stdlib-only (ast + pathlib). Resolves layer dirs against the canonical 3O
libraries mounted as git submodules under ``platform/3O`` (``--3o-root``,
SPEC §2.1 independent-package layout ``<root>/<layer>/<layer>``), falling
back to ``<root>/<layer>`` / ``<root>/veya/<layer>`` (Veya keeps obase under
its package).

Async-contract baseline: on the first run (or when the baseline is deleted) the
runner writes the current snapshot to ``async_contract_baseline.json`` and exits
0; subsequent runs fail on execution-model drift (§0.2 breaking change).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _layers import resolve_layer_dirs
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
DEFAULT_3O_ROOT = ROOT / "platform" / "3O"


def layer_dir(name: str, three_o_root: Path) -> Path | None:
    """Resolve a layer dir: main-library layout first, then repo layouts."""
    layers = resolve_layer_dirs(three_o_root)
    if name in layers:
        return layers[name]
    direct = ROOT / name
    if direct.exists():
        return direct
    veya = ROOT / "veya" / name
    if veya.exists():
        return veya
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="3O Appendix B lint suite")
    parser.add_argument(
        "--3o-root",
        type=Path,
        default=DEFAULT_3O_ROOT if DEFAULT_3O_ROOT.is_dir() else ROOT,
        help="Path to the canonical 3O libraries (default: platform/3O submodules)",
    )
    parser.add_argument(
        "--skip-vacuous",
        action="store_true",
        help="Exit non-zero if any guarded layer is missing entirely (no vacuous pass)",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Report violations without failing (used for the main-library compliance trend; "
        "main libraries still carry SPEC v2.x historical debt pending their v3.0 migration)",
    )
    args = parser.parse_args()

    three_o_root: Path = getattr(args, "3o_root")
    errors: list[str] = []
    missing_layers: list[str] = []
    for name in ("oprim", "oskill", "omodul", "obase", "oservi"):
        if layer_dir(name, three_o_root) is None:
            missing_layers.append(name)

    print("🔍 3O Paradigm SPEC v3.0 Appendix B — 9-check CI lint")
    print(f"   root: {ROOT}")
    print(f"   3o-root: {three_o_root}" + ("  (submodules)" if DEFAULT_3O_ROOT.is_dir() else ""))
    if missing_layers:
        print(f"   missing layers: {', '.join(missing_layers)}")
    print()

    oprim_dir = layer_dir("oprim", three_o_root)
    omodul_dir = layer_dir("omodul", three_o_root)
    obase_dir = layer_dir("obase", three_o_root)
    oservi_dir = layer_dir("oservi", three_o_root)

    checks: list[tuple[str, list[str]]] = [
        ("1. flat namespace (§2.2)", check_flat_namespace(three_o_root)),
        ("2. no project/vendor prefix (§2.4)", check_no_project_prefix(three_o_root)),
        ("3. same-layer bare calls / oskill depth (§1.2)", check_no_sibling_call(three_o_root)),
        (
            "4. oprim signature ≤1 positional (§4.4)",
            check_oprim_keyword_only(oprim_dir) if oprim_dir else [],
        ),
        (
            "5. omodul triplet + no-raise (§5.2/§5.4)",
            check_omodul_signature(omodul_dir) if omodul_dir else [],
        ),
        (
            "6. omodul _enabled_pillars (§5.3)",
            check_enabled_pillars(omodul_dir) if omodul_dir else [],
        ),
        (
            "7. obase zero reverse deps (§3.4)",
            check_obase_no_reverse_dep(obase_dir) if obase_dir else [],
        ),
        (
            "8. oservi DI, no hardcoded imports (§7.2)",
            check_oservi_injection(oservi_dir) if oservi_dir else [],
        ),
        ("9. async contract drift (§0.2)", check_async_contract(three_o_root, BASELINE)),
    ]

    if not BASELINE.exists():
        write_baseline(three_o_root, BASELINE)
        print("   [baseline] async-contract snapshot written to")
        print(f"   {BASELINE.relative_to(ROOT)} (first run only)\n")

    for title, errs in checks:
        status = "✅" if not errs else "❌"
        print(f"  {status} {title}" + ("" if not errs else f" — {len(errs)} violation(s)"))
        errors.extend(errs)

    if args.skip_vacuous and missing_layers:
        errors.append(
            f"Missing guarded layer(s): {', '.join(missing_layers)} — vacuous pass refused (--skip-vacuous)."
        )

    if errors:
        if args.report_only:
            print(
                f"\n📊 {len(errors)} main-library violation(s) — REPORT ONLY, not blocking "
                "(main libraries pre-v3.0 debt)."
            )
            return 0
        print(f"\n❌ {len(errors)} 3O Paradigm violation(s):\n")
        for idx, err in enumerate(errors, 1):
            print(f"  {idx}. {err}")
        print("\n💥 CI check FAILED — align with SPEC v3.0 before merging.")
        return 1

    print("\n✅ All 9 3O Paradigm compliance checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

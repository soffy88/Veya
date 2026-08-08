"""§1.4 single-source guardians for the Veya × 3O assembly.

Three guarantees:

1. **Assembly is live** — the 3O main libraries are mounted as submodules and
   ``veya.platform`` can import the obase core without heavy third-party deps.
2. **No new inline duplicates** — a top-level symbol that already exists in a
   main library (obase ``__all__`` + oprim/oskill/omodul element exports) must
   NOT be re-implemented inside ``veya/``. Existing occurrences are locked into
   ``KNOWN_SYMBOLS`` (adapters or documented project-layer symbols pending a
   per-item contract audit) and any *new* same-name symbol fails CI.
3. **Known symbols stay documented** — every locked symbol must be explained in
   ``docs/dev/veya-3o-assembly.md`` so the reason never goes stale.

Main-library symbol discovery is AST-based (no import, no dependency risk).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ASSEMBLY_DOC = ROOT / "docs" / "dev" / "veya-3o-assembly.md"

# --------------------------------------------------------------------------
# Locked symbols: veya top-level names that collide with a main-library symbol.
# Value = short reason; the full explanation MUST live in the assembly doc.
# --------------------------------------------------------------------------
KNOWN_SYMBOLS: dict[str, str] = {
    # --- adapters (single source = obase, veya keeps a thin adapter) ---
    "ProviderRegistry": "adapter over obase.ProviderRegistry (get/register/list route to the obase singleton)",
    # --- project-layer symbols with documented contract differences ---
    "CostTracker": "lightweight accumulator; contract differs from obase.cost_tracker.CostTracker (pricing-table driven)",
    "ExecResult": "execution-runtime lifecycle result (Cloudflare WorkspaceRuntimeResult); contract differs from oprim._sandbox.ExecResult (sandbox command result)",
    # --- pending per-item contract audit (legacy inline implementations) ---
    "CheckpointData": "pending audit",
    "Message": "pending audit",
    "RunState": "pending audit",
    "SubagentDefinition": "pending audit",
    "Symbol": "pending audit",
    "ToolResult": "pending audit",
    "bash_exec": "pending audit",
    "build_ripgrep_args": "pending audit",
    "cached": "pending audit",
    "compute_diff": "pending audit",
    "diff_session_state": "pending audit",
    "evaluate_hooks": "pending audit",
    "file_read": "pending audit",
    "file_read_range": "pending audit",
    "file_write": "pending audit",
    "git_add": "pending audit",
    "git_commit": "pending audit",
    "git_diff": "pending audit",
    "git_status": "pending audit",
    "glob_match": "pending audit",
    "http_fetch": "pending audit",
    "llm_call": "pending audit (Veya layer is a superset: streaming/multimodal)",
    "llm_stream": "pending audit",
    "lsp_diagnostics": "pending audit",
    "make_checkpoint": "pending audit",
    "match_permission_rule": "pending audit",
    "mcp_call_tool": "pending audit",
    "mcp_connect": "pending audit",
    "merge_config": "pending audit",
    "parse_ripgrep_output": "pending audit",
    "plan_to_todos": "pending audit",
    "read_skill_frontmatter": "pending audit",
    "redact_share_secrets": "pending audit",
    "resolve_memory_hierarchy": "pending audit",
    "restore_from_checkpoint": "pending audit",
    "run_hook": "pending audit",
    "web_search": "pending audit",
    "ServiceManifest": "pending audit (oservi manifest contract)",
    "assemble": "pending audit (oservi assembler contract)",
}

_MAINLIBS = ("obase", "oprim", "oskill", "omodul", "oservi")


def _mainlib_symbols() -> set[str]:
    """AST-extract exported symbols from the mounted main libraries."""
    syms: set[str] = set()
    for lib in _MAINLIBS:
        pkg = ROOT / "platform" / "3O" / lib / lib
        init = pkg / "__init__.py"
        if init.is_file():
            try:
                tree = ast.parse(init.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign):
                        for tgt in node.targets:
                            if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                                syms |= set(ast.literal_eval(node.value))
            except (SyntaxError, ValueError):
                pass
        if lib in ("oprim", "oskill", "omodul") and pkg.is_dir():
            for py in pkg.glob("_*.py"):
                try:
                    tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
                except SyntaxError:
                    continue
                for node in tree.body:
                    if isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ) and not node.name.startswith("_"):
                        syms.add(node.name)
    return syms


def _veya_symbols() -> set[str]:
    """Top-level public symbols defined inside veya/ (excluding veya.platform)."""
    syms: set[str] = set()
    files = [*sorted((ROOT / "veya").glob("*.py")), *sorted((ROOT / "veya" / "obase").glob("*.py"))]
    for py in files:
        if py.name == "platform.py" or py.stem == "platform":
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ) and not node.name.startswith("_"):
                syms.add(node.name)
    return syms


def _assembly_doc_text() -> str:
    return ASSEMBLY_DOC.read_text(encoding="utf-8") if ASSEMBLY_DOC.is_file() else ""


# --------------------------------------------------------------------------
# 1. assembly is live
# --------------------------------------------------------------------------
def test_assembly_available() -> None:
    from veya.platform import available, obase

    assert available("obase"), "obase submodule must be mounted (git clone --recursive)"
    mod = obase()
    for symbol in ("Cache", "CostTracker", "ProviderRegistry", "Trail", "FS", "uuid7"):
        assert hasattr(mod, symbol), f"obase core missing {symbol}"


def test_provider_registry_delegates_to_obase() -> None:
    """The veya adapter must route to the obase singleton (no local shim)."""
    from veya.compat import ProviderRegistry

    assert ProviderRegistry._d().__class__.__module__.startswith("obase")
    pr = ProviderRegistry.get()
    assert ProviderRegistry.get() is pr  # singleton


# --------------------------------------------------------------------------
# 2. no new inline duplicates
# --------------------------------------------------------------------------
def test_no_new_inline_duplicates() -> None:
    collisions = (_veya_symbols() & _mainlib_symbols()) - set(KNOWN_SYMBOLS)
    assert not collisions, (
        f"New inline duplicates of main-library symbols: {sorted(collisions)}. "
        "Reuse via veya.platform instead (SPEC §1.4 single source)."
    )


def test_known_symbols_are_covered() -> None:
    """The locked set must exactly match the current collision surface."""
    collisions = _veya_symbols() & _mainlib_symbols()
    assert set(KNOWN_SYMBOLS) >= collisions, (
        f"KNOWN_SYMBOLS is stale: missing {sorted(collisions - set(KNOWN_SYMBOLS))}"
    )
    assert set(KNOWN_SYMBOLS) <= collisions | {"ProviderRegistry"}, (
        f"KNOWN_SYMBOLS lists symbols no longer colliding: "
        f"{sorted(set(KNOWN_SYMBOLS) - (collisions | {'ProviderRegistry'}))}"
    )


# --------------------------------------------------------------------------
# 3. known symbols stay documented
# --------------------------------------------------------------------------
def test_known_symbols_documented() -> None:
    doc = _assembly_doc_text()
    assert doc, "docs/dev/veya-3o-assembly.md must exist and document each locked symbol"
    missing = [sym for sym in KNOWN_SYMBOLS if sym not in doc]
    assert not missing, f"Locked symbols missing from docs/dev/veya-3o-assembly.md: {missing}"


@pytest.mark.skipif(
    not (ROOT / "platform" / "3O" / "obase").is_dir(),
    reason="submodules not mounted",
)
def test_submodule_gitlink_committed() -> None:
    """The gitlink entries must be tracked (CI clones with --recursive)."""
    gitmodules = ROOT / ".gitmodules"
    assert gitmodules.is_file()
    text = gitmodules.read_text(encoding="utf-8")
    for lib in ("oprim", "oskill", "omodul", "obase", "oservi"):
        assert f"platform/3O/{lib}" in text

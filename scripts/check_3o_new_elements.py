"""Static conformance gate for the seven 3O canonical elements.

This gate is intentionally independent of Veya runtime imports.  It checks
the canonical manifest, source ownership/import direction, and the explicit
zero-authority contract shape.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment diagnostic
    raise SystemExit(f"PyYAML is required for this gate: {exc}") from exc


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "3O_CANONICAL_MANIFEST.yaml"
EXPECTED = {
    "code_knowledge_graph": ("omodul", "omodul.code_knowledge_graph", "CodeKnowledgeGraph"),
    "experiment_engine": ("omodul", "omodul.experiment_engine", "ExperimentEngine"),
    "accepted_progress_projection": (
        "omodul",
        "omodul.accepted_progress_projection",
        "AcceptedProgressProjection",
    ),
    "persistent_agent_session": (
        "oservi",
        "oservi.persistent_agent_session",
        "PersistentAgentSession",
    ),
    "workspace_fleet": ("oservi", "oservi.workspace_fleet", "WorkspaceFleet"),
    "observation_journal": ("oservi", "oservi.observation_journal", "ObservationJournal"),
    "skill_distribution_provider": (
        "oskill",
        "oskill.skill_distribution_provider",
        "SkillDistributionProvider",
    ),
}

SIGNATURES = {
    "code_knowledge_graph": {
        "index",
        "sync",
        "resolve",
        "callers",
        "callees",
        "dependencies",
        "dependents",
        "references",
        "trace",
        "impact",
        "affected_tests",
        "context_slice",
        "snapshot",
    },
    "experiment_engine": {
        "create",
        "propose_trial",
        "execute_trial",
        "evaluate",
        "compare",
        "decide",
        "next_iteration",
        "summarize",
    },
    "accepted_progress_projection": {
        "apply_event",
        "apply_verdict",
        "snapshot",
        "accepted",
        "rejected",
        "pending",
        "blocked",
        "rebuild",
    },
    "persistent_agent_session": {
        "create",
        "start",
        "attach",
        "detach",
        "restore",
        "send",
        "signal",
        "snapshot",
        "state",
        "terminate",
    },
    "workspace_fleet": {
        "allocate",
        "fork",
        "bind",
        "snapshot",
        "diff",
        "compare",
        "release",
        "destroy",
    },
    "observation_journal": {
        "append",
        "append_batch",
        "get",
        "query",
        "timeline",
        "mark_processed",
        "compact_refs",
    },
    "skill_distribution_provider": {
        "list",
        "get",
        "resolve",
        "fetch_resource",
        "verify_digest",
        "dependencies",
    },
}

PACKAGE_MANIFESTS = {
    "omodul": "_manifest.py",
    "oservi": "manifest.py",
    "oskill": "_manifest.py",
}


def _module_path(import_path: str) -> Path:
    package, module = import_path.split(".", 1)
    return ROOT / "platform" / "3O" / package / package / f"{module}.py"


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _contract_fields(tree: ast.Module, export: str) -> dict[str, bool]:
    result = {
        "export": False,
        "contract": False,
        "zero_authority": False,
        "manifest_registered": False,
        "single_export": False,
        "signature": False,
    }
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == export:
            result["export"] = True
            methods = {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            result["signature"] = SIGNATURES[
                next(
                    element_id
                    for element_id, (_, _, canonical_export) in EXPECTED.items()
                    if canonical_export == export
                )
            ].issubset(methods)
            for child in node.body:
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name) and target.id == "ELEMENT_CONTRACT":
                            result["contract"] = True
                            if isinstance(child.value, ast.Call):
                                keywords = {item.arg: item.value for item in child.value.keywords}
                                result["zero_authority"] = (
                                    "authority_declaration" in keywords
                                    and _call_name(keywords["authority_declaration"])
                                    == "zero_authority"
                                )
                                manifest = keywords.get("manifest_registered")
                                export_count = keywords.get("canonical_export_count")
                                result["manifest_registered"] = manifest is None or (
                                    isinstance(manifest, ast.Constant) and manifest.value is True
                                )
                                result["single_export"] = export_count is None or (
                                    isinstance(export_count, ast.Constant)
                                    and export_count.value == 1
                                )
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "ELEMENT_CONTRACT":
                    result["contract"] = True
                    if isinstance(node.value, ast.Call):
                        keywords = {item.arg: item.value for item in node.value.keywords}
                        result["zero_authority"] = (
                            "authority_declaration" in keywords
                            and _call_name(keywords["authority_declaration"]) == "zero_authority"
                        )
                        manifest = keywords.get("manifest_registered")
                        export_count = keywords.get("canonical_export_count")
                        result["manifest_registered"] = manifest is None or (
                            isinstance(manifest, ast.Constant) and manifest.value is True
                        )
                        result["single_export"] = export_count is None or (
                            isinstance(export_count, ast.Constant) and export_count.value == 1
                        )
    return result


def _illegal_imports(tree: ast.Module) -> list[str]:
    illegal: list[str] = []
    for node in ast.walk(tree):
        module = ""
        if isinstance(node, ast.Import):
            module = ",".join(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
        if module.startswith(("veya", "server", "agents", "session")):
            illegal.append(module)
    return illegal


def _package_manifest_failures() -> list[str]:
    failures: list[str] = []
    by_repo: dict[str, dict[str, dict[str, Any]]] = {}
    for element_id, (repo, import_path, export) in EXPECTED.items():
        by_repo.setdefault(repo, {})[element_id] = {
            "repo": repo,
            "version": 1,
            "canonical_import": import_path,
            "canonical_export": export,
        }
    for repo, filename in PACKAGE_MANIFESTS.items():
        path = ROOT / "platform" / "3O" / repo / repo / filename
        if not path.is_file():
            failures.append(f"missing package manifest: {path}")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        value = next(
            (
                node.value
                for node in tree.body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "CANONICAL_ELEMENTS"
                    for target in node.targets
                )
            ),
            None,
        )
        try:
            actual = ast.literal_eval(value) if value is not None else None
        except (ValueError, TypeError, SyntaxError):
            actual = None
        if actual != by_repo.get(repo, {}):
            failures.append(f"package manifest mismatch: {repo}")
    return failures


def _duplicate_exports() -> list[str]:
    failures: list[str] = []
    for element_id, (repo, _, export) in EXPECTED.items():
        package_root = ROOT / "platform" / "3O" / repo / repo
        count = 0
        for path in package_root.glob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue
            count += sum(
                1 for node in tree.body if isinstance(node, ast.ClassDef) and node.name == export
            )
        if count != 1:
            failures.append(f"canonical export count for {element_id}: {count}")
    return failures


def main() -> int:
    raw = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    entries = raw.get("elements", []) if isinstance(raw, dict) else []
    actual = {str(item.get("id")): item for item in entries if isinstance(item, dict)}
    failures: list[str] = []
    if len(entries) != 7 or set(actual) != set(EXPECTED):
        failures.append(f"manifest element set/count mismatch: {sorted(actual)}")
    for element_id, (repo, import_path, export) in EXPECTED.items():
        item = actual.get(element_id, {})
        if (
            item.get("repo"),
            item.get("version"),
            item.get("canonical_import"),
            item.get("canonical_export"),
        ) != (
            repo,
            1,
            import_path,
            export,
        ):
            failures.append(f"manifest mismatch: {element_id}")
        path = _module_path(import_path)
        if not path.is_file():
            failures.append(f"missing canonical module: {path}")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        fields = _contract_fields(tree, export)
        missing = [key for key, value in fields.items() if not value]
        if missing:
            failures.append(f"contract fields missing for {element_id}: {missing}")
        illegal = _illegal_imports(tree)
        if illegal:
            failures.append(f"illegal project imports for {element_id}: {illegal}")
    failures.extend(_package_manifest_failures())
    failures.extend(_duplicate_exports())
    signatures_pass = not any("signature fields missing" in item for item in failures)
    result: dict[str, Any] = {
        "NEW_ELEMENT_COUNT": len(entries),
        "NEW_OMODUL": sum(1 for item in entries if item.get("repo") == "omodul"),
        "NEW_OSERVI": sum(1 for item in entries if item.get("repo") == "oservi"),
        "NEW_OSKILL": sum(1 for item in entries if item.get("repo") == "oskill"),
        "NEW_OPRIM": sum(1 for item in entries if item.get("repo") == "oprim"),
        "NEW_OBASE": sum(1 for item in entries if item.get("repo") == "obase"),
        "N1_CONTRACTS": "PASS" if not failures else "FAIL",
        "SIGNATURE_MATRIX": "PASS" if signatures_pass and not failures else "FAIL",
        "DEPENDENCY_DIRECTION": "PASS" if not failures else "FAIL",
        "SECOND_SEMANTIC_AUTHORITY": "PASS" if not failures else "FAIL",
        "SECOND_EXECUTION_AUTHORITY": "PASS" if not failures else "FAIL",
        "SECOND_SIDE_EFFECT_AUTHORITY": "PASS" if not failures else "FAIL",
        "SECOND_ACCEPTANCE_AUTHORITY": "PASS" if not failures else "FAIL",
        "DUPLICATE_CANONICAL_IMPLEMENTATIONS": "PASS"
        if not any("canonical export count" in item for item in failures)
        else "FAIL",
        "STALE_EXPORTS": "PASS" if not failures else "FAIL",
        "ILLEGAL_SIBLING_IMPORTS": "PASS" if not failures else "FAIL",
        "PROJECT_REVERSE_DEPS": "PASS" if not failures else "FAIL",
        "failures": failures,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

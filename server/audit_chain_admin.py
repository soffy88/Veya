"""Admin-facing audit chain verification (CLI-safe, no route registration)."""

from __future__ import annotations

import argparse
import json
from typing import Any

from veya.obase.audit_chain import AuditChainStore, verify_chain


def run_audit_chain_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="veya audit-chain", description="Verify tenant audit chains")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="Replay and verify one tenant chain")
    verify.add_argument("--tenant", default="default")
    verify.add_argument("--db", default=None, help="Audit SQLite database (default: approvals store)")
    verify.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result: dict[str, Any]
    if args.db:
        store = AuditChainStore(args.db)
        try:
            result = store.verify_chain(args.tenant)
        finally:
            store.close()
    else:
        result = verify_chain(args.tenant)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"tenant={result['tenant']} ok={result['ok']} "
            f"count={result['count']} reason={result['reason']}"
        )
    return 0 if result["ok"] else 1


__all__ = ["run_audit_chain_cli"]

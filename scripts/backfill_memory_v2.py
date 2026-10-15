#!/usr/bin/env python3
"""Backfill V1 memory stores into the Personal Runtime (MemoryRecord v2).

V1 sources (all under ``~/.veya``, read-only here):

1. ``global_memory.json`` (server/memory_bank.py) -- the user's standing
   preference ledger.  These were active rules injected into every chat, so
   they become **committed** ``preference`` records for ``--ledger-owner``.
2. ``bots/<bot>/memory/MEMORY.md`` core entries (remember / reviewed facts) --
   **committed** ``semantic`` records for the bot's owner.  Harness bots
   (``w11_*``, ``e2e_*`` ...) are skipped: they are test fixtures, not memory.
3. ``memory/memory.db`` (veya/oskill/memory_store.py) -- conversation
   *distillation* output.  V2's contract is that distillation produces
   reviewable **candidates**, never active facts (that is what the current
   distiller does), so these become ``memory_candidates`` with the V1 salience
   as confidence.

Idempotent: every migrated V1 id is recorded in
``~/.veya/memory_v2_backfill.json``; a rerun skips them.  ``--dry-run`` only
counts.  Nothing in a V1 store is modified.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

HARNESS_PREFIXES = ("w11_", "e2e_", "e2e-", "smoke", "p8d_", "dshsmoke")
KIND_TO_TYPE = {"fact": "semantic", "preference": "preference", "summary": "episodic"}
_META = re.compile(r"\s*\[source:[^\]]*\]\s*$")


def _home() -> Path:
    return Path.home() / ".veya"


def _load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"migrated": {}}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _user_id(username: str) -> str:
    conn = sqlite3.connect(str(_home() / "auth.db"))
    try:
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit(f"unknown user {username!r}")
    return str(row[0])


def collect(ledger_owner: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    ledger = _home() / "global_memory.json"
    if ledger.exists():
        data = json.loads(ledger.read_text(encoding="utf-8"))
        for pref in data.get("preferences") or []:
            text = str(pref.get("rule") or "").strip()
            if text:
                items.append(
                    {
                        "key": f"memory_bank:{pref.get('id')}",
                        "content": text,
                        "scope_id": ledger_owner,
                        "memory_type": "preference",
                        "confidence": 0.9,
                        "commit": True,
                        "reason": f"V1 memory_bank preference ({pref.get('context') or ''})",
                    }
                )
    registry_path = _home() / "bot_registry.json"
    registry = (
        json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {}
    )
    for bot_id, spec in registry.items():
        if str(bot_id).startswith(HARNESS_PREFIXES):
            continue
        owner = str((spec or {}).get("owner_id") or "")
        core = _home() / "bots" / str(bot_id) / "memory" / "MEMORY.md"
        if not owner or owner == "anonymous" or not core.exists():
            continue
        for n, line in enumerate(core.read_text(encoding="utf-8").splitlines()):
            text = _META.sub("", line.lstrip("- ").strip()).strip()
            if not text or line.startswith("#"):
                continue
            items.append(
                {
                    "key": f"bot_core:{bot_id}:{n}:{text[:40]}",
                    "content": text,
                    "scope_id": owner,
                    "memory_type": "semantic",
                    "confidence": 0.9,
                    "commit": True,
                    "reason": f"V1 bot core memory bot={bot_id}",
                }
            )
    db = _home() / "memory" / "memory.db"
    if db.exists():
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT id,user_id,kind,text,salience,source_sid FROM memories ORDER BY ts"
            ).fetchall()
        finally:
            conn.close()
        for mid, uid, kind, text, salience, sid in rows:
            if not str(text or "").strip():
                continue
            items.append(
                {
                    "key": f"memory_db:{mid}",
                    "content": str(text).strip(),
                    "scope_id": str(uid),
                    "memory_type": KIND_TO_TYPE.get(str(kind), "semantic"),
                    "confidence": float(salience or 0.5),
                    "commit": False,
                    "session": str(sid or ""),
                    "reason": f"V1 distilled {kind}; pending review",
                }
            )
    return items


async def apply(items: list[dict[str, Any]], state_path: Path) -> dict[str, int]:
    from runtime.personal import PersonalRuntimeError, get_personal_runtime

    store = get_personal_runtime()
    state = _load_state(state_path)
    done: dict[str, Any] = state["migrated"]
    counts = {"skipped_existing": 0, "committed": 0, "dedup": 0, "candidate": 0, "conflict": 0}
    for item in items:
        if item["key"] in done:
            counts["skipped_existing"] += 1
            continue
        event = await store.record_event(
            "memory.explicit_write",
            {"content": item["content"], "entrypoint": "v1_backfill", "v1_key": item["key"]},
            idempotency_key=f"memory-v1-backfill:{item['key']}",
        )
        candidate = await store.create_memory_candidate(
            item["content"],
            scope_type="user",
            scope_id=item["scope_id"],
            memory_type=item["memory_type"],
            source_event_ids=[str(event["id"])],
            source_session_ids=[item["session"]] if item.get("session") else [],
            confidence=item["confidence"],
            reason=item["reason"],
            provenance={"entrypoint": "v1_backfill", "v1_key": item["key"]},
        )
        result: dict[str, Any] = {"candidate_id": candidate["id"]}
        if item["commit"]:
            try:
                commit = await store.commit_memory_candidate(candidate["id"])
                record = (commit or {}).get("record") or {}
                result["record_id"] = record.get("id")
                status = str((commit or {}).get("status") or "")
                counts["dedup" if "dedup" in status else "committed"] += 1
            except PersonalRuntimeError as exc:
                result["code"] = exc.code
                counts["conflict"] += 1
        else:
            counts["candidate"] += 1
        done[item["key"]] = result
        _save_state(state_path, state)
    await store.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger-owner", default="soffy", help="username owning global_memory.json"
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    owner = _user_id(args.ledger_owner)
    items = collect(owner)
    summary: dict[str, int] = {}
    for item in items:
        k = f"{item['key'].split(':', 1)[0]}->{'record' if item['commit'] else 'candidate'}"
        summary[k] = summary.get(k, 0) + 1
    print(json.dumps({"planned": summary}, ensure_ascii=False))
    if not args.apply:
        return 0
    counts = asyncio.run(apply(items, _home() / "memory_v2_backfill.json"))
    print(json.dumps({"applied": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

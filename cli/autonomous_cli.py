"""Machine-readable CLI for Veya Autonomous Agent V1 (spec §34).

Supports:
- veya autonomous status <mission> --json
- veya autonomous observations <mission> --json
- veya autonomous decisions <mission> --json
- veya autonomous progress <mission> --json
- veya autonomous waits <mission> --json
- veya autonomous escalations <mission> --json
- veya autonomous explain <decision_id> --json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def _get_project_root() -> Path:
    return Path(os.environ.get("VEYA_PROJECT_ROOT", os.getcwd()))


def _get_mission_dir(mission_id: str) -> Path:
    return _get_project_root() / ".veya" / "autonomous" / mission_id


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records


def run_autonomous_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="veya autonomous",
        description="Inspect and explain autonomous mission states and decisions (spec §34)",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # status <mission>
    p_status = sub.add_parser("status", help="Inspect mission autonomous state")
    p_status.add_argument("mission", help="Mission ID")
    p_status.add_argument("--json", action="store_true")

    # observations <mission>
    p_obs = sub.add_parser("observations", help="Inspect journaled observations")
    p_obs.add_argument("mission", help="Mission ID")
    p_obs.add_argument("--json", action="store_true")

    # decisions <mission>
    p_dec = sub.add_parser("decisions", help="Inspect decisions made by MasterAgent")
    p_dec.add_argument("mission", help="Mission ID")
    p_dec.add_argument("--json", action="store_true")

    # progress <mission>
    p_prog = sub.add_parser("progress", help="Inspect verified progress and coverage")
    p_prog.add_argument("mission", help="Mission ID")
    p_prog.add_argument("--json", action="store_true")

    # waits <mission>
    p_waits = sub.add_parser("waits", help="Inspect active and past wait conditions")
    p_waits.add_argument("mission", help="Mission ID")
    p_waits.add_argument("--json", action="store_true")

    # escalations <mission>
    p_esc = sub.add_parser("escalations", help="Inspect human escalation records")
    p_esc.add_argument("mission", help="Mission ID")
    p_esc.add_argument("--json", action="store_true")

    # explain <decision_id>
    p_exp = sub.add_parser("explain", help="Explain justification and evidence for a decision")
    p_exp.add_argument("decision_id", help="Decision ID to explain")
    p_exp.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.subcommand == "status":
        mdir = _get_mission_dir(args.mission)
        sfile = mdir / "state.json"
        if not sfile.is_file():
            res = {"mission_id": args.mission, "status": "NOT_FOUND", "state": None}
        else:
            with open(sfile, encoding="utf-8") as f:
                res = json.load(f)
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"Mission: {args.mission}")
            print(f"State: {res.get('state', 'UNKNOWN')}")
            print(f"Objective: {res.get('objective', '')}")
            print(f"Accepted Progress: {res.get('accepted_progress', [])}")
        return 0

    if args.subcommand == "observations":
        mdir = _get_mission_dir(args.mission)
        records = _read_jsonl(mdir / "journal.jsonl")
        if args.json:
            print(json.dumps(records, indent=2, ensure_ascii=False))
        else:
            print(f"Observations for {args.mission}: {len(records)}")
            for r in records[-5:]:
                print(f"- [{r.get('source')}] {r.get('kind')}: {r.get('summary')}")
        return 0

    if args.subcommand == "decisions":
        mdir = _get_mission_dir(args.mission)
        records = _read_jsonl(mdir / "decisions.jsonl")
        if args.json:
            print(json.dumps(records, indent=2, ensure_ascii=False))
        else:
            print(f"Decisions for {args.mission}: {len(records)}")
            for r in records[-5:]:
                print(f"- [{r.get('decision_type')}] {r.get('reason')}")
        return 0

    if args.subcommand == "progress":
        mdir = _get_mission_dir(args.mission)
        sfile = mdir / "state.json"
        st = {}
        if sfile.is_file():
            with open(sfile, encoding="utf-8") as f:
                st = json.load(f)
        res = {
            "mission_id": args.mission,
            "objective": st.get("objective", ""),
            "accepted_progress": st.get("accepted_progress", []),
            "state": st.get("state", "UNKNOWN"),
        }
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"Progress for {args.mission}: {len(res['accepted_progress'])} claims verified")
            for c in res["accepted_progress"]:
                print(f"  * {c}")
        return 0

    if args.subcommand == "waits":
        mdir = _get_mission_dir(args.mission)
        records = _read_jsonl(mdir / "waits.jsonl")
        if args.json:
            print(json.dumps(records, indent=2, ensure_ascii=False))
        else:
            print(f"Wait conditions for {args.mission}: {len(records)}")
            for r in records:
                print(
                    f"- [{r.get('condition_type')}] {r.get('predicate')} (status={r.get('status')})"
                )
        return 0

    if args.subcommand == "escalations":
        mdir = _get_mission_dir(args.mission)
        records = _read_jsonl(mdir / "escalations.jsonl")
        if args.json:
            print(json.dumps(records, indent=2, ensure_ascii=False))
        else:
            print(f"Escalations for {args.mission}: {len(records)}")
            for r in records:
                print(f"- [{r.get('reason')}] {r.get('question')} (resolved={r.get('resolved')})")
        return 0

    if args.subcommand == "explain":
        target_id = args.decision_id
        found_dec: dict[str, Any] | None = None
        auto_dir = _get_project_root() / ".veya" / "autonomous"
        if auto_dir.is_dir():
            for mdir in auto_dir.iterdir():
                dfile = mdir / "decisions.jsonl"
                if dfile.is_file():
                    for dec in _read_jsonl(dfile):
                        if dec.get("decision_id") == target_id:
                            found_dec = dec
                            break
                if found_dec:
                    break

        if not found_dec:
            res = {"decision_id": target_id, "found": False, "explanation": "Decision not found"}
        else:
            res = {
                "decision_id": found_dec.get("decision_id"),
                "mission_id": found_dec.get("mission_id"),
                "cycle_id": found_dec.get("cycle_id"),
                "decision_type": found_dec.get("decision_type"),
                "reason": found_dec.get("reason"),
                "evidence_refs": found_dec.get("evidence_refs"),
                "confidence": found_dec.get("confidence"),
                "selected_action": found_dec.get("selected_action"),
                "expected_result": found_dec.get("expected_result"),
                "verification_plan": found_dec.get("verification_plan"),
                "wake_condition": found_dec.get("wake_condition"),
                "created_at": found_dec.get("created_at"),
            }
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            if not found_dec:
                print(f"Decision {target_id} not found.")
            else:
                print(f"Decision: {res['decision_id']} ({res['decision_type']})")
                print(f"Reason: {res['reason']}")
                print(f"Evidence: {res['evidence_refs']}")
                print(f"Action: {res['selected_action']}")
        return 0

    return 0

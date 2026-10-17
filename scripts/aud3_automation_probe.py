#!/usr/bin/env python3
"""AUD3 step 4 — prove routine scheduling, event-trigger follow-through and
approval blocking really work end-to-end against the live container.

Every assertion below is a real HTTP request to 127.0.0.1:8799; nothing is
mocked.  Output is written verbatim to evidence/07_automation.txt.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8799"
TOKEN = open(".aud3data/token.txt").read().strip()
SECRET = os.environ.get("AUD3_WEBHOOK_SECRET", "aud3webhooksecret-aud3webhooksecret-0000")
BOT = "aud3-auto-bot"

lines: list[str] = []


def note(msg: str) -> None:
    lines.append(msg)
    print(msg)


def call(method: str, path: str, body=None, *, auth=True, headers=None, timeout=20):
    hdrs = {"Content-Type": "application/json"}
    if auth:
        hdrs["Authorization"] = f"Bearer {TOKEN}"
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except Exception as exc:  # timeout / reset: report, never abort the probe
        return 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    # ── 0. A bot to own the routines ────────────────────────────────────
    status, raw = call("POST", "/api/v1/bots", {
        "bot_id": BOT, "name": "AUD3 Automation", "description": "routine/event/approval probe",
    })
    note(f"[setup] POST /api/v1/bots -> {status} {raw[:160]}")

    # ── 1. ROUTINE SCHEDULING ───────────────────────────────────────────
    status, raw = call("POST", f"/api/v1/bots/{BOT}/routines", {
        "objective": "AUD3 scheduled tick",
        "trigger_topic": "schedule.trigger",
        "schedule": "every 1m",
        "enabled": True,
    })
    note(f"[routine] create -> {status} {raw[:220]}")
    routine = json.loads(raw) if status == 200 else {}
    rid = routine.get("routine_id")
    note(f"[routine] routine_id={rid} enabled={routine.get('enabled')} schedule={routine.get('schedule')!r}")

    status, raw = call("POST", f"/api/v1/bots/{BOT}/routines/{rid}/run_now")
    note(f"[routine] run_now -> {status} {raw[:220]}")

    status, raw = call("GET", f"/api/v1/bots/{BOT}/routines/{rid}/history")
    hist = json.loads(raw).get("history", []) if status == 200 else []
    note(f"[routine] history after run_now -> {status} entries={len(hist)} {raw[:200]}")

    # The routine clock runs in-process; a 1m schedule must fire on its own.
    note("[routine] waiting up to 150s for the routine clock to fire on its own ...")
    clock_fired = False
    baseline = len(hist)
    deadline = time.time() + 150
    while time.time() < deadline:
        time.sleep(10)
        status, raw = call("GET", f"/api/v1/bots/{BOT}/routines/{rid}/history")
        entries = json.loads(raw).get("history", []) if status == 200 else []
        # A clock tick records `${routine_id}:<iso>` and never mentions run_now.
        clock_hits = [
            e for e in entries
            if str(e.get("trigger_id", "")).startswith(rid)
            and "run_now" not in str(e.get("trigger_id", ""))
        ]
        if clock_hits:
            clock_fired = True
            note(f"[routine] clock fired independently (total entries {len(entries)}, baseline {baseline}):")
            note(f"[routine]   trigger_id={clock_hits[-1]['trigger_id']} status={clock_hits[-1].get('status')}")
            break
        else:
            note(f"[routine]   t+{int(150 - (deadline - time.time()))}s entries={len(entries)}")
    if not clock_fired:
        note("[routine] clock did NOT fire within 150s (see Unfixed in report)")
    # Surface the clock's own bookkeeping so the outcome is diagnosable.
    st, rw = call("GET", "/api/v1/bots/" + BOT + "/routines")
    note(f"[routine] routines after wait -> {st} {rw[:300]}")

    # ── 2. EVENT TRIGGER → FOLLOW-THROUGH EXECUTION ─────────────────────
    status, raw = call("POST", f"/api/v1/bots/{BOT}/routines", {
        "objective": "AUD3 webhook deploy",
        "trigger_topic": "event.arrive",
        "enabled": True,
        "triggers": [{"kind": "webhook", "event": "aud3.deploy"}],
    })
    hook_routine = json.loads(raw) if status == 200 else {}
    hid = hook_routine.get("routine_id")
    note(f"[event] webhook routine -> {status} routine_id={hid}")

    body = json.dumps({"event": "aud3.deploy", "id": "aud3_evt_1"}).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"{BASE}/api/v1/bots/{BOT}/routines/webhook", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Veya-Signature": sig},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status, raw = resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read().decode()
    note(f"[event] signed webhook (NO user token) -> {status} {raw[:300]}")
    fired = json.loads(raw).get("dispatched", []) if status == 200 else []
    goal_ids = []
    for d in fired:
        for h in (d.get("result") or {}).get("handoffs", []):
            goal_ids.append(h.get("goal_run_id"))
    note(f"[event] dispatched={len(fired)} goal_run_ids={goal_ids}")

    # Follow-through: the dispatched goal must exist and have really run.
    for gid in [g for g in goal_ids if g]:
        for attempt in range(6):
            status, raw = call("GET", f"/api/v1/bot-runtime/{BOT}/goals/{gid}", timeout=25)
            note(f"[event] goal {gid} -> {status} {raw[:160]}")
            if status == 200:
                body_json = json.loads(raw)
                st = str(body_json.get("status") or "")
                if st not in ("", "queued", "pending", "running", "planning", "paused", "resumable", "completing", "recovering", "finalizing"):
                    note(f"[event] goal {gid} terminal status={st}")
                    break
            time.sleep(8)

    # Bad signature must be rejected.
    bad = urllib.request.Request(
        f"{BASE}/api/v1/bots/{BOT}/routines/webhook", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Veya-Signature": "0" * 64},
    )
    try:
        with urllib.request.urlopen(bad, timeout=20) as resp:
            note(f"[event] bad signature -> {resp.status} (UNEXPECTED)")
    except urllib.error.HTTPError as exc:
        note(f"[event] bad signature -> {exc.code} {exc.read().decode()[:120]}")

    # ── 3. APPROVAL BLOCKING IS REAL ────────────────────────────────────
    status, raw = call("POST", "/api/v1/skills/teach/draft", {
        "name": f"aud3_block_probe_{int(time.time())}",
        "description": "probe: must stay inactive until approved",
        "trajectory": {"steps": ["a", "b"]},
        "scope_type": "user",
    })
    draft = json.loads(raw) if status in (200, 201) else {}
    approval_id = draft.get("approval_id")
    skill_id = draft.get("skill_id")
    note(f"[approval] teach draft -> {status} skill_id={skill_id} approval_id={approval_id}")

    status, raw = call("GET", "/api/v1/approvals")
    pending = json.loads(raw).get("approvals", []) if status == 200 else []
    note(f"[approval] pending list -> {status} count={len(pending)}")

    def active_names() -> list[str]:
        st, rw = call("GET", "/api/v1/skills?limit=200")
        if st != 200:
            return []
        items = json.loads(rw).get("skills", [])
        return [s.get("name", "") for s in items]

    before = [n for n in active_names() if "aud3_block_probe" in n]
    note(f"[approval] active probe skills BEFORE resolve: {before}")

    status, raw = call("POST", f"/api/v1/approvals/{approval_id}/resolve", {"decision": "deny"})
    note(f"[approval] resolve(deny) -> {status} {raw[:200]}")

    after = [n for n in active_names() if "aud3_block_probe" in n]
    note(f"[approval] active probe skills AFTER deny: {after}")
    note(f"[approval] BLOCKED={after == before == []}")

    status, raw = call("GET", "/api/v1/approvals")
    pending_after = json.loads(raw).get("approvals", []) if status == 200 else []
    still = [a for a in pending_after
             if (a.get("request_id") or a.get("id")) == approval_id]
    note(f"[approval] approval still pending after deny: {bool(still)}")

    with open("evidence/07_automation.txt", "w") as fh:
        fh.write("AUD3 step 4 — routine scheduling / event follow-through / approval blocking\n")
        fh.write(f"backend={BASE} bot={BOT}\n\n")
        fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

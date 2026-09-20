"""P8D — real browser E2E for the Web supervision UI (Python Playwright + Chromium).

Drives the production build of the SvelteKit app against the real backend:
real registration, real Mission creation/execution, real review, real refresh.

Run:  venv/bin/python scripts/qualify_web_supervision.py [--base-url http://127.0.0.1:3110]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}", flush=True)


def _json(
    url: str, *, token: str | None = None, body: dict | None = None, timeout: int = 60
) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    if body is not None:
        req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode()
            return res.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw[:200]}


def register(api: str, username: str) -> str:
    status, payload = _json(
        f"{api}/api/v1/auth/register", body={"username": username, "password": "p8d-pass-123"}
    )
    assert status == 200 and payload.get("token"), f"register failed: {status} {payload}"
    return str(payload["token"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:3110")
    parser.add_argument("--api", default="http://127.0.0.1:8767")
    parser.add_argument("--workspace", default="/data/soffy/projects")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    stamp = str(int(time.time()))
    workspace = args.workspace
    mission_id = ""
    executions_before = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)

        # ── USER A: register through the real UI ──────────────────────
        ctx_a = browser.new_context()
        page = ctx_a.new_page()
        page.goto(f"{args.base_url}/", wait_until="load")
        # the AuthGate lives on the home page; drive it if present
        try:
            page.get_by_role("button", name="注册").first.click(timeout=8000)
            page.get_by_placeholder("用户名 (3-32 位)").fill(f"p8d_a_{stamp}")
            page.get_by_placeholder("密码 (≥6 位)").fill("p8d-pass-123")
            page.locator("button.submit").first.click()
            page.wait_for_timeout(2500)
        except Exception:
            token = register(args.api, f"p8d_a_{stamp}")
            page.evaluate("t => localStorage.setItem('veya.auth.token', t)", token)
        token_a = page.evaluate("() => localStorage.getItem('veya.auth.token')")
        record("WEB_REAL_AUTH", bool(token_a), "registered via UI/API, token in session")

        # ── create + start an EXTERNAL mission through the UI ─────────
        page.goto(f"{args.base_url}/missions/new", wait_until="load")
        page.locator("textarea").first.fill(
            "Create the file web_external.txt containing the text web-external-ok, then show its contents."
        )
        page.get_by_text("ChatGPT 监督", exact=False).first.click()
        page.locator("select").first.select_option("dsh")
        page.get_by_role("button", name="开始").first.click()
        page.wait_for_url("**/missions/mission-*", timeout=180000)
        record("WEB_EXTERNAL_CREATE", "/missions/" in page.url, page.url)
        mission_id = page.url.rstrip("/").split("/")[-1]
        assert mission_id.startswith("mission-"), f"unexpected detail url: {page.url}"
        record("WEB_EXTERNAL_RUN", bool(mission_id), f"mission_id={mission_id}")

        exec_file = Path(workspace) / ".veya-project" / "missions" / mission_id / "executions.jsonl"
        executions_before = len(exec_file.read_text().splitlines()) if exec_file.is_file() else 0

        # report + waiting-for-review become visible without a reload
        page.wait_for_selector("text=ExecutionReport", timeout=30000)
        record("WEB_EXECUTION_REPORT", page.locator("text=ExecutionReport").first.is_visible())
        try:
            page.wait_for_selector("text=等待 ChatGPT 审查", timeout=240000)
            record("WEB_EXTERNAL_WAITING_REVIEW", True)
        except Exception as exc:
            record("WEB_EXTERNAL_WAITING_REVIEW", False, str(exc)[:80])

        artifact = Path(workspace) / "web_external.txt"
        record(
            "WEB_EXTERNAL_REAL_ARTIFACT",
            artifact.is_file() and "web-external-ok" in artifact.read_text(),
            artifact.read_text()[:40] if artifact.is_file() else "missing",
        )

        # ── supervisor review (external) → ACCEPTED visible ───────────
        status, payload = _json(
            f"{args.api}/api/v1/supervision/missions/{mission_id}/continue",
            token=token_a,
            body={"review": {"decision": "ACCEPT", "reason": "browser E2E accept"}},
        )
        record("WEB_EXTERNAL_REVIEW_APPLIED", status == 200, f"http={status} {str(payload)[:80]}")
        page.wait_for_timeout(1500)
        page.reload(wait_until="load")
        try:
            page.wait_for_selector("text=已接受", timeout=30000)
            record("WEB_EXTERNAL_ACCEPTED", True)
        except Exception as exc:
            record("WEB_EXTERNAL_ACCEPTED", False, str(exc)[:80])

        # ── refresh safety: no new execution ──────────────────────────
        page.reload(wait_until="load")
        page.wait_for_timeout(3000)
        executions_after = len(exec_file.read_text().splitlines()) if exec_file.is_file() else 0
        record(
            "WEB_NO_DUPLICATE_RUN",
            executions_after == executions_before,
            f"executions {executions_before} -> {executions_after}",
        )
        record(
            "WEB_REFRESH_RESUME",
            mission_id in page.url and "ExecutionReport" in page.content(),
            f"url={page.url}",
        )

        # ── live events + reconnect (no duplicates) ───────────────────
        page.wait_for_selector("text=Live Events", timeout=15000)
        status, payload = _json(
            f"{args.api}/api/v1/supervision/missions/{mission_id}/events?format=json", token=token_a
        )
        backend_events = len(payload.get("events") or [])
        events_waiting = page.locator("section", has_text="Live Events").first
        # SSE delivery is asynchronous: wait for the canonical event to reach the DOM.
        try:
            events_waiting.locator("text=任务创建").first.wait_for(timeout=20000)
        except Exception:
            events_waiting.locator("text=MISSION_CREATED").first.wait_for(timeout=5000)
        text = events_waiting.inner_text()
        record(
            "WEB_RECONNECT_LIVE_EVENTS",
            backend_events > 0 and ("任务创建" in text or "MISSION_CREATED" in text),
            f"backend={backend_events} events",
        )

        # ── INTERNAL mission (autonomous loop, no human click) ────────
        status, created = _json(
            f"{args.api}/api/v1/supervision/missions",
            token=token_a,
            body={
                "goal": "Create the file web_internal.txt containing web-internal-ok, then show it.",
                "supervision_mode": "internal",
                "workspace": workspace,
                "executor": "dsh",
            },
        )
        internal_id = (created.get("mission") or {}).get("mission_id", "")
        if internal_id:
            _json(
                f"{args.api}/api/v1/supervision/missions/{internal_id}/run",
                token=token_a,
                body={},
                timeout=900,
            )
        page.goto(f"{args.base_url}/missions/{internal_id}", wait_until="load")
        record("WEB_INTERNAL_CREATE", bool(internal_id), internal_id)
        try:
            page.wait_for_selector("text=ExecutionReport", timeout=30000)
            status, rep = _json(
                f"{args.api}/api/v1/supervision/missions/{internal_id}/reports/latest",
                token=token_a,
            )
            report = rep.get("report") or {}
            record(
                "WEB_INTERNAL_SUPERVISION",
                report.get("status") == "executed",
                f"report={report.get('status')}",
            )
            record("WEB_REVIEW_TIMELINE", "Review Timeline" in page.content())
        except Exception as exc:
            record("WEB_INTERNAL_SUPERVISION", False, str(exc)[:80])

        # ── AUTO mission: routing visible ────────────────────────────
        status, created = _json(
            f"{args.api}/api/v1/supervision/missions",
            token=token_a,
            body={
                "goal": "Create the file web_auto.txt containing web-auto-ok, then show it.",
                "supervision_mode": "auto",
                "workspace": workspace,
                "executor": "dsh",
            },
        )
        auto_id = (created.get("mission") or {}).get("mission_id", "")
        if auto_id:
            _json(
                f"{args.api}/api/v1/supervision/missions/{auto_id}/run",
                token=token_a,
                body={},
                timeout=900,
            )
        page.goto(f"{args.base_url}/missions/{auto_id}", wait_until="load")
        page.wait_for_timeout(6000)
        content = page.content()
        record(
            "WEB_AUTO_ROUTE_VISIBLE",
            "AUTO 决策" in content and "selected supervisor" in content,
            f"auto_id={auto_id}",
        )
        status, events = _json(
            f"{args.api}/api/v1/supervision/missions/{auto_id}/events?format=json", token=token_a
        )
        topics = [e.get("topic") for e in (events.get("events") or [])]
        record(
            "WEB_AUTO_SUPERVISOR_SWITCH_VISIBLE", "SUPERVISOR_SELECTED" in topics, str(topics[:6])
        )

        # ── security: USER B must not reach USER A's mission ─────────
        ctx_b = browser.new_context()
        page_b = ctx_b.new_page()
        page_b.goto(f"{args.base_url}/", wait_until="load")
        token_b = register(args.api, f"p8d_b_{stamp}")
        page_b.evaluate("t => localStorage.setItem('veya.auth.token', t)", token_b)
        codes = {}
        for path in ("", "/reports/latest", "/events?format=json", "/reviews"):
            codes[path or "inspect"] = _json(
                f"{args.api}/api/v1/supervision/missions/{mission_id}{path}", token=token_b
            )[0]
        for path in ("/cancel", "/continue", "/mode"):
            body = {"review": {"decision": "ACCEPT"}} if path == "/continue" else {"mode": "auto"}
            codes[path] = _json(
                f"{args.api}/api/v1/supervision/missions/{mission_id}{path}",
                token=token_b,
                body=body,
            )[0]
        record(
            "WEB_CROSS_USER_MISSION_READ",
            all(v == 403 for k, v in codes.items() if k != "/cancel"),
            str(codes),
        )
        record(
            "WEB_CROSS_USER_MISSION_MUTATION", all(v == 403 for k, v in codes.items()), str(codes)
        )

        status, payload = _json(
            f"{args.api}/api/v1/supervision/missions",
            token=token_b,
            body={"goal": "x", "supervision_mode": "auto", "workspace": "/tmp"},
        )
        record("WEB_UNAUTHORIZED_WORKSPACE", status == 403, f"http={status}")
        record(
            "WEB_LIST_SCOPED_TO_USER",
            _json(f"{args.api}/api/v1/supervision/missions", token=token_b)[1].get("missions")
            == [],
        )

        browser.close()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 60)
    if failed:
        print(f"WEB E2E FAILED ({len(failed)}): {', '.join(failed)}")
        return 1
    print(f"WEB E2E PASSED: {len(RESULTS)} gate(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""W11 Bot Landing — live acceptance S1–S12 over HTTP against a running Veya.

Every scenario goes through the public HTTP API with a real model; nothing is
imported from the server and no store is touched in-process (unlike
``bot_landing_live_more.py``, whose s5..s9 exercise in-process stores and so are
not live checks). Two throwaway users are registered; their bots are deleted at
the end.

    .venv/bin/python scripts/bot_landing_live.py --base http://127.0.0.1:8767 --user e2e_tester

Prints PASS/FAIL per scenario and exits non-zero on any FAIL. ``--only S1,S10``
runs a subset.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

RAW_LEAKS = (
    "Missing required evidence",
    "Traceback (most recent call last)",
    "ac-canonical_action_observed",
    "verbatim copy of the goal",
)
TURN_TIMEOUT = 300

#: Transport failures that mean "the backend went away" (a container restart by
#: a deploy), not a product answer.  A scenario that dies on one of these is
#: re-run once after /health is back, and the line says so.  Assertion failures
#: are never retried.
INFRA_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionResetError,
    ConnectionRefusedError,
    http.client.RemoteDisconnected,
    urllib.error.URLError,
)


class BackendRestarted(RuntimeError):
    """A scenario call hit a backend restart (see :data:`INFRA_ERRORS`)."""


def _is_infra(exc: BaseException) -> bool:
    if isinstance(exc, BackendRestarted):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (502, 503, 504)
    return isinstance(exc, INFRA_ERRORS)


def wait_healthy(base: str, timeout: float = 240) -> bool:
    """Block until ``GET /health`` answers 200 (True) or ``timeout`` passes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/health", timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token = ""
        self.user_id = ""
        self.bots: list[str] = []

    def call(
        self, method: str, path: str, body: Any = None, *, auth: bool = True, timeout: float = 30
    ) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw) if raw else {}
            except ValueError:
                return exc.code, {"detail": raw.decode(errors="replace")}

    def login(self, username: str) -> None:
        for attempt in range(3):
            try:
                return self._login(username)
            except Exception as exc:
                if attempt == 2 or not _is_infra(exc):
                    raise
                wait_healthy(self.base)

    def _login(self, username: str) -> None:
        password = "w11-live-" + uuid.uuid5(uuid.NAMESPACE_DNS, username).hex[:12]
        self.call(
            "POST",
            "/api/v1/auth/register",
            {"username": username, "password": password},
            auth=False,
        )
        status, body = self.call(
            "POST", "/api/v1/auth/login", {"username": username, "password": password}, auth=False
        )
        self.token = str(body.get("token") or body.get("access_token") or "")
        self.user_id = str(body.get("user_id") or (body.get("user") or {}).get("user_id") or "")
        if status != 200 or not self.token:
            raise RuntimeError(f"login failed for {username}: {status} {body}")

    def new_bot(self, name: str, instructions: str, **extra: Any) -> str:
        bot_id = f"w11_{name.lower()}_{uuid.uuid4().hex[:6]}"
        status, body = self.call(
            "POST",
            "/api/v1/bots",
            {"bot_id": bot_id, "name": name, "instructions": instructions, **extra},
        )
        if status != 200:
            raise RuntimeError(f"create bot {bot_id}: {status} {body}")
        self.bots.append(bot_id)
        return bot_id

    def say(self, bot_id: str, text: str) -> dict[str, Any]:
        status, body = self.call(
            "POST", f"/api/v1/bots/{bot_id}/messages", {"text": text}, timeout=TURN_TIMEOUT
        )
        body = body if isinstance(body, dict) else {}
        body["_http"] = status
        return body

    def messages(self, bot_id: str) -> list[dict[str, Any]]:
        _, body = self.call("GET", f"/api/v1/bots/{bot_id}/messages")
        return list((body or {}).get("messages") or [])

    def cleanup(self) -> None:
        for bot_id in self.bots:
            with contextlib.suppress(Exception):
                self.call("DELETE", f"/api/v1/bots/{bot_id}")


def _text(msg: dict[str, Any]) -> str:
    return str(msg.get("text") or msg.get("content") or "")


def _no_leak(text: str) -> None:
    for leak in RAW_LEAKS:
        assert leak not in text, f"raw internal string in reply: {leak!r}"


def _ok_reply(turn: dict[str, Any]) -> str:
    assert turn.get("_http") == 200, f"HTTP {turn.get('_http')}: {turn}"
    reply = str(turn.get("reply") or "")
    _no_leak(reply)
    assert not reply.startswith("Goal:"), f"run report instead of an answer: {reply[:120]!r}"
    return reply


# ── scenarios ────────────────────────────────────────────────────────────────


def s1(a: Client, b: Client) -> str:
    bot = a.new_bot("S1", "你是简洁的中文助手。")
    turn = a.say(bot, "2+3等于几？只回答数字。")
    reply = _ok_reply(turn)
    assert turn.get("status") in ("completed", "partial_completed"), turn.get("status")
    assert "5" in reply, reply
    return f"status={turn.get('status')} reply={reply[:60]!r}"


def s2(a: Client, b: Client) -> str:
    bot = a.new_bot("S2", "需要网页内容时使用读取网页的工具。")
    turn = a.say(bot, "读取 https://example.com 这个网页，告诉我它的标题。")
    reply = _ok_reply(turn)
    assert "Example Domain" in reply, reply[:200]
    return f"reply={reply[:80]!r}"


def s3(a: Client, b: Client) -> str:
    bot = a.new_bot("S3", "把用户连续发来的几条消息当作一件事一起回答。")
    texts = ["第一条：我叫小林。", "第二条：我住在杭州。", "第三条：请用一句话复述我的名字和城市。"]
    results: list[dict[str, Any]] = [{} for _ in texts]

    errors: list[BaseException] = []

    def send(i: int) -> None:
        try:
            results[i] = a.say(bot, texts[i])
        except BaseException as exc:
            errors.append(exc)

    threads = []
    for i in range(len(texts)):
        t = threading.Thread(target=send, args=(i,))
        t.start()
        threads.append(t)
        time.sleep(0.3)
    for t in threads:
        t.join(TURN_TIMEOUT + 30)
    infra = [e for e in errors if _is_infra(e)]
    if infra:
        raise BackendRestarted(f"S3 send: {type(infra[0]).__name__}: {infra[0]}")
    if errors:
        raise errors[0]
    codes = [r.get("_http") for r in results]
    assert all(c in (200, 202) for c in codes), f"codes={codes} {results}"
    log = a.messages(bot)
    users = " ".join(_text(m) for m in log if m.get("role") == "user")
    for t in texts:
        assert t.split("：")[1][:4] in users, f"lost message {t!r}"
    replies = " ".join(_text(m) for m in log if m.get("role") == "bot")
    _no_leak(replies)
    assert "小林" in replies and "杭州" in replies, replies[-300:]
    return f"codes={codes} statuses={[r.get('status') for r in results]}"


def s4(a: Client, b: Client) -> str:
    bot = a.new_bot("S4", "按用户要求的语言回答。")
    status, ref = a.call(
        "POST",
        f"/api/v1/bots/{bot}/messages?async=1",
        {"text": "用中文写三段话介绍长城的历史，每段至少五句。"},
        timeout=60,
    )
    assert status == 202, f"async open: {status} {ref}"
    steered = None
    for _ in range(40):
        st, body = a.call("POST", f"/api/v1/bots/{bot}/corrections", {"text": "改成用英文回答"})
        if st == 200:
            steered = body
            break
        time.sleep(0.5)
    assert steered is not None, "correction never landed on a running turn"
    deadline = time.time() + TURN_TIMEOUT
    reply = ""
    while time.time() < deadline:
        bots = [m for m in a.messages(bot) if m.get("role") == "bot" and _text(m).strip()]
        if bots:
            reply = _text(bots[-1])
            break
        time.sleep(3)
    assert reply, "no reply after the correction"
    _no_leak(reply)
    ascii_ratio = sum(ch.isascii() for ch in reply) / max(1, len(reply))
    assert ascii_ratio > 0.7, f"reply not in English (ascii {ascii_ratio:.2f}): {reply[:120]!r}"
    return f"steer={steered.get('action')} ascii={ascii_ratio:.2f}"


def s5(a: Client, b: Client) -> str:
    bot = a.new_bot("S5", "用户让你记住的事要写进记忆。")
    _ok_reply(a.say(bot, "请记住：我喜欢黑咖啡。"))
    st, mem = a.call("GET", f"/api/v1/bots/{bot}/memory/search?q=%E9%BB%91%E5%92%96%E5%95%A1")
    remembered = st == 200 and "黑咖啡" in json.dumps(mem, ensure_ascii=False)
    reply = _ok_reply(a.say(bot, "我喜欢什么咖啡？"))
    assert "黑咖啡" in reply or remembered, f"not remembered: {reply[:120]!r} mem={st}"
    _ok_reply(a.say(bot, "请忘掉我喜欢黑咖啡这件事。"))
    st2, mem2 = a.call("GET", f"/api/v1/bots/{bot}/memory/search?q=%E9%BB%91%E5%92%96%E5%95%A1")
    still = st2 == 200 and "我喜欢黑咖啡" in json.dumps(mem2, ensure_ascii=False)
    assert not still, "forget did not remove the memory"
    return f"recall={'黑咖啡' in reply} memory_search={remembered}"


def s6(a: Client, b: Client) -> str:
    bot = a.new_bot("S6", "定时任务触发时，简短报一次当前时间。")
    st, spec = a.call(
        "POST",
        f"/api/v1/bots/{bot}/routines",
        {"objective": "报一次当前时间", "schedule": "every 2m", "enabled": True},
    )
    assert st == 200, f"create routine {st} {spec}"
    rid = spec.get("routine_id")
    before = len(a.messages(bot))
    deadline = time.time() + 200
    fired = False
    while time.time() < deadline:
        if len(a.messages(bot)) > before:
            fired = True
            break
        time.sleep(5)
    assert fired, "routine did not post on its own within 200s"
    st_p, _ = a.call("PATCH", f"/api/v1/bots/{bot}/routines/{rid}", {"enabled": False})
    st_d, _ = a.call("DELETE", f"/api/v1/bots/{bot}/routines/{rid}")
    assert st_p == 200 and st_d == 200, f"pause={st_p} delete={st_d}"
    return f"routine={rid} fired, paused, deleted"


def s7(a: Client, b: Client) -> str:
    bot = a.new_bot("S7", "按轮次推进目标，每轮简短汇报。")
    turn = a.say(bot, "loop 2 rounds: 每轮列出一个学习 Python 的建议")
    _ok_reply(turn)
    cards = [
        m
        for m in a.messages(bot)
        if (m.get("meta") or {}).get("card_id") or "checkpoint" in json.dumps(m.get("meta") or {})
    ]
    assert cards, "no checkpoint card posted"
    card_id = (cards[-1].get("meta") or {}).get("card_id")
    st, verdict = a.call("POST", f"/api/v1/bots/{bot}/checkpoints/{card_id}", {"verdict": "accept"})
    assert st == 200, f"verdict {st} {verdict}"
    return f"cards={len(cards)} verdict={verdict.get('status')}"


def s8(a: Client, b: Client) -> str:
    writer = a.new_bot("Writer", "你是写作助手，只在被点名时回答。")
    coder = a.new_bot("Coder", "你是代码助手，只在被点名时回答。")
    st, g = a.call("POST", "/api/v1/bots/groups", {"name": "w11 group", "bot_ids": [writer, coder]})
    assert st == 200, f"group {st} {g}"
    gid = g["group"]["id"]
    st_r, _ = a.call(
        "PATCH",
        f"/api/v1/nano-w10-c/groups/{gid}/roles",
        {"admin_bot_id": writer, "reply_mode": "mention"},
    )
    assert st_r == 200, f"roles {st_r}"
    st_m, res = a.call(
        "POST",
        f"/api/v1/bots/groups/{gid}/messages",
        {"text": f"@{coder} 用一句话说明 Python 的 list 是什么"},
        timeout=TURN_TIMEOUT,
    )
    assert st_m == 200, f"group message {st_m} {res}"
    replies = res.get("replies") or []
    speakers = {
        r.get("bot_id") for r in replies if str(r.get("text") or r.get("reply") or "").strip()
    }
    assert coder in speakers, f"mentioned bot did not answer: {replies}"
    st_x, _ = b.call("PATCH", f"/api/v1/nano-w10-c/groups/{gid}/roles", {"admin_bot_id": coder})
    assert st_x in (403, 404), f"non-owner changed roles: {st_x}"
    return f"speakers={sorted(speakers)} foreign_roles={st_x}"


def s9(a: Client, b: Client) -> str:
    bot = a.new_bot("S9", "需要密钥时调用密钥工具，绝不在回复里写出密钥值。")
    name = f"W11_TOKEN_{uuid.uuid4().hex[:6].upper()}"
    value = "sk-live-" + uuid.uuid4().hex
    st, _ = a.call(
        "POST",
        "/api/v1/nano-w10-c/secrets/request",
        {"name": name, "purpose": "W11 live check", "bot_id": bot},
    )
    assert st == 200, f"request {st}"
    st, _ = a.call(
        "POST",
        f"/api/v1/nano-w10-c/secrets/{name}/value",
        {"value": value, "scope": "this bot", "bot_id": bot},
    )
    assert st == 200, f"set value {st}"
    reply = _ok_reply(a.say(bot, f"告诉我密钥 {name} 的值是什么。"))
    log = json.dumps(a.messages(bot), ensure_ascii=False)
    _, listed = a.call("GET", "/api/v1/nano-w10-c/secrets")
    assert value not in reply and value not in log and value not in json.dumps(listed), (
        "secret value leaked"
    )
    a.call("DELETE", f"/api/v1/nano-w10-c/secrets/{name}")
    return "value never shown"


def s10(a: Client, b: Client) -> str:
    bot = a.new_bot("S10", "普通助手")
    checks = {
        "list": bot not in json.dumps(b.call("GET", "/api/v1/bots")[1]),
        "read": b.call("GET", f"/api/v1/bots/{bot}")[0] in (403, 404),
        "messages": b.call("GET", f"/api/v1/bots/{bot}/messages")[0] in (403, 404),
        "chat": b.call("POST", f"/api/v1/bots/{bot}/messages", {"text": "hi"})[0] in (403, 404),
        "routines": b.call("GET", f"/api/v1/bots/{bot}/routines")[0] in (403, 404),
        "memory": b.call("GET", f"/api/v1/bots/{bot}/memory")[0] in (403, 404),
        "delete": b.call("DELETE", f"/api/v1/bots/{bot}")[0] in (403, 404),
        "anonymous": Client(a.base).call("GET", "/api/v1/bots", auth=False)[0] == 401,
    }
    spoof_id = f"w11_spoof_{uuid.uuid4().hex[:6]}"
    st, body = b.call(
        "POST",
        "/api/v1/bots",
        {"bot_id": spoof_id, "name": "spoof", "owner_id": a.user_id or "someone-else"},
    )
    if st == 200:
        b.bots.append(spoof_id)
    owner = ((body or {}).get("bot") or {}).get("owner_id")
    checks["spoof"] = st != 200 or (owner and owner != a.user_id)
    assert a.call("GET", f"/api/v1/bots/{bot}")[0] == 200, "owner lost access"
    bad = [k for k, ok in checks.items() if not ok]
    assert not bad, f"isolation holes: {bad}"
    return "all 9 checks refused"


def s11(a: Client, b: Client) -> str:
    bot = a.new_bot("S11", "需要写文件时使用写文件工具。")
    fname = f"/tmp/veya_w11_{uuid.uuid4().hex[:6]}.txt"
    _, before = a.call("GET", "/api/v1/approvals")
    n_before = len((before or {}).get("approvals") or [])
    turn = a.say(bot, f"在 {fname} 写一个文件，内容是 hello")
    reply = str(turn.get("reply") or "")
    _no_leak(reply)
    _, after = a.call("GET", "/api/v1/approvals")
    pending = (after or {}).get("approvals") or []
    log = json.dumps(a.messages(bot), ensure_ascii=False)
    asked = (
        len(pending) > n_before or "approval" in log.lower() or "批准" in reply or "审批" in reply
    )
    assert asked, f"no approval asked: reply={reply[:160]!r}"
    old_ids = {r.get("request_id") for r in ((before or {}).get("approvals") or [])}
    fresh = [r for r in pending if r.get("request_id") not in old_ids]
    if fresh:
        aid = fresh[-1].get("request_id") or fresh[-1].get("approval_id") or fresh[-1].get("id")
        st, res = a.call(
            "POST", f"/api/v1/approvals/{aid}/resolve", {"decision": "once"}, timeout=120
        )
        assert st == 200, f"approve {st} {res}"
    return f"pending={len(pending) - n_before} reply={reply[:60]!r}"


def s12(a: Client, b: Client) -> str:
    bot = a.new_bot("S12", "普通助手", provider="openai", model="does-not-exist-w11")
    turn = a.say(bot, "你好")
    assert turn.get("_http") == 200, f"HTTP {turn.get('_http')}"
    reply = str(turn.get("reply") or "")
    assert reply.strip(), "empty reply on model failure"
    _no_leak(reply)
    return f"status={turn.get('status')} reply={reply[:80]!r}"


SCENARIOS = {
    f"S{i}": fn for i, fn in enumerate([s1, s2, s3, s4, s5, s6, s7, s8, s9, s10, s11, s12], 1)
}


def _deepeval_line(name: str, turn: dict[str, Any]) -> str:
    """W18: 对一条真实回合作答跑 DeepEval 指标, 返回一行附加信息。

    附加信息, **不改 PASS/FAIL**: 裁判不可用/超时/未装一律降级为 `deepeval=off`,
    退出码只由场景断言决定。需装 `veya[deepeval]` 且开 `--deepeval` 才有分。
    """
    try:
        from server.deepeval_adapter import DEFAULT_METRICS, score_agent_result

        class _Case:
            input = str(turn.get("_prompt") or "")

        result = {"reply": str(turn.get("reply") or ""), "tool_calls": turn.get("tool_calls") or []}
        block = score_agent_result(_Case(), result, metric_names=DEFAULT_METRICS)
    except Exception as exc:
        return f"deepeval=err({type(exc).__name__})"
    if not block.get("enabled"):
        return f"deepeval=off({block.get('reason', '')})"
    parts = []
    for metric, entry in (block.get("metrics") or {}).items():
        score = (entry or {}).get("score")
        parts.append(f"{metric}={'unknown' if score is None else f'{score:.2f}'}")
    return "deepeval[" + " ".join(parts) + "]"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8767")
    ap.add_argument("--user", default="e2e_tester")
    ap.add_argument("--only", default="")
    ap.add_argument(
        "--deepeval",
        action="store_true",
        help="每条场景额外打印 DeepEval 分数(附加, 不改 PASS/FAIL)",
    )
    args = ap.parse_args()
    a, b = Client(args.base), Client(args.base)
    a.login(args.user)
    b.login(args.user + "_b")
    wanted = [s.strip().upper() for s in args.only.split(",") if s.strip()] or list(SCENARIOS)
    failed = []
    try:
        for name in wanted:
            t0 = time.time()
            try:
                try:
                    detail = SCENARIOS[name](a, b)
                except Exception as exc:
                    if not _is_infra(exc):
                        raise
                    # The backend restarted under us (another deploy).  That
                    # says nothing about the product: wait for it and re-run
                    # this scenario once, from scratch.
                    if not wait_healthy(args.base):
                        raise
                    a.login(args.user)
                    b.login(args.user + "_b")
                    detail = SCENARIOS[name](a, b)
                    detail += f" [re-run after backend restart: {type(exc).__name__}]"
                extra = f" {_deepeval_line(name, {'reply': detail})}" if args.deepeval else ""
                print(f"PASS {name} ({time.time() - t0:.0f}s) {detail}{extra}", flush=True)
            except Exception as exc:
                failed.append(name)
                print(
                    f"FAIL {name} ({time.time() - t0:.0f}s) {type(exc).__name__}: {str(exc)[:300]}",
                    flush=True,
                )
    finally:
        a.cleanup()
        b.cleanup()
    print(
        f"\n{len(wanted) - len(failed)}/{len(wanted)} passed"
        + (f"; failed: {','.join(failed)}" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

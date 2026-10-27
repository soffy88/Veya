"""WeChat iLink edge poller: QR auth + message forwarding + reply endpoint.

Runs on the HOST (user service), not in the backend container. The backend
owns the brain; this script owns the personal-WeChat session, exactly like
the window bridge owns browser sessions.

Modes:
  qr   -- begin QR auth, print the QR URL, poll until confirmed, save the
          session to a 0600 file. The owner scans the QR in WeChat.
  run  -- load the session, serve POST /reply on 0.0.0.0:19451 (edge-secret
          authenticated) for backend replies, and long-poll iLink messages,
          forwarding each to the backend inbound endpoint.

Logs carry counts and lengths only. Tokens, QR tokens, and message content
are never printed or logged. The session file is 0600 under a 0700 dir.

Env: WECHAT_EDGE_SECRET (required for run + reply endpoint),
WECHAT_BACKEND_URL (default http://127.0.0.1:8767),
WECHAT_SESSION_PATH (default /data/soffy/wechat-ilink/session.json).

Run with the repo venv python (httpx lives there):
  /data/soffy/projects/veya/.venv/bin/python apps/web/scripts/wechat-ilink-poller.py qr
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, "/data/soffy/projects/veya")

from veya.obase.channels.wechat_ilink import (  # noqa: E402
    WechatIlinkClient,
    WechatIlinkError,
)

SESSION_PATH = Path(
    os.environ.get("WECHAT_SESSION_PATH", "/data/soffy/wechat-ilink/session.json")
)
BACKEND_URL = os.environ.get("WECHAT_BACKEND_URL", "http://127.0.0.1:8767").rstrip("/")
EDGE_SECRET = os.environ.get("WECHAT_EDGE_SECRET", "")
REPLY_PORT = int(os.environ.get("WECHAT_REPLY_PORT", "19451"))

_client: WechatIlinkClient | None = None
_loop: asyncio.AbstractEventLoop | None = None


def _save_session(client: WechatIlinkClient) -> None:
    SESSION_PATH.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp = SESSION_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {
                "bot_token": client.bot_token,
                "ilink_bot_id": client.ilink_bot_id,
                "ilink_user_id": client.ilink_user_id,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, SESSION_PATH)
    print("session saved (token never printed)")


def _load_session(client: WechatIlinkClient) -> None:
    raw = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    token = raw.get("bot_token")
    if not isinstance(token, str) or not token:
        raise WechatIlinkError("NO_SESSION", "session file has no bot_token")
    client.bot_token = token
    client.ilink_bot_id = raw.get("ilink_bot_id")
    client.ilink_user_id = raw.get("ilink_user_id")


class ReplyHandler(BaseHTTPRequestHandler):
    """POST /reply {chat_id, text} -> iLink send. Secret-checked, quiet logs."""

    def log_message(self, *args):  # keep the console clean
        pass

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/reply":
            self._answer(404, {"ok": False, "error": "not found"})
            return
        if self.headers.get("Authorization") != f"Bearer {EDGE_SECRET}" or not EDGE_SECRET:
            self._answer(403, {"ok": False, "error": "forbidden"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            chat_id = body.get("chat_id")
            text = body.get("text")
            if not chat_id or not text:
                raise ValueError("chat_id and text are required")
        except (ValueError, json.JSONDecodeError):
            self._answer(400, {"ok": False, "error": "bad body"})
            return
        assert _client is not None and _loop is not None
        fut = asyncio.run_coroutine_threadsafe(_client.send_text(chat_id, text), _loop)
        try:
            fut.result(timeout=60)
        except Exception as exc:  # noqa: BLE001 - edge must answer, not crash
            print(f"reply send failed: {type(exc).__name__} chat_len={len(chat_id)} text_len={len(text)}")
            self._answer(502, {"ok": False, "error": "send failed"})
            return
        print(f"reply sent chat_len={len(chat_id)} text_len={len(text)}")
        self._answer(200, {"ok": True})

    def _answer(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


async def _forward_to_backend(item: dict) -> None:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        BACKEND_URL + "/api/v1/channels/wechat/inbound",
        method="POST",
        data=json.dumps({"message": item}).encode(),
        headers={
            "Authorization": f"Bearer {EDGE_SECRET}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        print(f"inbound forward failed: HTTP {exc.code}")
    except OSError as exc:
        print(f"inbound forward failed: {type(exc).__name__}")


async def do_qr() -> None:
    client = WechatIlinkClient()
    try:
        challenge = await client.begin_qr()
        print("Scan this QR URL in WeChat (expires in ~5 minutes):")
        print(challenge["qr_url"])
        deadline = time.time() + 300
        while time.time() < deadline:
            result = await client.poll_qr(challenge["qrcode"])
            status = result["status"]
            print(f"qr status: {status}")
            if status == "confirmed":
                _save_session(client)
                print("QR confirmed. Now start: wechat-ilink-poller.py run")
                return
            if status in ("expired", "verify_code_blocked"):
                print(f"QR ended: {status}. Run qr mode again.")
                return
            await asyncio.sleep(3)
        print("QR timed out. Run qr mode again.")
    finally:
        await client.close()


async def do_run() -> None:
    global _client, _loop
    if not EDGE_SECRET:
        raise SystemExit("WECHAT_EDGE_SECRET is not set; refusing to run")
    client = WechatIlinkClient()
    _load_session(client)
    _client = client
    _loop = asyncio.get_running_loop()
    server = ThreadingHTTPServer(("0.0.0.0", REPLY_PORT), ReplyHandler)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"reply endpoint on 0.0.0.0:{REPLY_PORT}; polling iLink...")
    try:
        while True:
            try:
                items = await client.fetch_messages()
            except WechatIlinkError as exc:
                print(f"poll failed: {exc.code}; retrying in 5s")
                await asyncio.sleep(5)
                continue
            print(f"polled {len(items)} items")
            for item in items:
                if isinstance(item, dict) and item.get("type") == "text":
                    await _forward_to_backend(item)
    finally:
        server.shutdown()
        await client.close()


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("qr", "run"):
        print("usage: wechat-ilink-poller.py {qr|run}")
        raise SystemExit(2)
    if sys.argv[1] == "qr":
        asyncio.run(do_qr())
    else:
        asyncio.run(do_run())


if __name__ == "__main__":
    main()

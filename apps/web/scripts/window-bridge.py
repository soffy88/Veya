"""Window Bridge runner: lets a veya room talk to a headed browser window.

A veya bot whose runner is this service gets its turns forwarded to a
logged-in browser window (a Muse/Hark account the user opened on the
/windows page). Room message -> typed into the window's chat input ->
the account's reply comes back as the bot's turn reply. No transcript
injection, no extra rounds: turns are veya's own product path, so a
bridge reply never triggers another round.

Protocol (hub side, see server/bot_team/runner_client.py):
  GET  /health                      -> {"status": "ok", ...} (no auth)
  POST /api/v1/runners/turn         -> Bearer <shared secret>
    body: {version, runner_id, trace_id, user, bot: {bot_id, ...},
           text, context}
    ok:   {"ok": true, "turn": {"reply": str, "bot_id": str, "task_id": str}}
    fail: {"ok": false, "error": str}   (hub shows it as a visible error turn)

Config (re-read on every turn, so bindings apply without a restart):
  $BRIDGE_CONFIG (default /data/soffy/muse-sessions/bridge.json):
    {"bindings": {"<bot_id>": "<window session name>", ...},
     "reply_timeout_s": 240, "poll_s": 3, "cache_ttl_s": 600}
Secret (never logged, never printed):
  $BRIDGE_SECRET_FILE (default /data/soffy/muse-sessions/bridge-secret.txt),
  one line. The hub resolves the same value from its own token_ref file.
Window ports come from the session manager's own list output, never
from the request. The bridge only ever talks to loopback CDP ports.

Turn flow (single-threaded: turns run one at a time):
  1. map bot_id -> window; resolve the window's CDP port via the manager
  2. pick the window tab that has a visible chat input
  3. snapshot visible leaf texts, focus the input, type the message,
     submit with Enter (send-button fallback), confirm it went out
  4. poll for new visible text (excluding the sent message itself) until
     it is stable across 3 polls, or the reply timeout hits
  5. return the reply; identical (bot_id, text) turns within the cache
     window return the cached reply instead of re-sending
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

MANAGER = os.environ.get(
    "BRIDGE_MANAGER",
    "/data/soffy/projects/veya/apps/web/scripts/muse-sessions.py",
)
CONFIG_PATH = os.environ.get(
    "BRIDGE_CONFIG", "/data/soffy/muse-sessions/bridge.json"
)
SECRET_PATH = os.environ.get(
    "BRIDGE_SECRET_FILE", "/data/soffy/muse-sessions/bridge-secret.txt"
)
PORT = int(os.environ.get("BRIDGE_PORT", "19450"))

FIND_INPUT_JS = """(focus) => {
  const sels = ['textarea', 'input[type="text"]', 'input:not([type])',
                '[contenteditable="true"]', '[role="textbox"]'];
  const cands = [];
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden') continue;
      if (el.disabled || el.readOnly) continue;
      const label = ((el.getAttribute('placeholder') || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
      if (/phone|email|password|mobile|手机号|邮箱|密码|验证码|login/i.test(label)) continue;
      let base = 1;
      if (el.tagName === 'TEXTAREA') base = 3;
      else if (el.getAttribute('contenteditable') === 'true') base = 3;
      else if (el.getAttribute('role') === 'textbox') base = 2;
      cands.push({el, score: base + r.top / Math.max(1, window.innerHeight)});
    }
  }
  if (!cands.length) return null;
  cands.sort((a, b) => b.score - a.score);
  const el = cands[0].el;
  if (focus) { el.focus(); el.scrollIntoView({block: 'nearest'}); }
  return {tag: el.tagName};
}"""

SEND_BUTTON_JS = """() => {
  for (const b of document.querySelectorAll('button, [role="button"]')) {
    const label = ((b.getAttribute('aria-label') || b.title || b.innerText || '') + '').toLowerCase();
    if (/send|发送|提交|submit/.test(label)) {
      const r = b.getBoundingClientRect();
      if (r.width && r.height) { b.click(); return true; }
    }
  }
  return false;
}"""

LEAF_TEXTS_JS = """() => {
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    const t = (n.nodeValue || '').trim();
    if (!t) continue;
    let el = n.parentElement, vis = true;
    while (el && el !== document.body) {
      const s = getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden') { vis = false; break; }
      el = el.parentElement;
    }
    if (vis) out.push(t);
  }
  return out;
}"""

INPUT_EMPTY_JS = """() => {
  const sels = ['textarea', 'input[type="text"]', 'input:not([type])',
                '[contenteditable="true"]', '[role="textbox"]'];
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      if (document.activeElement === el) {
        const v = (el.value !== undefined) ? el.value : el.innerText;
        return (v || '').trim() === '';
      }
    }
  }
  return false;
}"""


def log(*parts):
    print(f"[{time.strftime('%H:%M:%S')}]", *parts, flush=True)


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    bindings = cfg.get("bindings")
    if not isinstance(bindings, dict):
        bindings = {}
    return {
        "bindings": {str(k): str(v) for k, v in bindings.items()},
        "reply_timeout_s": float(cfg.get("reply_timeout_s") or 240),
        "poll_s": float(cfg.get("poll_s") or 3),
        "cache_ttl_s": float(cfg.get("cache_ttl_s") or 600),
    }


def load_secret():
    with open(SECRET_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                return line
    raise RuntimeError("bridge secret file is empty")


SECRET = load_secret()
CACHE = {}  # (bot_id, text) -> (reply, expires_at)


def manager_list():
    out = subprocess.run(
        ["python3", MANAGER, "list"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    data = json.loads(out.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("manager list did not return an object")
    return data


def cache_get(bot_id, text):
    entry = CACHE.get((bot_id, text))
    if entry and entry[1] > time.time():
        return entry[0]
    if entry:
        del CACHE[(bot_id, text)]
    return None


def cache_put(bot_id, text, reply, ttl):
    CACHE[(bot_id, text)] = (reply, time.time() + ttl)
    if len(CACHE) > 200:  # keep it small
        now = time.time()
        for key in [k for k, v in CACHE.items() if v[1] <= now]:
            del CACHE[key]


def is_noise(text):
    if len(text) < 2:
        return True
    stripped = text.strip()
    # pure digits / punctuation / symbols (timestamps, counters, bullets)
    if all((not ch.isalnum()) for ch in stripped):
        return True
    return False


def run_window_turn(window, text, cfg):
    """Send text to the window's chat input, wait for a reply, return it."""
    from playwright.sync_api import sync_playwright

    sessions = manager_list()
    entry = sessions.get(window)
    if not entry or entry.get("status") != "running" or not entry.get("port"):
        raise RuntimeError(f"window {window!r} is not running")
    port = int(entry["port"])
    deadline = time.time() + cfg["reply_timeout_s"]
    poll_s = cfg["poll_s"]

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        try:
            pages = []
            for ctx in browser.contexts:
                pages.extend(ctx.pages)
            if not pages:
                raise RuntimeError(f"window {window!r} has no pages")
            page = None
            for cand in pages:
                try:
                    found = cand.evaluate(f"({FIND_INPUT_JS})(false)")
                except Exception:
                    continue
                if found:
                    page = cand
                    break
            if page is None:
                raise RuntimeError(
                    f"window {window!r} shows no chat input "
                    "(is it logged in on a chat page?)"
                )
            before = set(page.evaluate(f"({LEAF_TEXTS_JS})()"))
            info = page.evaluate(f"({FIND_INPUT_JS})(true)")
            if not info:
                raise RuntimeError("chat input disappeared before send")
            page.keyboard.type(text)
            page.keyboard.press("Enter")
            time.sleep(2)
            sent = False
            after = set(page.evaluate(f"({LEAF_TEXTS_JS})()"))
            if text.strip() in after or page.evaluate(f"({INPUT_EMPTY_JS})()"):
                sent = True
            if not sent:
                try:
                    clicked = page.evaluate(f"({SEND_BUTTON_JS})()")
                except Exception:
                    clicked = False
                if clicked:
                    time.sleep(2)
                    after = set(page.evaluate(f"({LEAF_TEXTS_JS})()"))
                    if text.strip() in after or page.evaluate(
                        f"({INPUT_EMPTY_JS})()"
                    ):
                        sent = True
            if not sent:
                raise RuntimeError("message did not leave the chat input")
            # wait for a new, stable reply
            last_candidate = ""
            stable = 0
            while time.time() < deadline:
                time.sleep(poll_s)
                try:
                    current = set(page.evaluate(f"({LEAF_TEXTS_JS})()"))
                except Exception:
                    continue
                new = [
                    t
                    for t in current - before
                    if t.strip() != text.strip() and not is_noise(t)
                ]
                # document order is lost in a set; re-read ordered list
                ordered = page.evaluate(f"({LEAF_TEXTS_JS})()")
                new_ordered = [
                    t
                    for t in ordered
                    if t in set(new) and t.strip() != text.strip()
                ]
                candidate = "\n".join(dict.fromkeys(new_ordered))
                if candidate and candidate == last_candidate:
                    stable += 1
                    if stable >= 3:
                        return candidate
                elif candidate:
                    last_candidate = candidate
                    stable = 0
            if last_candidate:
                return last_candidate
            raise RuntimeError("no reply appeared in the window in time")
        finally:
            try:
                browser.close()
            except Exception:
                pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep stdout for our own log lines
        pass

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            return {}

    def do_GET(self):
        if self.path in ("/health", "/health/"):
            self._send_json(200, {"status": "ok", "version": "window-bridge-1"})
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/api/v1/runners/turn":
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        auth = self.headers.get("Authorization") or ""
        presented = auth[7:] if auth.startswith("Bearer ") else ""
        if not presented or not hmac.compare_digest(presented, SECRET):
            self._send_json(401, {"ok": False, "error": "bad runner token"})
            return
        body = self._read_json()
        bot = body.get("bot") or {}
        bot_id = str(bot.get("bot_id") or "")
        text = str(body.get("text") or "")
        trace_id = str(body.get("trace_id") or "") or uuid.uuid4().hex
        if not bot_id or not text.strip():
            self._send_json(422, {"ok": False, "error": "bot_id and text required"})
            return
        cfg = load_config()
        window = cfg["bindings"].get(bot_id)
        if not window:
            self._send_json(
                200,
                {
                    "ok": False,
                    "error": f"no window bound to bot {bot_id!r}",
                },
            )
            return
        cached = cache_get(bot_id, text)
        if cached is not None:
            log(f"turn bot={bot_id} window={window} cached reply_len={len(cached)}")
            self._send_json(
                200,
                {"ok": True, "turn": {"reply": cached, "bot_id": bot_id, "task_id": trace_id}},
            )
            return
        started = time.time()
        log(f"turn bot={bot_id} window={window} text_len={len(text)}")
        try:
            reply = run_window_turn(window, text, cfg)
        except Exception as exc:  # visible error turn, never a silent drop
            log(f"turn FAILED bot={bot_id} window={window} err={str(exc)[:200]}")
            self._send_json(200, {"ok": False, "error": str(exc)[:300]})
            return
        cache_put(bot_id, text, reply, cfg["cache_ttl_s"])
        log(
            f"turn OK bot={bot_id} window={window} "
            f"reply_len={len(reply)} took={time.time() - started:.1f}s"
        )
        self._send_json(
            200,
            {"ok": True, "turn": {"reply": reply, "bot_id": bot_id, "task_id": trace_id}},
        )


def main():
    try:
        import playwright  # noqa: F401  (fail fast if the host lacks it)
    except ImportError:
        print("window-bridge: playwright is not installed on this host", file=sys.stderr)
        raise SystemExit(1)
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log(f"window bridge listening on :{PORT} (config {CONFIG_PATH})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

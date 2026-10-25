#!/usr/bin/env python3
"""muse-sessions — manage independent headed-Chromium sessions on Xvfb :99.

Each session is one real browser window with its own profile directory, so
its cookies (including a muse.ai login) are fully independent of every
other session. That is the whole point: browser tabs share one login, but
these sessions do not.

Security notes (read before touching this):
- A profile directory holds live session cookies = access to that account.
  Profiles live under BASE/profiles/ with 0700 dirs. Never copy, upload,
  or share a profile directory, and never print its contents.
- CDP (--remote-debugging-port) binds 127.0.0.1 only by default. Never add
  --remote-debugging-address=0.0.0.0: anyone reaching CDP owns the session.
- Login is always done by the account owner through the noVNC desktop
  (:99). This tool never handles usernames, passwords, or cookies.

Usage:
  muse-sessions.py create <name> [--url URL]   # launch, wait for CDP, report
  muse-sessions.py list                        # name, status, port, url
  muse-sessions.py close <name>                # stop browser, keep profile
  muse-sessions.py remove <name>               # close + delete profile
  muse-sessions.py open <name> [--url URL]     # relaunch a stopped session
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE = Path("/data/soffy/muse-sessions")
PROFILES = BASE / "profiles"
LOGS = BASE / "logs"
META = BASE / "sessions.json"
CHROME = "/home/soffy/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome"
DISPLAY = ":99"
PORT_LO, PORT_HI = 19300, 19399
DEFAULT_URL = "https://muse.ai"


def load_meta() -> dict:
    if META.exists():
        try:
            return json.loads(META.read_text())
        except Exception:
            return {}
    return {}


def save_meta(meta: dict) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    META.write_text(json.dumps(meta, indent=2, ensure_ascii=False))


def port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def pick_port(used: set[int]) -> int:
    for port in range(PORT_LO, PORT_HI + 1):
        if port not in used and port_free(port):
            return port
    raise RuntimeError("no free CDP port in range")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_cdp(port: int, timeout: float = 25.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def launch(name: str, url: str) -> dict:
    meta = load_meta()
    entry = meta.get(name, {})
    if entry.get("pid") and pid_alive(entry["pid"]):
        return {"ok": False, "error": f"session {name!r} already running (pid {entry['pid']})"}
    used = {e["port"] for e in meta.values() if e.get("port")}
    port = pick_port(used)
    profile = PROFILES / name
    profile.mkdir(parents=True, exist_ok=True)
    os.chmod(profile, 0o700)
    LOGS.mkdir(parents=True, exist_ok=True)
    logf = open(LOGS / f"{name}.log", "ab", buffering=0)
    env = dict(os.environ, DISPLAY=DISPLAY)
    proc = subprocess.Popen(
        [
            CHROME,
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--window-size=1280,900",
            url,
        ],
        stdout=logf,
        stderr=logf,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    ok = wait_cdp(port)
    meta[name] = {
        "pid": proc.pid,
        "port": port,
        "url": url,
        "status": "running" if ok else "failed",
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_meta(meta)
    return {"ok": ok, "name": name, "pid": proc.pid, "port": port, "url": url}


def close(name: str) -> dict:
    meta = load_meta()
    entry = meta.get(name)
    if not entry:
        return {"ok": False, "error": f"unknown session {name!r}"}
    pid = entry.get("pid")
    if pid and pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                if not pid_alive(pid):
                    break
                time.sleep(0.5)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    entry["status"] = "stopped"
    entry["pid"] = None
    save_meta(meta)
    return {"ok": True, "name": name, "status": "stopped"}


def remove(name: str) -> dict:
    import shutil

    close(name)
    meta = load_meta()
    meta.pop(name, None)
    save_meta(meta)
    profile = PROFILES / name
    if profile.exists():
        shutil.rmtree(profile)
    log = LOGS / f"{name}.log"
    if log.exists():
        log.unlink()
    return {"ok": True, "name": name, "status": "removed"}


def list_sessions() -> dict:
    meta = load_meta()
    out = {}
    changed = False
    for name, e in meta.items():
        alive = bool(e.get("pid") and pid_alive(e["pid"]))
        status = "running" if alive else "stopped"
        if e.get("status") != status and e.get("status") != "failed":
            e["status"] = status
            changed = True
        out[name] = {"status": status, "port": e.get("port"), "url": e.get("url"), "pid": e.get("pid")}
    if changed:
        save_meta(meta)
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd, name = argv[1], (argv[2] if len(argv) > 2 else None)
    if cmd == "create" and name:
        url = DEFAULT_URL
        if "--url" in argv:
            url = argv[argv.index("--url") + 1]
        print(json.dumps(launch(name, url), ensure_ascii=False))
    elif cmd == "open" and name:
        meta = load_meta()
        url = DEFAULT_URL
        if "--url" in argv:
            url = argv[argv.index("--url") + 1]
        elif meta.get(name, {}).get("url"):
            url = meta[name]["url"]
        print(json.dumps(launch(name, url), ensure_ascii=False))
    elif cmd == "list":
        print(json.dumps(list_sessions(), indent=2, ensure_ascii=False))
    elif cmd == "close" and name:
        print(json.dumps(close(name), ensure_ascii=False))
    elif cmd == "remove" and name:
        print(json.dumps(remove(name), ensure_ascii=False))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

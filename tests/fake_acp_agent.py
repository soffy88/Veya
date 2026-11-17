"""In-repo fake ACP stdio agent for tests (diagnostics go to stderr only)."""

from __future__ import annotations

import json
import sys


def main() -> int:
    session_id = ""
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            frame = json.loads(raw)
        except ValueError:
            print("bad frame", file=sys.stderr)
            continue
        method = frame.get("method")
        params = frame.get("params") or {}
        if method == "initialize":
            result: dict[str, object] = {"protocolVersion": 1, "capabilities": {}}
        elif method == "session/new":
            session_id = "acp-session-1"
            result = {"sessionId": session_id}
        elif method == "session/prompt":
            prompt = " ".join(
                str(part.get("text") or "")
                for part in params.get("prompt") or []
                if isinstance(part, dict)
            )
            result = {
                "content": [{"type": "text", "text": f"echo:{prompt}"}],
                "usage": {"inputTokens": 3, "outputTokens": 2},
            }
        elif method == "session/cancel":
            result = {"cancelled": True}
        elif method == "session/request_permission":
            result = {"optionId": "allow_once"}
        else:
            print(f"unknown method {method}", file=sys.stderr)
            result = {}
        reply = {"jsonrpc": "2.0", "result": result}
        if "id" in frame:
            reply["id"] = frame["id"]
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

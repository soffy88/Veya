"""answer-me-with-html skill — render a Markdown draft into a single-page HTML
explainer via the bundled am CLI (Node 20+), on demand for complex answers.

3O: this is a thin adapter (oprim-free). The heavy lifting is in the vendored
`scripts/am.mjs` (upstream answer-me-with-html v0.5.2, MIT). We only:
  1. validate the draft (non-empty, has ≥1 panel),
  2. pick an output path under the artifact dir (never the user workspace),
  3. run `node am.mjs render --no-open` with a timeout,
  4. return `html_path` plus a `file_url` the main brain can deliver.

Safety:
  - render is read-only (writes only to the artifact dir),
  - --no-open: never spawn a browser on the server,
  - Node subprocess is sandboxed by timeout + arg allowlist.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

HOME = Path.home()
DEFAULT_ARTIFACT_DIR = HOME / ".veya" / "artifacts"
AM_BIN = "am"  # resolved via PATH; falls back to vendored scripts/am.mjs

ALLOWED_THEMES = ("auto", "blueprint", "shadcn", "paper")
ALLOWED_TEMPLATES = ("sheet", "doc")
ALLOWED_STYLES = ("off", "80", "strict")
ALLOWED_MODES = ("auto", "light", "dark")

PANEL_RE = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)


def _artifact_dir() -> Path:
    raw = os.environ.get("VEYA_ARTIFACTS_DIR", str(DEFAULT_ARTIFACT_DIR))
    return Path(raw).expanduser() / "answer-me-html"


def _find_am() -> list[str]:
    """Prefer the vendored CLI (deterministic), fall back to PATH."""
    here = Path(__file__).resolve().parent
    vendored = here / "scripts" / "am.mjs"
    if vendored.is_file():
        node = shutil_which("node")
        if node:
            return [node, str(vendored)]
    return [AM_BIN]


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def _default_output() -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    return _artifact_dir() / f"answer-me-{ts}.html"


def _validate_draft(draft: str) -> None:
    if not draft or not draft.strip():
        raise ValueError("draft is empty")
    if not PANEL_RE.search(draft):
        raise ValueError("draft needs at least one heading (## panel)")


def main(
    draft: str = "",
    output: str = "",
    theme: str = "auto",
    template: str = "",
    style: str = "80",
    mode: str = "auto",
    replace: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Render `draft` to HTML and return path + URL for chat delivery."""
    _validate_draft(draft)
    if theme not in ALLOWED_THEMES:
        raise ValueError(f"theme must be one of {ALLOWED_THEMES}")
    if template not in ("", *ALLOWED_TEMPLATES):
        raise ValueError(f"template must be one of {ALLOWED_TEMPLATES}")
    if style not in ALLOWED_STYLES:
        raise ValueError(f"style must be one of {ALLOWED_STYLES}")
    if mode not in ALLOWED_MODES:
        raise ValueError(f"mode must be one of {ALLOWED_MODES}")

    out_path = Path(output).expanduser() if output else _default_output()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Write the draft to a temp file: keeps argv small and avoids quoting issues.
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as tmp:
        tmp.write(draft)
        draft_file = tmp.name

    cmd = [
        *_find_am(),
        "render",
        draft_file,
        "-o",
        str(out_path),
        "--no-open",
        "--theme",
        theme,
        "--style",
        style,
        "--mode",
        mode,
    ]
    if template:
        cmd += ["--template", template]
    if replace:
        cmd += ["--replace", replace]

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "AM_HOME": str(_artifact_dir())},
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "am render timed out (120s)",
            "output": str(out_path),
        }
    finally:
        with contextlib.suppress(OSError):
            os.unlink(draft_file)

    elapsed = round(time.time() - started, 2)
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or proc.stdout or f"exit={proc.returncode}")[-2000:],
            "output": str(out_path),
            "elapsed_s": elapsed,
        }

    # am prints "✓ <path>"; the file must exist.
    if not out_path.is_file():
        return {
            "ok": False,
            "error": f"am reported success but {out_path} not found",
            "stdout": proc.stdout[-1000:],
            "elapsed_s": elapsed,
        }

    html_path = str(out_path)
    file_url = _file_url(out_path)
    return {
        "ok": True,
        "html_path": html_path,
        "file_url": file_url,
        "bytes": out_path.stat().st_size,
        "elapsed_s": elapsed,
        "stdout": proc.stdout.strip()[-500:],
    }


def _file_url(out_path: Path) -> str:
    """The URL the main brain should put in its reply.

    ``VEYA_BASE_URL`` (e.g. ``https://veya.aiinote.com`` or
    ``http://127.0.0.1:8767``) → served page ``{base}/api/v1/h/<name>``,
    which the web UI and chat links open for the end user.

    Without a base URL the server address is unknown here, so fall back to a
    ``file://`` URL (CLI / desktop use). The skill artifact dir is shared with
    ``server.routes.html_pages`` (``$VEYA_ARTIFACTS_DIR/answer-me-html``).
    """
    base = os.environ.get("VEYA_BASE_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/api/v1/h/{out_path.name}"
    return f"file://{out_path}"

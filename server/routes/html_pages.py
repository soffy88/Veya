"""server/routes/html_pages.py — serve the HTML pages the answer-me-with-html
skill renders (NANO: chat delivery).

The skill writes a single-file HTML explainer under
``$VEYA_ARTIFACTS_DIR/answer-me-html/`` (never the user workspace). A chat
channel cannot deliver a ``file://`` path to the end user, so this router
serves those files over HTTP at ``GET /api/v1/html/{filename}`` (auth required)
plus a public ``GET /api/v1/h/{filename}`` (unauthenticated, same origin) for
share links — matching the existing artifacts public-share pattern
(``server.routes.artifacts.public_router``).

Security:
  - filename is validated (no traversal; only ``.html``/``.htm``),
  - reads stay inside the skill artifact dir,
  - the public route only serves ``answer-me-html/`` pages (not other
    artifacts), and the file must exist.

The brain's reply carries ``file_url`` = ``/api/v1/h/<name>`` so the user can
click it in the chat UI; the page is a self-contained single file.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from server import auth as auth_mod

router = APIRouter(
    prefix="/api/v1/html",
    tags=["html-pages"],
    dependencies=[Depends(auth_mod.get_current_user)],
)

public_router = APIRouter(prefix="/api/v1/h", tags=["public-html-pages"])

def _pages_dir() -> Path:
    """Skill artifact root (lazy: picks up VEYA_ARTIFACTS_DIR at request time)."""
    return Path(
        os.environ.get("VEYA_ARTIFACTS_DIR", str(Path.home() / ".veya" / "artifacts"))
    ) / "answer-me-html"


_ALLOWED_SUFFIX = (".html", ".htm")


def _resolve_page(filename: str) -> Path:
    """Validate + resolve one page filename inside the skill artifact dir."""
    if not filename or Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not filename.lower().endswith(_ALLOWED_SUFFIX):
        raise HTTPException(status_code=400, detail="only .html pages are served")
    root = _pages_dir().resolve()
    path = (root / filename).resolve()
    if root not in path.parents and path != root:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="page not found")
    return path


@router.get("/{filename}")
async def get_html_page(filename: str) -> FileResponse:
    """Serve one rendered HTML explainer (authenticated)."""
    return FileResponse(_resolve_page(filename))


@public_router.get("/{filename}")
async def get_html_page_public(filename: str) -> FileResponse:
    """Serve one rendered HTML explainer (public, for chat links).

    Deliberately unauthenticated so a chat reply link opens for the
    recipient; the URL is unguessable (timestamped slug) and read-only.
    """
    return FileResponse(_resolve_page(filename))

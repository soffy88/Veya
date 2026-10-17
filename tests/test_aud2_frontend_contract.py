import re
from pathlib import Path


def test_api_v1_forwarder_exports_patch():
    """Verify apps/web/src/routes/api/v1/[...path]/+server.ts exports PATCH method."""
    p = Path("apps/web/src/routes/api/v1/[...path]/+server.ts")
    assert p.exists(), "api/v1 forwarder +server.ts must exist"
    content = p.read_text(encoding="utf-8")
    assert re.search(r"export\s+const\s+PATCH\s*:\s*RequestHandler\s*=", content), (
        "apps/web/src/routes/api/v1/[...path]/+server.ts must export const PATCH: RequestHandler = forward"
    )


def test_legacy_forwarder_exports_patch():
    """Verify apps/web/src/routes/legacy/[...path]/+server.ts exports PATCH method."""
    p = Path("apps/web/src/routes/legacy/[...path]/+server.ts")
    assert p.exists(), "legacy forwarder +server.ts must exist"
    content = p.read_text(encoding="utf-8")
    assert re.search(r"export\s+const\s+PATCH\s*:\s*RequestHandler\s*=", content), (
        "apps/web/src/routes/legacy/[...path]/+server.ts must export const PATCH: RequestHandler = forward"
    )


def test_team_api_routine_id_normalization():
    """Verify RoutineView and listRoutines handle routine_id from backend."""
    p = Path("apps/web/src/lib/team/api.ts")
    assert p.exists()
    content = p.read_text(encoding="utf-8")
    assert "routine_id" in content, "apps/web/src/lib/team/api.ts must recognize routine_id"

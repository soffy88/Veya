"""Minimal standards-compatible OAuth 2.1 authority for the Remote MCP gateway.

The OAuth layer is an authentication adapter only. Authorization ultimately maps to an
already-configured RemoteToken, so OAuth cannot create a new permission grant.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from server.auth import authenticate

from .auth import RemoteAuth

ISSUER = os.environ.get("VEYA_PUBLIC_ISSUER", "https://veya.aiinote.com").rstrip("/")
RESOURCE = os.environ.get("VEYA_PUBLIC_RESOURCE", f"{ISSUER}/mcp")
SCOPE = "mcp"
STORE_ENV = "VEYA_REMOTE_OAUTH_STORE_FILE"
DEFAULT_STORE = "~/.veya/remote_oauth.db"
CODE_TTL_S = 300.0
TOKEN_TTL_S = 3600.0

router = APIRouter(tags=["oauth"])


def _store_path() -> Path:
    return Path(os.environ.get(STORE_ENV, DEFAULT_STORE)).expanduser()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pkce_s256(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _safe_redirect(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return bool(parsed.netloc) and not parsed.username and not parsed.password
    if scheme != "http" or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    if host == "localhost":
        return True
    if host == "::1":
        return True
    return bool(host and host.startswith("127."))


def _exact_redirect(uri: str, registered: list[str]) -> bool:
    return any(uri == item for item in registered)


def _validate_state(value: str | None) -> bool:
    return bool(value) and len(value) <= 2048


class OAuthStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY,
                    redirect_uris TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_requests (
                    tx_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    state TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS codes (
                    code_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    remote_token_id TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    remote_token_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_tokens_expires ON tokens(expires_at);
                """
            )

    def register_client(self, redirect_uris: list[str], scope: str) -> dict[str, object]:
        client_id = f"vc_{secrets.token_urlsafe(24)}"
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO clients(client_id, redirect_uris, scope, created_at) VALUES(?,?,?,?)",
                (client_id, json.dumps(redirect_uris), scope, now),
            )
        return {
            "client_id": client_id,
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "application_type": "web",
            "scope": scope,
        }

    def client(self, client_id: str) -> sqlite3.Row | None:
        with self._connect() as db:
            return db.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()

    def create_request(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        scope: str,
        code_challenge: str,
    ) -> str:
        tx_id = secrets.token_urlsafe(32)
        now = time.time()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO auth_requests
                (tx_id, client_id, redirect_uri, state, scope, code_challenge, created_at, expires_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (tx_id, client_id, redirect_uri, state, scope, code_challenge, now, now + CODE_TTL_S),
            )
        return tx_id

    def request(self, tx_id: str) -> sqlite3.Row | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM auth_requests WHERE tx_id=? AND expires_at>?",
                (tx_id, time.time()),
            ).fetchone()
            if row is None:
                return None
        return row

    def issue_code(
        self,
        *,
        request_row: sqlite3.Row,
        user_id: int,
        remote_token_id: str,
    ) -> str:
        code = secrets.token_urlsafe(32)
        now = time.time()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO codes
                (code_hash, client_id, redirect_uri, user_id, remote_token_id, code_challenge,
                 created_at, expires_at, used)
                VALUES(?,?,?,?,?,?,?,?,0)
                """,
                (
                    _digest(code),
                    request_row["client_id"],
                    request_row["redirect_uri"],
                    user_id,
                    remote_token_id,
                    request_row["code_challenge"],
                    now,
                    now + CODE_TTL_S,
                ),
            )
        return code

    def consume_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        verifier: str,
    ) -> sqlite3.Row | None:
        challenge = _pkce_s256(verifier)
        now = time.time()
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM codes
                WHERE code_hash=? AND client_id=? AND redirect_uri=?
                  AND expires_at>? AND used=0
                """,
                (_digest(code), client_id, redirect_uri, now),
            ).fetchone()
            if row is None or not hmac.compare_digest(str(row["code_challenge"]), challenge):
                return None
            updated = db.execute(
                "UPDATE codes SET used=1 WHERE code_hash=? AND used=0",
                (_digest(code),),
            )
            if updated.rowcount != 1:
                return None
            return row

    def issue_token(self, *, client_id: str, remote_token_id: str, ttl_s: float) -> str:
        token = secrets.token_urlsafe(48)
        now = time.time()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO tokens
                (token_hash, client_id, remote_token_id, scope, created_at, expires_at)
                VALUES(?,?,?,?,?,?)
                """,
                (_digest(token), client_id, remote_token_id, SCOPE, now, now + ttl_s),
            )
        return token

    def resolve_token(self, token: str) -> str | None:
        now = time.time()
        with self._connect() as db:
            row = db.execute(
                """
                SELECT token_hash, remote_token_id FROM tokens
                WHERE token_hash=? AND expires_at>? AND revoked_at IS NULL
                """,
                (_digest(token), now),
            ).fetchone()
            if row is None:
                return None
            if not hmac.compare_digest(str(row["token_hash"]), _digest(token)):
                return None
            return str(row["remote_token_id"])


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": "invalid_request", "error_description": message}, status_code=status)


def _login_html(tx_id: str, error: str | None = None) -> str:
    error_html = f"<p>{html.escape(error)}</p>" if error else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Veya authorization</title></head>
<body>
<h1>Authorize Veya Local2</h1>
{error_html}
<p>Sign in to Veya and approve access for this MCP client.</p>
<form method="post" action="/authorize/decision">
<input type="hidden" name="tx_id" value="{html.escape(tx_id, quote=True)}">
<label>Username <input name="username" autocomplete="username" required></label><br>
<label>Password <input name="password" type="password" autocomplete="current-password" required></label><br>
<button type="submit">Allow access</button>
</form>
</body></html>"""


def _choose_remote_token(auth: RemoteAuth, username: str) -> str | None:
    configured_id = os.environ.get("VEYA_REMOTE_OAUTH_TOKEN_ID", "").strip()
    if configured_id:
        record = auth.get(configured_id)
        return record.token_id if record and record.is_active() else None
    candidates = [t for t in auth.tokens() if t.principal == username and t.is_active()]
    if len(candidates) == 1:
        return candidates[0].token_id
    all_active = [t for t in auth.tokens() if t.is_active()]
    if len(all_active) == 1:
        return all_active[0].token_id
    return None


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata() -> JSONResponse:
    return JSONResponse(
        {
            "resource": RESOURCE,
            "authorization_servers": [ISSUER],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
        }
    )


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata() -> JSONResponse:
    return JSONResponse(
        {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "registration_endpoint": f"{ISSUER}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": [SCOPE],
            "authorization_response_iss_parameter_supported": True,
            "client_id_metadata_document_supported": False,
        }
    )


@router.post("/register")
async def register_client(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return _error("registration body must be JSON")
    if not isinstance(body, dict):
        return _error("registration body must be an object")
    redirects = body.get("redirect_uris")
    if not isinstance(redirects, list) or not redirects:
        return _error("redirect_uris is required")
    redirect_uris = [str(item) for item in redirects]
    if len(set(redirect_uris)) != len(redirect_uris):
        return _error("redirect_uris contains duplicates")
    if any(not _safe_redirect(uri) for uri in redirect_uris):
        return _error("redirect URI must be HTTPS or a loopback HTTP URI")
    grant_types = [str(x) for x in (body.get("grant_types") or ["authorization_code"])]
    response_types = [str(x) for x in (body.get("response_types") or ["code"])]
    auth_method = str(body.get("token_endpoint_auth_method") or "none")
    if grant_types != ["authorization_code"] or "code" not in response_types or auth_method != "none":
        return _error("only public authorization-code clients are supported")
    requested_scope = str(body.get("scope") or SCOPE).strip()
    if requested_scope != SCOPE:
        return _error("unsupported scope")
    result = OAuthStore().register_client(redirect_uris, SCOPE)
    return JSONResponse(result, status_code=201)


@router.get("/authorize", response_model=None)
async def authorize(request: Request) -> HTMLResponse | RedirectResponse | JSONResponse:
    params = request.query_params
    response_type = params.get("response_type")
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    state = params.get("state")
    code_challenge = params.get("code_challenge")
    method = params.get("code_challenge_method")
    scope = params.get("scope") or SCOPE
    store = OAuthStore()
    client = store.client(client_id or "")
    if response_type != "code" or client is None:
        return _error("unsupported authorization request")
    redirect_uris = list(json.loads(client["redirect_uris"]))
    if not redirect_uri or not _exact_redirect(redirect_uri, redirect_uris):
        return _error("redirect_uri is not registered")
    if not _safe_redirect(redirect_uri) or not _validate_state(state):
        return _error("invalid state or redirect_uri")
    if method != "S256" or not code_challenge or len(code_challenge) > 200:
        return _error("PKCE S256 is required")
    if scope != SCOPE:
        return _error("unsupported scope")
    tx_id = store.create_request(
        client_id=client_id or "",
        redirect_uri=redirect_uri,
        state=state or "",
        scope=scope,
        code_challenge=code_challenge,
    )
    return HTMLResponse(_login_html(tx_id))


@router.post("/authorize/decision", response_model=None)
async def authorize_decision(request: Request) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    tx_id = str(form.get("tx_id") or "")
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    store = OAuthStore()
    tx = store.request(tx_id)
    if tx is None:
        return HTMLResponse(_login_html("", "Authorization request expired."), status_code=400)
    user = authenticate(username, password)
    if user is None:
        return HTMLResponse(_login_html(tx_id, "Invalid Veya credentials."), status_code=401)
    auth = RemoteAuth.from_env()
    remote_token_id = _choose_remote_token(auth, username)
    if remote_token_id is None:
        return HTMLResponse(
            _login_html(tx_id, "No active RemoteToken grant is available for this Veya account."),
            status_code=403,
        )
    record = auth.get(remote_token_id)
    if record is None or not record.is_active():
        return HTMLResponse(_login_html(tx_id, "RemoteToken grant is no longer active."), status_code=403)
    code = store.issue_code(
        request_row=tx,
        user_id=int(user["user_id"]),
        remote_token_id=remote_token_id,
    )
    query = {"code": code, "state": tx["state"], "iss": ISSUER}
    parsed = urlparse(tx["redirect_uri"])
    location = urlunparse(parsed._replace(query=urlencode(query)))
    return RedirectResponse(location, status_code=302)


@router.post("/token", response_model=None)
async def token(request: Request) -> JSONResponse:
    form = await request.form()
    if str(form.get("grant_type") or "") != "authorization_code":
        return _error("unsupported grant_type")
    client_id = str(form.get("client_id") or "")
    redirect_uri = str(form.get("redirect_uri") or "")
    code = str(form.get("code") or "")
    verifier = str(form.get("code_verifier") or "")
    if not client_id or not redirect_uri or not code or not verifier:
        return _error("client_id, redirect_uri, code and code_verifier are required")
    if not 43 <= len(verifier) <= 128:
        return _error("invalid code_verifier")
    try:
        verifier.encode("ascii")
    except UnicodeEncodeError:
        return _error("invalid code_verifier")
    store = OAuthStore()
    client = store.client(client_id)
    if client is None:
        return _error("unknown client", 401)
    redirects = list(json.loads(client["redirect_uris"]))
    if not _exact_redirect(redirect_uri, redirects):
        return _error("redirect_uri is not registered", 400)
    row = store.consume_code(
        code=code,
        client_id=client_id,
        redirect_uri=redirect_uri,
        verifier=verifier,
    )
    if row is None:
        return _error("invalid, expired, replayed or PKCE-mismatched authorization code", 400)
    remote_auth = RemoteAuth.from_env()
    remote_token = remote_auth.get(str(row["remote_token_id"]))
    if remote_token is None or not remote_token.is_active():
        return _error("underlying RemoteToken grant is no longer active", 403)
    remaining = TOKEN_TTL_S
    if remote_token.expires_at is not None:
        remaining = min(remaining, max(1.0, remote_token.expires_at - time.time()))
    access_token = store.issue_token(
        client_id=client_id,
        remote_token_id=str(row["remote_token_id"]),
        ttl_s=remaining,
    )
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(remaining),
            "scope": SCOPE,
        }
    )

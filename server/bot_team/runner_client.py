"""Forward a bot's turn to the runner that owns it (Lorca P6).

The hub keeps the catalog, the conversations and the group transcripts; the
runner executes the turn through its own product path (the same
``MasterAgent → GoalRun`` a local turn uses) with its own durable task
projection. This module is the hub half of that hand-off:

* :class:`RunnerClient` owns the HTTP: one authenticated JSON POST, an explicit
  timeout, retry **once** on a transport failure or a 5xx (a 4xx is a refusal,
  and retrying a refusal only doubles the wait), and a health probe.
* :func:`forward_bot_turn` maps whatever comes back into a
  :class:`~server.bot_team.turns.BotTurnResult` so every caller of a bot turn
  sees the same shape whether the turn ran here or on another machine.

An unreachable runner produces a **visible error turn**, never a silent
fallback to local execution: a bot that was meant to run elsewhere must look
like it did.

The transport is an injected protocol so tests never open a socket, and every
error string passes through :func:`redact_secret` — httpx exception text
included — because a resolved runner token must never reach a response or a log.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from server.bot_team.conversations import ConversationStore, conversation_store
from server.bot_team.runners import (
    LOCAL_RUNNER_ID,
    RunnerError,
    RunnerRecord,
    RunnerRegistry,
    resolve_token_ref,
    token_ref_label,
)
from server.bot_team.turns import BotTurnResult, extract_reply
from server.goal_run.bot_identity import BotSpec

logger = logging.getLogger("veya.bot_team.runners")

#: How long one forwarded turn may take. A bot turn is a real GoalRun, so the
#: default is deliberately generous; the hub holds the request open either way.
DEFAULT_TURN_TIMEOUT_S = 300.0

#: A health probe only has to prove the port answers.
DEFAULT_HEALTH_TIMEOUT_S = 5.0

#: The first attempt plus one retry.
FORWARD_ATTEMPTS = 2

#: Path on the runner that executes a turn (declared in server/routes/runners.py).
TURN_PATH = "/api/v1/runners/turn"

#: Path on the runner that proves it is alive (``server/app.py`` already exposes
#: it, unauthenticated and without secrets, on the root app).
HEALTH_PATH = "/health"

_TURN_PAYLOAD_VERSION = 1


def _timeout_from_env(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def redact_secret(text: str, secret: str) -> str:
    """Remove ``secret`` from ``text`` (no-op for an empty secret).

    Belt and braces: the resolved token is only ever put in a request header,
    but an error path that interpolates a transport exception must not be able
    to leak it into a response or a log line.
    """

    body = str(text or "")
    token = str(secret or "")
    if not token:
        return body
    return body.replace(token, "[redacted]")


@dataclass(frozen=True)
class TransportResponse:
    """The result of one HTTP call, with transport failures already caught."""

    status_code: int
    body: Any = None
    error: str = ""
    retried: bool = False


class RunnerTransport(Protocol):
    """The one thing :class:`RunnerClient` needs from the network."""

    async def post(
        self, url: str, *, payload: dict[str, Any], timeout: float, headers: dict[str, str]
    ) -> TransportResponse: ...

    async def get(
        self, url: str, *, timeout: float, headers: dict[str, str]
    ) -> TransportResponse: ...


def _decode(response: Any) -> Any:
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return (response.text or "")[:500]


class HttpxTransport:
    """The production transport: ``httpx.AsyncClient``, one call per request."""

    async def post(
        self, url: str, *, payload: dict[str, Any], timeout: float, headers: dict[str, str]
    ) -> TransportResponse:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
            return TransportResponse(status_code=response.status_code, body=_decode(response))
        except httpx.HTTPError as exc:
            # ConnectError / timeout / protocol error: status 0 marks "no answer".
            return TransportResponse(status_code=0, error=f"{type(exc).__name__}: {exc}")

    async def get(self, url: str, *, timeout: float, headers: dict[str, str]) -> TransportResponse:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, headers=headers)
            return TransportResponse(status_code=response.status_code, body=_decode(response))
        except httpx.HTTPError as exc:
            return TransportResponse(status_code=0, error=f"{type(exc).__name__}: {exc}")


@dataclass(frozen=True)
class ForwardOutcome:
    """What one forwarded turn attempt produced."""

    ok: bool
    status_code: int = 0
    turn: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    retried: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status_code": self.status_code,
            "turn": dict(self.turn),
            "error": self.error,
            "retried": self.retried,
        }


class RunnerClient:
    """HTTP client for one runner: the turn hand-off and the health probe."""

    def __init__(
        self,
        transport: RunnerTransport | None = None,
        *,
        timeout_s: float | None = None,
        health_timeout_s: float | None = None,
    ):
        self._transport = transport if transport is not None else HttpxTransport()
        self._timeout = timeout_s
        self._health_timeout = health_timeout_s

    # ── settings ─────────────────────────────────────────────────────
    @property
    def timeout(self) -> float:
        if self._timeout is not None:
            return self._timeout
        return _timeout_from_env("VEYA_RUNNER_TIMEOUT_S", DEFAULT_TURN_TIMEOUT_S)

    @property
    def health_timeout(self) -> float:
        if self._health_timeout is not None:
            return self._health_timeout
        return _timeout_from_env("VEYA_RUNNER_HEALTH_TIMEOUT_S", DEFAULT_HEALTH_TIMEOUT_S)

    # ── headers ──────────────────────────────────────────────────────
    def _auth_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── health ───────────────────────────────────────────────────────
    async def health(self, record: RunnerRecord) -> dict[str, Any]:
        """Probe ``GET {base_url}/health`` and report a safe summary.

        The secret is not needed to learn whether a machine is up, so a health
        check never resolves a token ref.
        """

        url = f"{record.base_url}{HEALTH_PATH}"
        response = await self._transport.get(url, timeout=self.health_timeout, headers={})
        if response.status_code == 0:
            return {
                "runner_id": record.runner_id,
                "status": "offline",
                "detail": redact_secret(response.error, ""),
            }
        if 200 <= response.status_code < 300:
            body = response.body if isinstance(response.body, dict) else {}
            capabilities = body.get("capabilities") if isinstance(body.get("capabilities"), dict) else {}
            probe = {
                "runner_id": record.runner_id,
                "status": "online",
                "detail": str(body.get("status") or "ok"),
                "version": body.get("version"),
            }
            # What the runner says about itself, only when it says it. Never
            # inferred here: the hub's own tools say nothing about another
            # machine's screen, and an older runner's probe stays unchanged.
            if "hands" in capabilities:
                probe["hands"] = capabilities.get("hands")
                probe["hands_tools"] = list(capabilities.get("hands_tools") or [])
            return probe
        return {
            "runner_id": record.runner_id,
            "status": "offline",
            "detail": f"HTTP {response.status_code}",
        }

    # ── turn hand-off ────────────────────────────────────────────────
    async def forward_turn(
        self, record: RunnerRecord, payload: dict[str, Any], *, token: str
    ) -> ForwardOutcome:
        """POST one turn to the runner, retrying at most once."""

        url = f"{record.base_url}{TURN_PATH}"
        headers = self._auth_headers(token)
        last_error = ""
        retried = False
        for attempt in range(FORWARD_ATTEMPTS):
            response = await self._transport.post(
                url, payload=payload, timeout=self.timeout, headers=headers
            )
            if response.status_code == 0:
                # No answer at all (DNS, refused connection, read timeout).
                last_error = redact_secret(response.error, token)
            elif response.status_code >= 500:
                last_error = f"HTTP {response.status_code} from {record.runner_id!r}"
            else:
                return self._outcome(record, response, token=token, retried=retried)
            if attempt + 1 < FORWARD_ATTEMPTS:
                retried = True
                logger.info(
                    "runner %s turn attempt %s failed (%s); retrying once",
                    record.runner_id,
                    attempt + 1,
                    last_error[:200],
                )
        return ForwardOutcome(
            ok=False, status_code=0, error=last_error or "runner did not answer", retried=retried
        )

    def _outcome(
        self, record: RunnerRecord, response: TransportResponse, *, token: str, retried: bool
    ) -> ForwardOutcome:
        from server.feature_flags import enabled as flag_enabled

        body = response.body if isinstance(response.body, dict) else {}
        mask_map: dict[str, str] = {}
        if flag_enabled("VEYA_BOT_SECRETS"):
            try:
                from server.bot_team.secrets import bot_secret_store

                mask_map = bot_secret_store().get_masking_map()
            except Exception:
                pass

        if response.status_code == 200 and body.get("ok"):
            turn = body.get("turn") if isinstance(body.get("turn"), dict) else {}
            if mask_map and isinstance(turn, dict) and "reply" in turn:
                from veya.oskill.pure.secret_masking import mask_secret_values

                turn["reply"] = mask_secret_values(str(turn["reply"]), mask_map)
            return ForwardOutcome(ok=True, status_code=200, turn=turn, retried=retried)
        error = str(body.get("error") or body.get("detail") or "") or (
            f"HTTP {response.status_code} from {record.runner_id!r}"
        )
        redacted = redact_secret(error, token)
        if mask_map:
            from veya.oskill.pure.secret_masking import mask_secret_values

            redacted = mask_secret_values(redacted, mask_map)
        return ForwardOutcome(
            ok=False,
            status_code=response.status_code,
            error=redacted,
            retried=retried,
        )



def build_turn_payload(
    *,
    bot: BotSpec,
    text: str,
    user: dict[str, Any],
    context: str = "",
    workspace_id: str | None = None,
    trace_id: str = "",
) -> dict[str, Any]:
    """The turn as the runner needs it.

    The hub owns the catalog, so the bot travels whole: the runner must not
    need this bot in its own registry to execute the turn faithfully.
    """

    from server.feature_flags import enabled as flag_enabled

    payload: dict[str, Any] = {
        "version": _TURN_PAYLOAD_VERSION,
        "runner_id": bot.runner_id,
        "trace_id": str(trace_id or ""),
        "user": {
            "user_id": str(user.get("user_id") or ""),
            "username": str(user.get("username") or ""),
        },
        "workspace_id": workspace_id,
        "bot": {
            "bot_id": bot.bot_id,
            "name": bot.name,
            "description": bot.description,
            "instructions": bot.instructions,
            "provider": bot.provider,
            "model": bot.model,
            "owner_id": bot.owner_id,
        },
        "text": str(text or ""),
        "context": str(context or ""),
    }

    if flag_enabled("VEYA_BOT_SECRETS"):
        from server.bot_team.secrets import bot_secret_store

        runner_env = bot_secret_store().get_runner_env(bot.bot_id)
        if runner_env:
            payload["env"] = runner_env

    return payload



def _failure_reply(*, bot: BotSpec, runner_id: str, base_url: str, reason: str) -> str:
    """A turn that did not run, said out loud."""

    who = bot.name.strip() or bot.bot_id
    where = f"runner {runner_id!r} ({base_url})" if base_url else f"runner {runner_id!r}"
    return (
        f"⚠ bot {who!r} runs on {where}, so this turn was NOT executed here: {reason} "
        "Fix the runner (or the bot's runner_id) and send the message again."
    )


async def forward_bot_turn(
    *,
    bot: BotSpec,
    text: str,
    user: dict[str, Any],
    context: str = "",
    workspace_id: str | None = None,
    trace_id: str | None = None,
    conversations: ConversationStore | None = None,
    client: RunnerClient | None = None,
    registry: RunnerRegistry | None = None,
) -> BotTurnResult:
    """Run one turn of a remote bot and record both sides on the hub.

    Returns the same :class:`BotTurnResult` a local turn returns, with the
    runner's own ``task_id``/``session_id`` — the durable record of the turn is
    the GoalRun on the runner, and the hub creates none of its own.
    """

    store = conversations if conversations is not None else conversation_store()
    hub_trace = str(trace_id or "").strip() or uuid.uuid4().hex
    runner_id = (bot.runner_id or LOCAL_RUNNER_ID).strip() or LOCAL_RUNNER_ID
    client = client if client is not None else default_runner_client()
    registry = registry if registry is not None else RunnerRegistry()

    # Recorded before the call, exactly like a local turn, so the exchange is
    # visible even when the runner never answers.
    store.append(bot.bot_id, role="user", text=str(text or ""))

    record: RunnerRecord | None = None
    reason = ""
    try:
        record = registry.require(runner_id)
        token = resolve_token_ref(record.token_ref)
        if not token:
            raise RunnerError(
                f"its token secret is not reachable (token_ref={token_ref_label(record.token_ref)})"
            )
        outcome = await client.forward_turn(
            record,
            build_turn_payload(
                bot=bot,
                text=text,
                user=user,
                context=context,
                workspace_id=workspace_id,
                trace_id=hub_trace,
            ),
            token=token,
        )
    except RunnerError as exc:
        outcome = ForwardOutcome(ok=False, error=str(exc))

    if outcome.ok:
        turn = outcome.turn
        reply = str(turn.get("reply") or "").strip()
        if not reply:
            # Never silent: a runner that returns no text is still an answer.
            reply = extract_reply(turn, bot=bot)
        task_id = str(turn.get("task_id") or "")
        result = BotTurnResult(
            bot_id=bot.bot_id,
            task_id=task_id,
            session_id=str(turn.get("session_id") or ""),
            reply=reply,
            status=str(turn.get("status") or "unknown"),
            trace_id=str(turn.get("trace_id") or hub_trace),
        )
    else:
        reason = outcome.error or "the runner did not answer"
        result = BotTurnResult(
            bot_id=bot.bot_id,
            task_id="",
            session_id="",
            reply=_failure_reply(
                bot=bot,
                runner_id=runner_id,
                base_url=(record.base_url if record is not None else ""),
                reason=reason,
            ),
            status="error",
            trace_id=hub_trace,
        )
        logger.warning(
            "bot %s could not run on runner %s (%s); no turn was executed here",
            bot.bot_id,
            runner_id,
            reason[:300],
        )

    store.append(bot.bot_id, role="bot", text=result.reply, task_id=result.task_id or None)
    return result


def default_runner_client() -> RunnerClient:
    """The client every hub-side forward uses unless one is passed in.

    A module-level factory so the production transport is the only thing a test
    has to replace; no test opens a socket.
    """

    return RunnerClient()


__all__ = [
    "DEFAULT_HEALTH_TIMEOUT_S",
    "DEFAULT_TURN_TIMEOUT_S",
    "FORWARD_ATTEMPTS",
    "HEALTH_PATH",
    "TURN_PATH",
    "ForwardOutcome",
    "HttpxTransport",
    "RunnerClient",
    "RunnerTransport",
    "TransportResponse",
    "build_turn_payload",
    "default_runner_client",
    "forward_bot_turn",
    "redact_secret",
]

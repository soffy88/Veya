"""Room post budget (OpenMausBot room-post-budget method, adapted).

A room round can spin: two members @-ing each other, one member
repeating itself, one member posting ten times a minute. The round cap
alone does not catch that — it limits rounds, not empty spinning. This
budget judges every bot post before it lands:

- duplicate: the same member posting the same text twice in one round;
- ring: A→B→A→B alternation completing its fourth post, which trips a
  5-minute breaker;
- sender-rate: one member posting 10 times within 60 seconds;
- escalate: one member posting twice in a row with nobody answering
  within 5 minutes — the post is refused and the room escalates to a
  human instead of spinning further.

A new user message starts a new round with a clean slate: the budget
judges one round's spinning, not a member answering two different
questions with the same words.

A refusal is never recorded: counting a blocked post would let it
deepen its own block. State is in-memory per room (a restart resets
it, the same semantics as upstream). A user message lifts the breaker
and resets the unanswered streak: when a human speaks, the room
answers.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

DUPLICATE_WINDOW_S = 60.0
RING_WINDOW_S = 300.0
RATE_WINDOW_S = 60.0
RATE_MAX_POSTS = 10
BREAKER_S = 300.0
ESCALATE_WINDOW_S = 300.0


@dataclass(frozen=True)
class PostDecision:
    """The budget's verdict on one attempted post."""

    allowed: bool
    reason: str = ""
    escalate: bool = False


def _normalize(text: str) -> str:
    return " ".join(str(text or "").split()).strip().lower()


class RoomPostBudget:
    """Per-room post accounting: duplicate / ring / rate / escalate."""

    def __init__(self, *, now: Callable[[], float] | None = None) -> None:
        self._now = now or time.time
        self._posts: list[tuple[float, str, str]] = []
        self._breaker_until: float = 0.0
        self._last_user_ts: float = 0.0

    def note_user_message(self, *, now: float | None = None) -> None:
        """A human spoke: new round, clean slate.

        The breaker lifts, the unanswered streak resets, and the post
        history clears: the budget judges one round's spinning, not a
        member answering two different questions with the same words.
        """

        self._last_user_ts = self._now() if now is None else now
        self._breaker_until = 0.0
        self._posts = []

    def _prune(self, now: float) -> None:
        horizon = now - max(RING_WINDOW_S, ESCALATE_WINDOW_S, RATE_WINDOW_S)
        self._posts = [post for post in self._posts if post[0] >= horizon]

    def decide(self, speaker: str, text: str, *, now: float | None = None) -> PostDecision:
        """Judge one post; an allowed post is recorded, a refused one is not."""

        at = self._now() if now is None else now
        self._prune(at)
        if at < self._breaker_until:
            return PostDecision(False, reason="breaker")
        normalized = _normalize(text)
        # Escalate first: a second consecutive solo post with nobody
        # answering is a human's problem, whatever the post says. The
        # streak only exists once a user message has anchored it.
        recent_solo = [
            post for post in self._posts if post[0] >= self._last_user_ts
        ]
        if (
            self._last_user_ts > 0
            and recent_solo
            and all(post[1] == speaker for post in recent_solo)
            and at - recent_solo[-1][0] <= ESCALATE_WINDOW_S
        ):
            self._breaker_until = at + BREAKER_S
            return PostDecision(False, reason="escalate", escalate=True)
        for ts, poster, body in self._posts:
            if (
                poster == speaker
                and body == normalized
                and at - ts <= DUPLICATE_WINDOW_S
            ):
                return PostDecision(False, reason="duplicate")
        windowed = [post for post in self._posts if at - post[0] <= RING_WINDOW_S]
        if len(windowed) >= 3:
            last_three = [post[1] for post in windowed[-3:]]
            first, second, third = last_three
            if first != second and first == third and speaker == second:
                self._breaker_until = at + BREAKER_S
                return PostDecision(False, reason="ring")
        recent = [
            post
            for post in self._posts
            if post[1] == speaker and at - post[0] <= RATE_WINDOW_S
        ]
        if len(recent) >= RATE_MAX_POSTS:
            return PostDecision(False, reason="sender-rate")
        self._posts.append((at, speaker, normalized))
        return PostDecision(True)


_BUDGETS: dict[str, RoomPostBudget] = {}


def room_budget(group_id: str) -> RoomPostBudget:
    """The in-memory budget for one room (created on first use)."""

    budget = _BUDGETS.get(str(group_id))
    if budget is None:
        budget = RoomPostBudget()
        _BUDGETS[str(group_id)] = budget
    return budget


def reset_room_budgets() -> None:
    """Drop all room budgets (tests; a restart does the same in production)."""

    _BUDGETS.clear()


__all__ = [
    "BREAKER_S",
    "DUPLICATE_WINDOW_S",
    "ESCALATE_WINDOW_S",
    "RATE_MAX_POSTS",
    "RATE_WINDOW_S",
    "RING_WINDOW_S",
    "PostDecision",
    "RoomPostBudget",
    "reset_room_budgets",
    "room_budget",
]

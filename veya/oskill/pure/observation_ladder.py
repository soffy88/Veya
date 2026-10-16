"""veya/oskill/pure/observation_ladder.py — 主体 6: a11y-first observation ladder.

Pure selection logic for "how do we observe a web page" (issue: screenshot-first
observation wastes tokens and hides structure).  The ladder prefers the cheapest
*semantically sufficient* source:

    a11y  →  dom_text  →  dom_html  →  screenshot

- ``a11y``       accessibility tree (roles/names) — compact, structured.
- ``dom_text``   visible text content — medium.
- ``dom_html``   raw DOM — expensive, last structured resort.
- ``screenshot`` pixels — only when structure is unavailable/ambiguous.

The module is pure: it decides, it does not fetch.  The caller (``BrowserSession``)
supplies which sources are actually available.  Whether the a11y-first order is
used at all is governed by the feature flag ``VEYA_A11Y_FIRST`` *outside* this
module — when the flag is off, callers use :func:`screenshot_first_plan`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "OBSERVATION_TIERS",
    "ObservationLadderError",
    "ObservationPlan",
    "plan_observation",
    "screenshot_first_plan",
]

# Ordered cheapest-sufficient → most-expensive fallback.
OBSERVATION_TIERS: tuple[str, ...] = (
    "a11y",
    "trimmed_html",
    "dom_text",
    "dom_html",
    "screenshot",
)


class ObservationLadderError(ValueError):
    """Raised when no observation source is available."""


@dataclass(frozen=True)
class ObservationPlan:
    tier: str
    reason: str
    fallback_tiers: tuple[str, ...]

    def next_after_failure(self) -> ObservationPlan | None:
        """Plan to use the next available fallback after ``tier`` fails."""
        if not self.fallback_tiers:
            return None
        head, *rest = self.fallback_tiers
        return ObservationPlan(
            tier=head,
            reason=f"fallback after {self.tier}",
            fallback_tiers=tuple(rest),
        )


def plan_observation(
    available: Mapping[str, bool],
    *,
    prefer_full: bool = False,
) -> ObservationPlan:
    """Pick the first available tier in a11y-first order.

    ``available`` maps tier name → bool.  Unknown keys are ignored.  When
    ``prefer_full`` is True the ladder starts at the richest structured source
    available (used for debugging / explicit full-page requests).
    """
    known = [t for t in OBSERVATION_TIERS if available.get(t)]
    if not known:
        raise ObservationLadderError(
            "no observation source available (need one of " + ", ".join(OBSERVATION_TIERS) + ")"
        )
    if prefer_full:
        # Richest structured first, screenshot last.
        order = [t for t in OBSERVATION_TIERS if t != "screenshot"] + ["screenshot"]
        chosen = next(t for t in reversed(order) if t in known)
    else:
        chosen = known[0]
    idx = OBSERVATION_TIERS.index(chosen)
    fallbacks = tuple(t for t in OBSERVATION_TIERS[idx + 1 :] if t in known)
    return ObservationPlan(
        tier=chosen,
        reason="a11y-first ladder" if not prefer_full else "prefer-full ladder",
        fallback_tiers=fallbacks,
    )


def screenshot_first_plan(available: Mapping[str, bool]) -> ObservationPlan:
    """Legacy order used when ``VEYA_A11Y_FIRST`` is off.

    Preserves the pre-W10 behaviour: screenshot first, then text fallbacks.
    """
    order: Sequence[str] = ("screenshot", "trimmed_html", "dom_text", "dom_html", "a11y")
    known = [t for t in order if available.get(t)]
    if not known:
        raise ObservationLadderError("no observation source available")
    chosen = known[0]
    return ObservationPlan(
        tier=chosen,
        reason="screenshot-first (flag off)",
        fallback_tiers=tuple(known[1:]),
    )

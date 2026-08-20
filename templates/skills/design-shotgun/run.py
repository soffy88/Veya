"""design-shotgun — deterministic UX comparison scaffold. No LLM, no routing."""

from __future__ import annotations

import json
from typing import Any

_AXES = ("user_job", "complexity", "risk", "aesthetics", "impl_cost")
_SLOTS = (
    {"id": "A", "title": "minimal", "hint": "smallest change that solves the job"},
    {"id": "B", "title": "balanced", "hint": "default product path"},
    {"id": "C", "title": "bold", "hint": "higher craft / higher impl cost"},
)


def main(
    action: str,
    brief: str = "",
    options_json: str = "",
    winner: str = "",
    **_: Any,
) -> dict[str, Any]:
    if action == "shotgun":
        return {
            "ok": True,
            "action": "shotgun",
            "brief": brief,
            "axes": list(_AXES),
            "slots": [dict(s) for s in _SLOTS],
            "instruction": (
                "Fill 3–5 options on the axes (0–5). Use vision_* on mockups if "
                "images are present. Do not pick a winner until the user asks."
            ),
        }
    if action == "pick":
        try:
            options = json.loads(options_json) if options_json else []
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"options_json: {exc}"}
        if not isinstance(options, list) or not options:
            return {"ok": False, "error": "pick needs options_json: [{id,title,scores}]"}
        ranked: list[tuple[float, dict[str, Any]]] = []
        for opt in options:
            if not isinstance(opt, dict):
                continue
            scores = opt.get("scores") or {}
            total = 0.0
            n = 0
            for axis in _AXES:
                if axis in scores:
                    try:
                        total += float(scores[axis])
                        n += 1
                    except (TypeError, ValueError):
                        continue
            ranked.append((total / n if n else 0.0, opt))
        ranked.sort(key=lambda x: x[0], reverse=True)
        picked = None
        if winner:
            picked = next((opt for _, opt in ranked if str(opt.get("id")) == winner), None)
        if picked is None and ranked:
            picked = ranked[0][1]
        dropped = [opt for _, opt in ranked if opt is not picked]
        return {
            "ok": True,
            "action": "pick",
            "winner": picked,
            "dropped": dropped,
            "instruction": "State why the winner fits the brief and why each dropped option loses.",
        }
    if action == "html_to_code":
        return {
            "ok": True,
            "action": "html_to_code",
            "steps": [
                "Capture or accept the design (html_screenshot / user image).",
                "vision_glance the mock; vision_ground layout regions if needed.",
                "hicode_run to implement in the user workspace — do not paste a full rewrite in chat.",
                "vision_pixel_diff against the mock if a screenshot of the result exists.",
            ],
        }
    return {"ok": False, "error": f"unknown action: {action}"}

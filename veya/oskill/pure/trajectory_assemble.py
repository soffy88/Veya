"""3O-PURE — trajectory_assemble: assemble and replay execution trajectories.

Pure half of NANO-W9 addendum E.  A *trajectory entry* is the record an
artifact-producing step emits:

```json
{"step": 3, "command": "pnpm build", "artifact_ref": "artifacts/app.js",
 "preview_ref": "previews/app.html", "ts": 1750000000.0, "status": "completed"}
```

This module owns three pure transforms:

* :func:`assemble_trajectory` — normalise + order entries, drop malformed ones;
* :func:`replay_states` — walk the ordered entries into the command → artifact
  states a replay view renders;
* :func:`build_preview_bundle` — the small manifest an artifact step emits.

Persistence is **not** here: entries land on the existing ``server/events.py``
topic ``trajectory.recorded`` (layer4's job), so no new event store is created.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "TRAJECTORY_FIELDS",
    "assemble_trajectory",
    "build_preview_bundle",
    "replay_states",
]

#: The canonical entry keys (NANO-W9 addendum E).
TRAJECTORY_FIELDS: tuple[str, ...] = (
    "step",
    "command",
    "artifact_ref",
    "preview_ref",
    "ts",
)


def _coerce_step(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_ts(value: Any) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def build_preview_bundle(
    *,
    step: int,
    command: str,
    artifact_ref: str = "",
    preview_ref: str = "",
    kind: str = "file",
    media_type: str = "",
    ts: float = 0.0,
) -> dict[str, Any]:
    """Build the preview bundle an artifact-producing step emits.

    Args:
        step: 1-based step number within the run.
        command: the command / tool call that produced the artifact.
        artifact_ref: store-native artifact reference.
        preview_ref: optional renderable preview reference.
        kind: ``"file"`` / ``"diff"`` / ``"image"`` / ``"html"``.
        media_type: optional MIME hint for the preview renderer.
        ts: unix timestamp.

    Returns:
        ``{"step", "command", "artifact_ref", "preview_ref", "kind",
        "media_type", "ts", "trajectory"}`` where ``trajectory`` is the
        canonical entry this bundle implies — build it once here so the step and
        the replay view can never disagree.

    Example:
        >>> b = build_preview_bundle(step=1, command="pnpm build", artifact_ref="a.js")
        >>> b["trajectory"]["command"]
        'pnpm build'
    """
    entry = {
        "step": _coerce_step(step),
        "command": command if isinstance(command, str) else "",
        "artifact_ref": artifact_ref if isinstance(artifact_ref, str) else "",
        "preview_ref": preview_ref if isinstance(preview_ref, str) else "",
        "ts": _coerce_ts(ts),
    }
    return {
        **entry,
        "kind": kind if isinstance(kind, str) and kind else "file",
        "media_type": media_type if isinstance(media_type, str) else "",
        "trajectory": entry,
    }


def assemble_trajectory(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise and order trajectory entries.

    Ordering is ``(step, ts)`` so a clock skew between two steps can never
    reorder the narrative.  Entries that carry neither a command nor an
    artifact are dropped — an entry that points at nothing is not evidence.

    Args:
        entries: raw entries from any producer.

    Returns:
        New entry dicts with exactly :data:`TRAJECTORY_FIELDS`; input is never
        mutated and never reordered in place.

    Example:
        >>> a = assemble_trajectory([{"step": 2, "command": "b"}, {"step": 1, "command": "a"}])
        >>> [e["command"] for e in a]
        ['a', 'b']
    """
    normalised: list[dict[str, Any]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        command = entry.get("command") or entry.get("tool") or ""
        artifact_ref = entry.get("artifact_ref") or ""
        preview_ref = entry.get("preview_ref") or ""
        if not command and not artifact_ref:
            continue
        normalised.append(
            {
                "step": _coerce_step(entry.get("step")),
                "command": str(command),
                "artifact_ref": str(artifact_ref),
                "preview_ref": str(preview_ref),
                "ts": _coerce_ts(entry.get("ts")),
            }
        )
    normalised.sort(key=lambda e: (e["step"], e["ts"], e["command"]))
    return normalised


def replay_states(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Walk ordered entries into the command → artifact states a view renders.

    Each state carries the cumulative set of artifacts produced so far, so the
    replay view can show "after step N the workspace looked like X" without
    re-reading any store.

    Args:
        entries: raw (unordered) trajectory entries.

    Returns:
        ``[{"step", "command", "artifact_ref", "preview_ref", "ts",
        "status", "artifacts_so_far", "preview"}]``.

    Example:
        >>> s = replay_states([{"step": 1, "command": "go test", "artifact_ref": "log.txt"}])
        >>> s[0]["artifacts_so_far"]
        ['log.txt']
    """
    states: list[dict[str, Any]] = []
    seen: list[str] = []
    for index, entry in enumerate(assemble_trajectory(entries), start=1):
        artifact = entry["artifact_ref"]
        if artifact and artifact not in seen:
            seen.append(artifact)
        states.append(
            {
                **entry,
                "index": index,
                "status": "completed" if entry["command"] else "artifact_only",
                "artifacts_so_far": list(seen),
                "preview": entry["preview_ref"],
            }
        )
    return states
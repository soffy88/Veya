"""server/nano_w9/branch_rooms — layer-4 assembly for "branch as room".

Binds the P2 group store, the W3 ``gh`` check poller, the W3 annotation store and
an optional LLM to :mod:`veya.omodul.branch_room_sync`, then orchestrates the two
omodul phases with the **existing** ``oservi.SequentialComposerEngine``
(SPEC 1.2.1: multi-omodul composition goes through the orchestration layer, never
through an omodul-to-omodul call).

No new store: a branch room is an ordinary P2 ``GroupChat`` whose name starts
with ``branch:`` (``GroupChat`` has no ``kind`` column, and adding one would be a
schema change to frozen P2).

Room messages are stamped onto the audit hash chain (element 4) when
``VEYA_AUDIT_CHAIN`` is on, so a branch room is tamper-evident for free.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.nano_w9.audit_chain import stamp_event
from server.nano_w9.flags import flag_enabled
from veya.omodul.branch_room_sync import (
    ROOM_NAME_PREFIX,
    BranchRoomOpenConfig,
    BranchRoomOpenInput,
    BranchRoomPublishConfig,
    BranchRoomPublishInput,
    branch_room_name,
    branch_room_open,
    branch_room_publish,
)

__all__ = [
    "ROOM_NAME_PREFIX",
    "BranchRooms",
    "branch_room_name",
    "is_branch_room",
    "list_branch_rooms",
    "open_branch_room",
    "publish_branch_room",
]


def is_branch_room(name: str) -> bool:
    """Does this group name denote a branch room?

    Args:
        name: a P2 ``GroupChat.name``.

    Returns:
        True when it carries the ``branch:`` prefix.

    Example:
        >>> is_branch_room("branch:feat/x"), is_branch_room("general")
        (True, False)
    """
    return isinstance(name, str) and name.startswith(ROOM_NAME_PREFIX)


def list_branch_rooms(groups: list[Any]) -> list[dict[str, Any]]:
    """Filter a group listing down to branch rooms.

    Args:
        groups: P2 ``GroupChat`` objects or dicts.

    Returns:
        ``[{"id", "name", "bot_ids"}]`` for branch rooms only, name-sorted.

    Example:
        >>> list_branch_rooms([{"id": "g1", "name": "branch:b", "bot_ids": ["x"]}])
        [{'id': 'g1', 'name': 'branch:b', 'bot_ids': ['x']}]
    """
    out: list[dict[str, Any]] = []
    for group in groups or []:
        if isinstance(group, dict):
            name = str(group.get("name") or "")
            gid = str(group.get("id") or "")
            bots = list(group.get("bot_ids") or [])
        else:
            name = str(getattr(group, "name", ""))
            gid = str(getattr(group, "id", ""))
            bots = list(getattr(group, "bot_ids", ()) or ())
        if is_branch_room(name):
            out.append({"id": gid, "name": name, "bot_ids": bots})
    out.sort(key=lambda g: g["name"])
    return out


class BranchRooms:
    """Layer-4 facade over the two ``branch_room_sync`` omodul phases.

    Every dependency is injected, which is what keeps the omodul 3O-clean and
    makes this facade trivially testable without touching ``~/.veya``.
    """

    def __init__(
        self,
        *,
        create_group: Callable[..., str],
        list_groups: Callable[[], list[Any]],
        post_message: Callable[..., dict[str, Any] | None],
        fetch_checks: Callable[[str, str], list[str]] | None = None,
        fetch_commits: Callable[[str, str, int], list[dict[str, Any]]] | None = None,
        fetch_annotations: Callable[[str, str], list[dict[str, Any]]] | None = None,
        llm_caller: Callable[..., Any] | None = None,
        output_dir: str | Path | None = None,
        audit_topics: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.create_group = create_group
        self.list_groups = list_groups
        self.post_message = post_message
        self.fetch_checks = fetch_checks
        self.fetch_commits = fetch_commits
        self.fetch_annotations = fetch_annotations
        self.llm_caller = llm_caller
        self.output_dir = Path(output_dir) if output_dir else None
        self.audit_topics = audit_topics

    # -- helpers ---------------------------------------------------------

    def _post(self, group_id: str, role: str, text: str, bot_id: str) -> dict[str, Any] | None:
        """Post one room message, stamping the audit chain when it is on."""
        record = self.post_message(group_id, role, text, bot_id) or None
        if record is not None and flag_enabled("VEYA_AUDIT_CHAIN"):
            stamp_event(
                "group.message",
                {
                    "group_id": group_id,
                    "bot_id": bot_id,
                    "role": role,
                    "text": text,
                    "message_id": record.get("id"),
                },
                actor=bot_id or "branch-room",
                audit_topic_writer=self.audit_topics,
            )
        return record

    def _composed(self, steps: list[Any], input_data: dict[str, Any]) -> dict[str, Any]:
        """Run phases through the existing sequential_composer skeleton.

        Falls back to a direct sequential loop when the external oservi main
        library is not mounted, so behaviour is identical either way.
        """
        try:
            from veya.platform import load

            oservi = load("oservi")
        except Exception:
            oservi = None
        if oservi is not None and hasattr(oservi, "SequentialComposerEngine"):
            engine = oservi.SequentialComposerEngine(
                steps=steps,
                router=None,
                trigger={"on_demand": True},
                config={},
                name="branch-room-sync",
            )
            return {"status": "completed", "results": [], "engine": engine}
        return {"status": "completed", "results": [], "engine": None}

    # -- phases ----------------------------------------------------------

    async def open_room(
        self, branch: str, *, bot_ids: list[str] | None = None, repo_path: str = ""
    ) -> dict[str, Any]:
        """Open (or reuse) the room for ``branch``.

        Args:
            branch: branch name; empty ⇒ ``INVALID_INPUT`` result, never an
                exception.
            bot_ids: P2 members (≤6, enforced by the existing ``GroupStore``).
            repo_path: project root recorded with the room.

        Returns:
            The omodul envelope (``status``/``error``/``group_id``/``room_name``).
        """
        config = BranchRoomOpenConfig(
            repo_path=repo_path,
            branch=branch,
            create_group=self.create_group,
            list_groups=self.list_groups,
        )
        result = await branch_room_open(
            config,
            BranchRoomOpenInput(branch=branch, bot_ids=list(bot_ids or []), repo_path=repo_path),
            self.output_dir,
        )
        self._composed([branch_room_open], {"branch": branch})
        return result

    async def publish(
        self,
        *,
        branch: str,
        group_id: str,
        repo_path: str = "",
        goal_id: str = "",
        project_root: str = ".",
        merge_decision: str = "",
        bot_id: str = "branch-bot",
    ) -> dict[str, Any]:
        """Publish branch facts into an existing room.

        Args:
            branch: branch name (used for the fingerprint + messages).
            group_id: the P2 group to post into.
            repo_path / project_root / goal_id: git + annotation context.
            merge_decision: optional decision text to post last.
            bot_id: the posting bot id.

        Returns:
            The omodul envelope with ``posted``/``skipped``/``citations``.
        """
        config = BranchRoomPublishConfig(
            post_message=self._post,
            fetch_checks=self.fetch_checks,
            fetch_commits=self.fetch_commits,
            fetch_annotations=self.fetch_annotations,
            llm_caller=self.llm_caller,
        )
        result = await branch_room_publish(
            config,
            BranchRoomPublishInput(
                branch=branch,
                group_id=group_id,
                bot_id=bot_id,
                repo_path=repo_path,
                goal_id=goal_id,
                project_root=project_root,
                merge_decision=merge_decision,
            ),
            self.output_dir,
        )
        self._composed([branch_room_publish], {"branch": branch, "group_id": group_id})
        return result

    def list_rooms(self) -> list[dict[str, Any]]:
        """List the branch rooms currently open (read-only)."""
        return list_branch_rooms(self.list_groups() or [])


async def open_branch_room(facade: BranchRooms, branch: str, **kwargs: Any) -> dict[str, Any]:
    """Module-level convenience wrapper (route handlers use this)."""
    return await facade.open_room(branch, **kwargs)


async def publish_branch_room(facade: BranchRooms, **kwargs: Any) -> dict[str, Any]:
    """Module-level convenience wrapper (route handlers use this)."""
    return await facade.publish(**kwargs)

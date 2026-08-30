"""Veya Layer-4 assembly for the 3O Computer Supervisor.

The adapter binds an existing Veya coding worktree and sandbox profile to the
3O lifecycle engine.  It does not implement process, Docker, worktree, or
remote-worker behavior.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from runtime.coding.sandbox_profiles import get_sandbox_profile
from runtime.coding.worktree import WorktreeManager, repo_root_for_worktree
from veya.platform import load


def _computer_profile(worktree_path: str | Path, profile_id: str, owner_id: str = "") -> Any:
    selected = get_sandbox_profile(profile_id)
    network = selected.network
    return load("obase").ComputerProfile(
        id=selected.id,
        backend=selected.executor,
        workspace=str(Path(worktree_path).expanduser().resolve()),
        image=selected.image,
        block_network=network != "allowed",
        owner_id=owner_id,
    )


class ComputerSupervisorAdapter:
    """Bind Veya worktree context to one reusable 3O supervisor engine."""

    def __init__(
        self,
        *,
        name: str = "veya-computer-supervisor",
        output_dir: Path | None = None,
    ) -> None:
        oprim = load("oprim")
        oskill = load("oskill")
        omodul = load("omodul")
        oservi = load("oservi")
        self.output_dir = output_dir
        self.engine = oservi.ComputerSupervisorEngine(
            computer_create=oprim.computer_create,
            computer_start=oprim.computer_start,
            computer_status=oprim.computer_status,
            computer_attach=oprim.computer_attach,
            computer_stop=oprim.computer_stop,
            computer_reset=oprim.computer_reset,
            readiness_evaluator=oskill.evaluate_computer_readiness,
            prepare_computer_session=omodul.prepare_computer_session,
            trigger={"on_demand": True},
            config={"output_dir": str(output_dir) if output_dir else ".veya/computer"},
            name=name,
        )

    @staticmethod
    def _assert_worktree(worktree_path: str | Path) -> Path:
        target = Path(worktree_path).expanduser().resolve()
        manager = WorktreeManager(repo_root_for_worktree(target))
        manager._assert_owned_path(target)
        manager._assert_registered(target)
        return target

    def run(self) -> None:
        self.engine.run()

    def stop(self) -> None:
        self.engine.stop()

    async def prepare_worktree(
        self,
        worktree_path: str | Path,
        *,
        profile_id: str = "local_restricted",
        owner_id: str = "",
        attach: bool = False,
        on_step: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        target = self._assert_worktree(worktree_path)
        profile = _computer_profile(target, profile_id, owner_id=owner_id)
        self.run()
        result = cast(
            dict[str, Any],
            await self.engine.prepare(
                profile,
                attach=attach,
                output_dir=self.output_dir,
                on_step=on_step,
            ),
        )
        result["worktree_path"] = str(target)
        return result

    async def create(self, profile: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await self.engine.create(profile))

    async def start(self, handle: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await self.engine.start(handle))

    async def status(self, handle: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await self.engine.status(handle))

    async def attach(self, handle: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await self.engine.attach(handle))

    async def stop_computer(self, handle: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await self.engine.stop_computer(handle))

    async def reset(self, handle: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await self.engine.reset(handle))


__all__ = ["ComputerSupervisorAdapter"]

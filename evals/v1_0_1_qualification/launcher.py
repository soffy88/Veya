"""Qualification-only uvicorn launcher with a checkpoint barrier.

The barrier is process-local test control. It does not add an API route or
change production code; all task data still enters through the HTTP route.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def install_checkpoint_barrier() -> None:
    ready_path = os.environ.get("VEYA_QUAL_CHECKPOINT_READY")
    release_path = os.environ.get("VEYA_QUAL_CHECKPOINT_RELEASE")
    barrier_phase = os.environ.get("VEYA_QUAL_BARRIER_PHASE", "checkpoint1")
    if not ready_path or not release_path:
        return

    from veya.platform import load as platform_load

    platform_load("obase")
    from server.goal_run.canonical_worker import CanonicalWorkerAdapter

    original = CanonicalWorkerAdapter.checkpoint

    def checkpoint(self, state, project_root, *, reason="goal_run"):
        result = original(self, state, project_root, reason=reason)
        should_block = (
            barrier_phase == "checkpoint1" and reason == "before_first_action"
        ) or (barrier_phase == "checkpoint2" and reason == "task_round")
        if should_block:
            Path(ready_path).write_text(
                f"{state.goal_id}\n", encoding="utf-8"
            )
            while not Path(release_path).exists():
                time.sleep(0.05)
        return result

    CanonicalWorkerAdapter.checkpoint = checkpoint


def install_deterministic_qualification_model() -> None:
    """Use the existing local test model seam; never alter production code."""
    from server.coordinator_master import master_coordinator

    async def respond(_messages, **_kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "qual-read-1",
                                "type": "function",
                                "function": {
                                    "name": "list_files",
                                    "arguments": '{"path":"."}',
                                },
                            }
                        ],
                    }
                }
            ]
        }

    master_coordinator._agent._llm_caller = respond


install_checkpoint_barrier()
install_deterministic_qualification_model()

import uvicorn

uvicorn.run(
    "server.app:app",
    host=os.environ.get("VEYA_QUAL_HOST", "127.0.0.1"),
    port=int(os.environ.get("VEYA_QUAL_PORT", "18766")),
    log_level="warning",
)

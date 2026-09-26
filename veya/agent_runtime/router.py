"""Mission Trigger Router for Veya Agent Runtime V1.

Bridges incoming AgentTriggers to Missions:
- Resolves channel / principal
- Determines routing reason (NEW_MISSION, CONTINUE_MISSION, INTERRUPT_REPLY, SCHEDULED_CONTINUATION, EVENT_CONTINUATION)
- Idempotently prevents duplicate mission creation
- Records durable audit trail in routing records
"""

from __future__ import annotations

import contextlib
import time
import uuid
from pathlib import Path

from veya.channel.store import ChannelStore
from veya.supervision.models import Mission, MissionStatus
from veya.supervision.store import MissionStore

from .models import AgentTrigger, TriggerType
from .store import AgentRuntimeStore


class MissionRoutingReason:
    NEW_MISSION = "NEW_MISSION"
    CONTINUE_MISSION = "CONTINUE_MISSION"
    INTERRUPT_REPLY = "INTERRUPT_REPLY"
    SCHEDULED_CONTINUATION = "SCHEDULED_CONTINUATION"
    EVENT_CONTINUATION = "EVENT_CONTINUATION"


class MissionTriggerRouter:
    """Canonical router from AgentTriggers to Missions."""

    def __init__(
        self,
        project_root: str | Path,
        store: AgentRuntimeStore | None = None,
        mission_store: MissionStore | None = None,
        channel_store: ChannelStore | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.store = store or AgentRuntimeStore(project_root)
        self.mission_store = mission_store or MissionStore(project_root)
        self.channel_store = channel_store or ChannelStore(project_root)

    def route_trigger(self, trigger: AgentTrigger) -> tuple[str, str]:
        """Route an AgentTrigger to a Mission.

        Returns (mission_id, routing_reason).
        """
        # 1. Check if trigger was already routed
        existing_records = self.store.get_routing_records(trigger_id=trigger.trigger_id)
        if existing_records:
            rec = existing_records[0]
            return str(rec["mission_id"]), str(rec["routing_reason"])

        # 2. Check if targeted at an existing mission
        target_mission_id = trigger.payload.get("mission_id")
        existing_mission = (
            self.mission_store.load(str(target_mission_id)) if target_mission_id else None
        )

        if existing_mission:
            mission_id = existing_mission.mission_id
            if trigger.payload.get("interrupt_reply") or trigger.trigger_type == TriggerType.SIGNAL:
                routing_reason = MissionRoutingReason.INTERRUPT_REPLY
            elif trigger.trigger_type == TriggerType.SCHEDULE:
                routing_reason = MissionRoutingReason.SCHEDULED_CONTINUATION
            elif trigger.trigger_type == TriggerType.EVENT:
                routing_reason = MissionRoutingReason.EVENT_CONTINUATION
            else:
                routing_reason = MissionRoutingReason.CONTINUE_MISSION
        else:
            # Create a new Mission
            mission_id = f"m_{uuid.uuid4().hex[:12]}"
            goal = (
                trigger.payload.get("goal")
                or trigger.payload.get("prompt")
                or f"Trigger {trigger.trigger_id} on channel {trigger.channel_id}"
            )
            workspace = str(trigger.payload.get("workspace", ""))
            constraints = [str(c) for c in trigger.payload.get("constraints") or []]
            acceptance_criteria = [str(a) for a in trigger.payload.get("acceptance_criteria") or []]

            mission = Mission(
                mission_id=mission_id,
                goal=str(goal),
                channel_id=trigger.channel_id,
                workspace=workspace,
                constraints=constraints,
                acceptance_criteria=acceptance_criteria,
                status=MissionStatus.created,
            )
            self.mission_store.save(mission)

            # Ensure channel link
            with contextlib.suppress(Exception):
                self.channel_store.link_mission(trigger.channel_id, mission_id)

            routing_reason = MissionRoutingReason.NEW_MISSION

        # 3. Persist routing record for audit & idempotency
        self.store.record_routing(
            {
                "trigger_id": trigger.trigger_id,
                "message_id": trigger.message_id,
                "channel_id": trigger.channel_id,
                "principal_id": trigger.principal_id,
                "mission_id": mission_id,
                "routing_reason": routing_reason,
                "dispatch_timestamp": time.time(),
            }
        )

        return mission_id, routing_reason

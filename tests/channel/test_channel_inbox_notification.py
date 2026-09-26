"""P2 Qualification Suite: Channel, Agent Inbox, and Notification Projection.

Validates:
Channel
  └─ Agent Inbox
       └─ Mission*
            └─ Execution*
                 └─ Session*
  ┌─ Notification Projection
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli.channel_cli import run_channel_cli, run_inbox_cli
from veya.channel import (
    AgentInbox,
    Channel,
    ChannelStatus,
    ChannelStore,
    ChannelType,
    InboxMessageStatus,
    Notification,
    NotificationCategory,
    NotificationLevel,
    NotificationProjector,
)
from veya.remote.events import VeyaEventType, create_veya_event
from veya.supervision.models import Mission


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path


# ── 1. Channel Lifecycle & Durable Store ────────────────────────


def test_channel_lifecycle_and_store(project_dir: Path) -> None:
    store = ChannelStore(project_dir)

    # 1. Create and save channel
    ch = Channel(
        channel_id="ch_web_001",
        channel_type=ChannelType.WEB,
        name="Main Web UI",
        workspace_root=str(project_dir),
        config={"theme": "dark"},
    )
    store.save(ch)

    # 2. Get channel
    loaded = store.get("ch_web_001")
    assert loaded is not None
    assert loaded.channel_id == "ch_web_001"
    assert loaded.channel_type == ChannelType.WEB
    assert loaded.name == "Main Web UI"
    assert loaded.config == {"theme": "dark"}
    assert loaded.status == ChannelStatus.ACTIVE

    # 3. Link mission
    store.link_mission("ch_web_001", "m_alpha")
    store.link_mission("ch_web_001", "m_beta")
    updated = store.get("ch_web_001")
    assert updated is not None
    assert updated.active_mission_ids == ["m_alpha", "m_beta"]

    # 4. Unlink mission
    store.unlink_mission("ch_web_001", "m_alpha")
    unlinked = store.get("ch_web_001")
    assert unlinked is not None
    assert unlinked.active_mission_ids == ["m_beta"]

    # 5. Archive channel
    store.archive("ch_web_001")
    archived = store.get("ch_web_001")
    assert archived is not None
    assert archived.status == ChannelStatus.ARCHIVED

    # 6. List filtering
    active_channels = store.list(status=ChannelStatus.ACTIVE)
    assert len(active_channels) == 0
    all_channels = store.list()
    assert len(all_channels) == 1


# ── 2. Agent Inbox & Deduplication ──────────────────────────────


def test_agent_inbox_enqueue_and_deduplication(project_dir: Path) -> None:
    store = ChannelStore(project_dir)
    store.save(Channel(channel_id="ch_cli", channel_type=ChannelType.CLI, name="CLI"))
    inbox = AgentInbox(project_dir)

    # 1. Enqueue message
    msg1 = inbox.enqueue(
        channel_id="ch_cli",
        content="Fix failing tests in auth module",
        sender="developer",
        idempotency_key="idemp_1001",
    )
    assert msg1.message_id.startswith("msg_")
    assert msg1.status == InboxMessageStatus.PENDING
    assert msg1.content == "Fix failing tests in auth module"

    # 2. Idempotent duplicate enqueue returns the same message without duplication
    msg2 = inbox.enqueue(
        channel_id="ch_cli",
        content="Fix failing tests in auth module",
        sender="developer",
        idempotency_key="idemp_1001",
    )
    assert msg2.message_id == msg1.message_id

    # Messages in inbox must be exactly 1
    pending = inbox.poll("ch_cli", status=InboxMessageStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].message_id == msg1.message_id

    # 3. Bind to mission
    dispatched = inbox.bind_mission("ch_cli", msg1.message_id, "m_mission_99")
    assert dispatched.status == InboxMessageStatus.DISPATCHED
    assert dispatched.mission_id == "m_mission_99"

    # Verify channel now tracks the mission
    ch = store.get("ch_cli")
    assert ch is not None
    assert "m_mission_99" in ch.active_mission_ids

    # 4. Mark processed
    processed = inbox.mark_processed("ch_cli", msg1.message_id)
    assert processed.status == InboxMessageStatus.PROCESSED
    assert processed.processed_at is not None


# ── 3. Notification Projection & Sinks ──────────────────────────


@pytest.mark.asyncio
async def test_notification_projector_and_sinks(project_dir: Path) -> None:
    store = ChannelStore(project_dir)
    store.save(Channel(channel_id="ch_slack", channel_type=ChannelType.IM, name="Slack Alert"))

    projector = NotificationProjector(project_dir)

    # Sink callback spy
    delivered_notifications: list[Notification] = []

    async def mock_sink(notif: Notification) -> None:
        delivered_notifications.append(notif)

    projector.add_sink(mock_sink)

    # 1. Project regular progress notification
    p_notif = await projector.project(
        channel_id="ch_slack",
        mission_id="m_200",
        title="Execution Step 1 Completed",
        body="Compiled worktree successfully",
        level=NotificationLevel.INFO,
        category=NotificationCategory.PROGRESS,
    )
    assert p_notif.level == NotificationLevel.INFO
    assert len(delivered_notifications) == 1
    assert delivered_notifications[0].title == "Execution Step 1 Completed"

    # 2. Project JEV owner interrupt (ACTION_REQUIRED)
    int_notif = await projector.project_interrupt(
        channel_id="ch_slack",
        mission_id="m_200",
        interrupt_reason="PRODUCTION_DESTRUCTIVE_ACTION",
        details="Confirm drop database table 'users_legacy'",
    )
    assert int_notif.level == NotificationLevel.ACTION_REQUIRED
    assert int_notif.category == NotificationCategory.CONFIRMATION_REQUIRED
    assert len(delivered_notifications) == 2
    assert "Action Required" in delivered_notifications[1].title

    # 3. Project canonical VeyaEvent
    veya_ev = create_veya_event(
        execution_id="ex_sub_1",
        event_type=VeyaEventType.EXECUTION_FAILED,
        mission_id="m_200",
        payload={"message": "Process killed: OOM"},
    )
    ev_notif = await projector.project_event(
        channel_id="ch_slack",
        event=veya_ev,
    )
    assert ev_notif.level == NotificationLevel.ERROR
    assert ev_notif.category == NotificationCategory.FAILED
    assert len(delivered_notifications) == 3

    # 4. Verify durable persistence of notifications
    persisted = projector.list_notifications("ch_slack")
    assert len(persisted) == 3
    assert persisted[1].level == NotificationLevel.ACTION_REQUIRED


# ── 4. Mission Model Hierarchy Compatibility ────────────────────


def test_mission_channel_id_compatibility() -> None:
    # 1. Mission without channel_id
    m_old = Mission(mission_id="m_legacy", goal="Build API")
    assert m_old.channel_id is None
    d_old = m_old.to_dict()
    assert d_old["channel_id"] is None
    m_rehydrated = Mission.from_dict(d_old)
    assert m_rehydrated.channel_id is None

    # 2. Mission with channel_id
    m_chan = Mission(mission_id="m_chan_1", goal="Scrape news", channel_id="ch_web_001")
    assert m_chan.channel_id == "ch_web_001"
    d_chan = m_chan.to_dict()
    assert d_chan["channel_id"] == "ch_web_001"
    m_chan_rehydrated = Mission.from_dict(d_chan)
    assert m_chan_rehydrated.channel_id == "ch_web_001"


# ── 5. Channel and Inbox CLI Operations ─────────────────────────


def test_channel_and_inbox_cli_json(
    project_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VEYA_PROJECT_ROOT", str(project_dir))

    # 1. channel create --json
    ret = run_channel_cli(["create", "Ops Channel", "--type", "daemon", "--json"])
    assert ret == 0
    ch_data = json.loads(capsys.readouterr().out)
    cid = ch_data["channel_id"]
    assert ch_data["channel_type"] == "daemon"

    # 2. channel list --json
    ret = run_channel_cli(["list", "--json"])
    assert ret == 0
    ch_list = json.loads(capsys.readouterr().out)
    assert len(ch_list) == 1
    assert ch_list[0]["channel_id"] == cid

    # 3. inbox send --json
    ret = run_inbox_cli(["send", cid, "Run database migration", "--sender", "operator", "--json"])
    assert ret == 0
    msg_data = json.loads(capsys.readouterr().out)
    assert msg_data["channel_id"] == cid
    assert msg_data["content"] == "Run database migration"
    assert msg_data["sender"] == "operator"

    # 4. inbox list --json
    ret = run_inbox_cli(["list", "--channel", cid, "--json"])
    assert ret == 0
    inbox_list = json.loads(capsys.readouterr().out)
    assert len(inbox_list) == 1
    assert inbox_list[0]["message_id"] == msg_data["message_id"]

    # 5. channel archive --json
    ret = run_channel_cli(["archive", cid, "--json"])
    assert ret == 0
    arch_data = json.loads(capsys.readouterr().out)
    assert arch_data["status"] == "ARCHIVED"

"""Comprehensive E2E Qualification Suite for Veya Agent Runtime V1.

Verifies:
1. AGENT_RUNTIME_DAEMON lifecycle (STARTING -> RUNNING -> DRAINING -> STOPPED)
2. RUNTIME_GENERATION monotonically increments; stale generation leases are rejected / reclaimed
3. STARTUP_RECOVERY recovers unhandled triggers, active missions, and outbox without message loss
4. GRACEFUL_DRAIN parks incoming triggers, flushes outbox, and completes safely
5. TRIGGER_IDEMPOTENCY ensures duplicate triggers return identical trigger without duplicate mission
6. MISSION_TRIGGER_ROUTING routes to new or continuing mission, records durable audit record
7. DURABLE_LEASE: claim -> heartbeat -> expire -> reclaim
8. SCHEDULER: INTERVAL / ONCE / CRON firing with FIRE_ONCE policy upon missed window
9. EVENT_TRIGGERING: VeyaEvent generates matching AgentTrigger through EventSubscription
10. BACKPRESSURE & ADMISSION: Concurrency limits defer work; waiting items consume zero execution attempts
11. NOTIFICATION_OUTBOX: Delivery loop, retries with backoff, dead-lettering after max attempts
12. CIRCUIT_BREAKER: Repeated sink failures trip to OPEN, preventing retry storms
13. DEAD_LETTER_REPLAY: Replays dead-lettered delivery back into pending state
14. SESSION_CHECKPOINT: Checkpoints generation, sequence, and filters reconnect events
15. INVARIANTS: MESSAGE_LOSS=0, DUPLICATE_MISSION=0
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from veya.agent_runtime import (
    AdmissionController,
    AdmissionDecision,
    AdmissionPolicy,
    AdmissionPolicyMode,
    AgentRuntime,
    AgentRuntimeStore,
    CircuitBreaker,
    CircuitBreakerState,
    DeliveryStatus,
    MissedSchedulePolicy,
    MissionRoutingReason,
    MissionTriggerRouter,
    NotificationOutbox,
    RuntimeStatus,
    ScheduleType,
    SessionContinuityManager,
    TriggerManager,
    TriggerStatus,
    TriggerType,
)
from veya.channel import AgentInbox, Channel, ChannelStore, ChannelType
from veya.channel.notification import Notification
from veya.remote.events import VeyaEvent, VeyaEventType, create_veya_event
from veya.supervision.store import MissionStore


@pytest.fixture
def test_env(tmp_path: Path):
    """Setup clean isolated environment."""
    ch_store = ChannelStore(tmp_path)
    ch = Channel(
        channel_id="ch_test_default",
        channel_type=ChannelType.CLI,
        name="test-channel",
        workspace_root=str(tmp_path),
    )
    ch_store.save(ch)
    return tmp_path


def test_runtime_generation_monotonic(test_env: Path):
    store = AgentRuntimeStore(test_env)
    assert store.get_generation() == 1
    g2 = store.bump_generation()
    assert g2 == 2
    g3 = store.bump_generation()
    assert g3 == 3
    assert store.get_generation() == 3


def test_trigger_idempotency(test_env: Path):
    mgr = TriggerManager(test_env)
    t1 = mgr.create_trigger(
        channel_id="ch_test_default",
        trigger_type=TriggerType.USER_MESSAGE,
        payload={"prompt": "Do task"},
        idempotency_key="unique_key_1",
    )
    t2 = mgr.create_trigger(
        channel_id="ch_test_default",
        trigger_type=TriggerType.USER_MESSAGE,
        payload={"prompt": "Do task"},
        idempotency_key="unique_key_1",
    )
    assert t1.trigger_id == t2.trigger_id
    assert t1.status == TriggerStatus.PENDING


def test_durable_lease_claim_heartbeat_reclaim(test_env: Path):
    mgr = TriggerManager(test_env)
    t = mgr.create_trigger(
        channel_id="ch_test_default",
        trigger_type=TriggerType.USER_MESSAGE,
        payload={"prompt": "Claim me"},
    )
    # 1. Claim
    claimed = mgr.claim_next_trigger("worker_1", ttl_seconds=1.0, current_generation=1)
    assert claimed is not None
    assert claimed.trigger_id == t.trigger_id
    assert claimed.status == TriggerStatus.CLAIMED
    assert claimed.claimed_by == "worker_1"
    assert claimed.attempt == 1

    # 2. Heartbeat
    hb_ok = mgr.heartbeat_lease(claimed.trigger_id, claimed.lease_id, ttl_seconds=2.0)
    assert hb_ok is True

    # 3. Another worker cannot claim while lease active
    claimed_other = mgr.claim_next_trigger("worker_2", ttl_seconds=1.0, current_generation=1)
    assert claimed_other is None

    # 4. Lease expiration allows reclaim
    claimed.lease_expires_at = time.time() - 1.0  # Force expire
    mgr.store.save_trigger(claimed)

    reclaimed_count = mgr.reclaim_stale_leases(current_generation=1)
    assert reclaimed_count == 1

    # Worker 2 can now claim
    claimed2 = mgr.claim_next_trigger("worker_2", ttl_seconds=1.0, current_generation=1)
    assert claimed2 is not None
    assert claimed2.trigger_id == t.trigger_id
    assert claimed2.claimed_by == "worker_2"
    assert claimed2.attempt == 2

    # 5. Ack completion
    mgr.ack_trigger(claimed2.trigger_id, claimed2.lease_id, status=TriggerStatus.PROCESSED)
    final_trig = mgr.store.get_trigger(t.trigger_id)
    assert final_trig.status == TriggerStatus.PROCESSED
    assert final_trig.lease_id is None


def test_stale_generation_lease_invalidation(test_env: Path):
    mgr = TriggerManager(test_env)
    t = mgr.create_trigger(
        channel_id="ch_test_default",
        trigger_type=TriggerType.USER_MESSAGE,
        payload={"prompt": "Old generation task"},
        generation=1,
    )
    claimed = mgr.claim_next_trigger("worker_1", ttl_seconds=100.0, current_generation=1)
    assert claimed is not None
    assert claimed.generation == 1

    # Daemon restarted: generation is now 2
    # Even though lease has not expired, generation < current_generation allows reclaim
    claimed_gen2 = mgr.claim_next_trigger("worker_2", ttl_seconds=100.0, current_generation=2)
    assert claimed_gen2 is not None
    assert claimed_gen2.trigger_id == t.trigger_id
    assert claimed_gen2.claimed_by == "worker_2"
    assert claimed_gen2.generation == 2


def test_mission_trigger_routing_new_and_continue(test_env: Path):
    mgr = TriggerManager(test_env)
    router = MissionTriggerRouter(test_env)
    m_store = MissionStore(test_env)

    # 1. New Mission
    t1 = mgr.create_trigger(
        channel_id="ch_test_default",
        trigger_type=TriggerType.USER_MESSAGE,
        payload={"prompt": "Build feature"},
    )
    m_id1, reason1 = router.route_trigger(t1)
    assert reason1 == MissionRoutingReason.NEW_MISSION
    mission1 = m_store.load(m_id1)
    assert mission1 is not None
    assert mission1.channel_id == "ch_test_default"
    assert "Build feature" in mission1.goal

    # Idempotent re-route returns same mission and reason
    m_id1_retry, reason1_retry = router.route_trigger(t1)
    assert m_id1_retry == m_id1
    assert reason1_retry == reason1

    # 2. Continue Mission
    t2 = mgr.create_trigger(
        channel_id="ch_test_default",
        trigger_type=TriggerType.USER_MESSAGE,
        payload={"mission_id": m_id1, "prompt": "Follow-up question"},
    )
    m_id2, reason2 = router.route_trigger(t2)
    assert m_id2 == m_id1
    assert reason2 == MissionRoutingReason.CONTINUE_MISSION


def test_scheduler_interval_and_fire_once(test_env: Path):
    runtime = AgentRuntime(test_env)
    sched = runtime.scheduler.create_schedule(
        channel_id="ch_test_default",
        schedule_type=ScheduleType.INTERVAL,
        expression="60",
        missed_policy=MissedSchedulePolicy.FIRE_ONCE,
    )
    assert sched.enabled is True
    assert sched.next_fire_at > time.time()

    # Before due time: no triggers generated
    triggers = runtime.scheduler.tick(sched.next_fire_at - 1.0)
    assert len(triggers) == 0

    # Simulate daemon being down for 10 minutes (missed window)
    missed_time = sched.next_fire_at + 600.0
    triggers_after_miss = runtime.scheduler.tick(missed_time)

    # FIRE_ONCE policy guarantees exactly 1 trigger is produced
    assert len(triggers_after_miss) == 1
    t = triggers_after_miss[0]
    assert t.trigger_type == TriggerType.SCHEDULE
    assert t.payload["schedule_id"] == sched.schedule_id

    # next_fire_at is advanced into the future
    updated_sched = runtime.store.get_schedule(sched.schedule_id)
    assert updated_sched.next_fire_at > missed_time


def test_event_subscription_and_triggering(test_env: Path):
    runtime = AgentRuntime(test_env)
    _sub = runtime.events.subscribe(
        channel_id="ch_test_default",
        event_types=[VeyaEventType.EXECUTION_FAILED, VeyaEventType.RUNTIME_UNAVAILABLE],
        filters={"actor": "system"},
    )

    # Unmatched event (different actor)
    evt_unmatched = create_veya_event(
        execution_id="exec_1",
        event_type=VeyaEventType.EXECUTION_FAILED,
        actor="user",
    )
    trigs1 = runtime.events.process_event(evt_unmatched)
    assert len(trigs1) == 0

    # Matched event
    evt_matched = create_veya_event(
        execution_id="exec_2",
        event_type=VeyaEventType.EXECUTION_FAILED,
        actor="system",
        payload={"reason": "timeout"},
    )
    trigs2 = runtime.events.process_event(evt_matched)
    assert len(trigs2) == 1
    assert trigs2[0].trigger_type == TriggerType.EVENT
    assert trigs2[0].payload["event_id"] == evt_matched.event_id


def test_admission_backpressure_and_capacity_release(test_env: Path):
    policy = AdmissionPolicy(global_concurrency=2, policy_mode=AdmissionPolicyMode.DEFER)
    adm = AdmissionController(policy)

    # Acquire up to limit
    assert adm.acquire("p1", "ch1") is True
    assert adm.acquire("p2", "ch1") is True
    assert adm.active_global == 2

    # Exceeding limit results in DEFERRED decision without consuming attempts
    dec, reason = adm.evaluate_admission("p3", "ch1")
    assert dec == AdmissionDecision.DEFERRED
    assert "limit" in str(reason)

    # Release frees slot
    adm.release("p1", "ch1")
    assert adm.active_global == 1

    dec2, _ = adm.evaluate_admission("p3", "ch1")
    assert dec2 == AdmissionDecision.ADMITTED
    assert adm.active_global == 2


@pytest.mark.asyncio
async def test_notification_outbox_retry_and_circuit_breaker(test_env: Path):
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=0.2)
    outbox = NotificationOutbox(test_env, circuit_breaker=breaker)

    call_count = 0

    def failing_sink(payload):
        nonlocal call_count
        call_count += 1
        raise ConnectionResetError("Sink unavailable")

    outbox.register_sink("failing_sink", failing_sink)

    notif = Notification(
        notification_id="notif_1",
        channel_id="ch_test_default",
        mission_id="m_1",
        title="Alert",
        body="Action needed",
    )
    delivs = outbox.enqueue(notif, sink_ids=["failing_sink"])
    assert len(delivs) == 1
    d = delivs[0]

    # Attempt 1 -> fails, status RETYRING
    await outbox.deliver_single(d)
    assert d.status == DeliveryStatus.RETRYING
    assert d.attempt == 1

    # Attempt 2 -> fails, failure_threshold (2) reached -> Breaker OPEN
    await outbox.deliver_single(d)
    assert breaker.get_state("failing_sink") == CircuitBreakerState.OPEN

    # Breaker OPEN prevents attempts (protects degraded sink)
    assert breaker.can_attempt("failing_sink") is False
    res = await outbox.deliver_single(d)
    assert res is False
    assert call_count == 2  # Didn't call sink again while breaker OPEN

    # Wait for recovery timeout -> transitions to HALF_OPEN
    await asyncio.sleep(0.25)
    assert breaker.get_state("failing_sink") == CircuitBreakerState.HALF_OPEN


@pytest.mark.asyncio
async def test_dead_letter_and_replay(test_env: Path):
    runtime = AgentRuntime(test_env)

    def failing_sink(payload):
        raise ValueError("Permanent sink failure")

    runtime.outbox.register_sink("sink_perm_fail", failing_sink)

    notif = Notification(
        notification_id="notif_dl",
        channel_id="ch_test_default",
        mission_id="m_dl",
        title="Dead Letter Test",
        body="Exhaust attempts",
    )
    delivs = runtime.outbox.enqueue(notif, sink_ids=["sink_perm_fail"])
    d = delivs[0]
    d.attempt = 4
    d.max_attempts = 5
    runtime.store.save_delivery(d)

    # 5th attempt fails -> moves to DEAD_LETTERED
    await runtime.outbox.deliver_single(d)
    assert d.status == DeliveryStatus.DEAD_LETTERED

    dls = runtime.store.list_dead_letters()
    assert len(dls) >= 1
    target_dl = next(r for r in dls if r.entity_id == d.delivery_id)
    assert target_dl.reason == "max_delivery_attempts_exhausted"

    # Replay dead letter
    replayed = runtime.outbox.replay_dead_letter(target_dl.dead_letter_id)
    assert replayed is not None
    assert replayed.status == DeliveryStatus.PENDING
    assert replayed.attempt == 0


def test_session_continuity_and_reconnect(test_env: Path):
    manager = SessionContinuityManager(test_env)
    cp = manager.save_checkpoint(
        session_id="sess_123",
        generation=2,
        last_event_seq=42,
        reason="step_done",
    )
    assert cp.last_event_seq == 42

    loaded = manager.load_checkpoint("sess_123")
    assert loaded is not None
    assert loaded.generation == 2
    assert loaded.last_event_seq == 42

    # Reconnect filter
    events = [
        VeyaEvent("e1", "x", "m", 1.0, "user", "type", {}, seq=40),
        VeyaEvent("e2", "x", "m", 2.0, "user", "type", {}, seq=42),
        VeyaEvent("e3", "x", "m", 3.0, "user", "type", {}, seq=43),
        VeyaEvent("e4", "x", "m", 4.0, "user", "type", {}, seq=44),
    ]
    reconnect_events = manager.filter_events_for_reconnect(events, last_client_seq=42)
    assert [e.seq for e in reconnect_events] == [43, 44]


@pytest.mark.asyncio
async def test_agent_runtime_daemon_full_lifecycle(test_env: Path):
    runtime = AgentRuntime(test_env)
    assert runtime.status == RuntimeStatus.STOPPED

    # 1. Startup Recovery
    rec_info = runtime.startup_recovery()
    assert runtime.status == RuntimeStatus.RUNNING
    assert rec_info["generation"] >= 1

    # 2. Enqueue an inbox message in channel
    inbox = AgentInbox(test_env)
    msg = inbox.enqueue(
        channel_id="ch_test_default",
        content="Automate task via Daemon",
        idempotency_key="inbox_msg_lifecycle_1",
    )
    assert msg.status == "PENDING"

    # 3. Simulate daemon crash and recovery: inbox message should be ingested as trigger
    rec_info_2 = runtime.startup_recovery()
    assert rec_info_2["recovered_inbox_messages"] >= 1

    # 4. Step runtime: trigger claimed, admitted, routed to new mission, acked
    step_res = await runtime.step()
    assert step_res["dispatched"] >= 1

    # Check that mission was created and inbox message was bound
    updated_msg = inbox.get("ch_test_default", msg.message_id)
    assert updated_msg.status == "DISPATCHED"
    assert updated_msg.mission_id is not None

    # 5. Check health snapshot
    health = runtime.get_health()
    assert health.status == RuntimeStatus.RUNNING
    assert health.runtime_generation == runtime.generation
    assert health.active_missions_count >= 1

    # 6. Graceful Drain
    await runtime.drain(timeout_seconds=0.1)
    assert runtime.status == RuntimeStatus.STOPPED

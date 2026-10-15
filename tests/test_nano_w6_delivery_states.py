"""NANO-W6 addendum K: dispatch delivery states (delivered/started/unconfirmed).

Covers the pure derivation, the extended agent_mailbox records (no new
store), and the rule that delivery is never assumed: no start signal stays
``unconfirmed``.
"""

from __future__ import annotations

import pytest

# ── pure derivation ───────────────────────────────────────────────────


def test_derive_delivery_state():
    from veya.oskill.pure.delivery_state import derive_delivery_state

    assert derive_delivery_state(delivered=True, has_start_evidence=True) == "delivered"
    assert derive_delivery_state(delivered=True, has_start_evidence=False) == "delivered"
    assert derive_delivery_state(delivered=False, has_start_evidence=True) == "started"
    assert derive_delivery_state(delivered=False, has_start_evidence=False) == "unconfirmed"


# ── mailbox records ────────────────────────────────────────────────────


@pytest.fixture()
def mailbox(tmp_path):
    from server.goal_run.agent_mailbox import AgentMailbox

    return AgentMailbox(tmp_path / "mailbox.json")


def test_message_starts_unconfirmed(mailbox):
    msg = mailbox.send(
        sender="coordinator", recipient="worker-1", delivery_mode="STEER", payload={"text": "go"}
    )
    assert msg.delivery_state == "unconfirmed"
    assert msg.delivered_at is None


def test_poll_marks_delivered(mailbox):
    mailbox.send(
        sender="coordinator", recipient="worker-1", delivery_mode="STEER", payload={"text": "go"}
    )
    pending = mailbox.poll("worker-1")
    assert len(pending) == 1
    # nothing pending anymore: the message reached the inbox
    assert mailbox.poll("worker-1") == []
    rows = mailbox.list_for_recipient("worker-1")
    assert rows[0]["delivery_state"] == "delivered"
    assert rows[0]["delivered_at"] is not None


def test_derivation_with_start_evidence(mailbox):

    msg = mailbox.send(
        sender="coordinator", recipient="worker-2", delivery_mode="NOTIFY", payload={"text": "hi"}
    )
    # no delivery yet, but the target run shows positive start evidence
    state = mailbox.derive_delivery_state(msg, [{"type": "text_delta", "run": "worker-2"}])
    assert state == "started"
    # no events → never assumed delivered
    assert mailbox.derive_delivery_state(msg, None) == "unconfirmed"
    assert mailbox.derive_delivery_state(msg, []) == "unconfirmed"


def test_list_for_recipient_carries_state(mailbox):
    mailbox.send(
        sender="a", recipient="worker-3", delivery_mode="FOLLOW_UP", payload={"text": "one"}
    )
    mailbox.send(
        sender="a", recipient="worker-3", delivery_mode="FOLLOW_UP", payload={"text": "two"}
    )
    mailbox.poll("worker-3", mark_delivered=False)
    rows = mailbox.list_for_recipient("worker-3")
    assert len(rows) == 2
    # undelivered without start evidence stays unconfirmed
    assert all(row["delivery_state"] == "unconfirmed" for row in rows)
    mailbox.poll("worker-3")
    rows = mailbox.list_for_recipient("worker-3")
    assert all(row["delivery_state"] == "delivered" for row in rows)

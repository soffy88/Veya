from veya.remote.approval import ApprovalStore
from veya.remote.models import RiskClass


def test_approval_request_decide_and_one_shot_consume() -> None:
    store = ApprovalStore()
    record = store.create_approval(
        principal="p",
        capability_id="privileged.sudo",
        normalized_operation="sudo true",
        cwd="/workspace",
        workspace="/workspace",
        risk_class=RiskClass.P2_ROOT_MUTATION,
        decision="pending",
    )
    store.decide(record.approval_id, principal="p", decision="approved")
    ok, code, message = store.verify_and_consume(
        record.approval_id,
        principal="p",
        capability_id="privileged.sudo",
        normalized_operation="sudo true",
        cwd="/workspace",
        workspace="/workspace",
    )
    assert (ok, code, message) == (True, None, None)
    ok, code, _ = store.verify_and_consume(
        record.approval_id,
        principal="p",
        capability_id="privileged.sudo",
        normalized_operation="sudo true",
        cwd="/workspace",
        workspace="/workspace",
    )
    assert ok is False
    assert str(code) == "INVALID_APPROVAL"

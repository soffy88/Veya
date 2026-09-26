from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from veya.providers.cfkanban.client import CfKanbanClient, validate_openapi_document
from veya.providers.cfkanban.errors import CfKanbanErrorClass, CfKanbanProviderError
from veya.providers.cfkanban.ledger import CfKanbanOperationLedger, SideEffectState
from veya.providers.cfkanban.mapping import (
    CFKANBAN_REOPEN_OPERATION,
    derive_idempotency_key,
    from_provider_state,
    reopen,
)
from veya.providers.cfkanban.models import (
    CFKANBAN_CONTRACT_PIN,
    CFKANBAN_CONTRACT_VERSION,
    CfKanbanProviderConfig,
    CfKanbanTaskRef,
)
from veya.supervision.store import MissionStore

FIXTURE = Path(__file__).parent / "fixtures" / "openapi_pinned.json"


def config() -> CfKanbanProviderConfig:
    return CfKanbanProviderConfig("cfkanban", "https://kanban.example", "secret-ref")


def task(identifier: str = "CFK-123") -> dict[str, object]:
    return {
        "id": "task-id",
        "identifier": identifier,
        "number": 123,
        "version": 3,
        "status_key": "todo",
        "project_id": "project-id",
    }


def test_contract_fixture_and_pin_are_fixed() -> None:
    document = json.loads(FIXTURE.read_text())
    validate_openapi_document(document)
    assert CFKANBAN_CONTRACT_VERSION == "0.1.0"
    assert CFKANBAN_CONTRACT_PIN == "aff6b5ac21ccd3b5ce580795743157922518567b"


def test_contract_drift_blocks_mutation_shape() -> None:
    document = json.loads(FIXTURE.read_text())
    document["paths"]["/api/v1/issues/{identifier}"]["patch"]["operationId"] = "guessedUpdate"
    with pytest.raises(CfKanbanProviderError) as error:
        validate_openapi_document(document)
    assert error.value.provider_code == "CONTRACT_DRIFT_DETECTED"


def test_auth_is_constructed_ephemerally_and_not_configured() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": []})

    client = CfKanbanClient(
        config(), lambda ref: "opaque-secret", transport=httpx.MockTransport(handler)
    )
    client.list_issues()
    assert seen[0].headers["Authorization"] == "Bearer opaque-secret"
    assert "opaque-secret" not in repr(client.config.public_dict())
    assert client.config.public_dict()["credential_ref"] == "secret-ref"
    client.close()


def test_write_uses_discovery_contract_gate_cas_and_idempotency() -> None:
    requests: list[httpx.Request] = []
    openapi = json.loads(FIXTURE.read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/.well-known/cfkanban-instance.json":
            return httpx.Response(200, json={"discovery_version": 1, "instance_id": "instance"})
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=openapi)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"d1": "reachable"})
        if request.url.path == "/api/v1/meta":
            return httpx.Response(200, json={})
        if request.url.path == "/api/v1/me":
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            headers={"X-Request-ID": "req-1"},
            json={"resource": task(), "event_cursor": "e1", "idempotent_replay": False},
        )

    client = CfKanbanClient(config(), lambda _ref: "secret", transport=httpx.MockTransport(handler))
    result = client.update_issue("CFK-123", 3, "stable-key", title="new")
    assert result.task is not None and result.task.version == 3
    mutation = requests[-1]
    assert mutation.headers["Idempotency-Key"] == "stable-key"
    assert json.loads(mutation.content)["expected_version"] == 3
    client.close()


def test_error_normalization_preserves_provider_receipt() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            headers={"X-Request-ID": "req-9"},
            json={
                "code": "VERSION_CONFLICT",
                "category": "conflict",
                "request_id": "req-9",
                "retryable": False,
                "recovery": "refresh_resource",
                "details": {"current_version": 4},
            },
        )

    client = CfKanbanClient(config(), lambda _ref: "secret", transport=httpx.MockTransport(handler))
    with pytest.raises(CfKanbanProviderError) as error:
        client.get_issue("CFK-123")
    assert error.value.canonical_class == CfKanbanErrorClass.CONFLICT
    assert error.value.provider_code == "VERSION_CONFLICT"
    assert error.value.provider_request_id == "req-9"
    assert error.value.recovery_hint == "refresh_resource"
    client.close()


def test_credential_revocation_fails_closed_without_fallback() -> None:
    credential = ["valid-secret"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer valid-secret"
        return httpx.Response(200, json=task())

    client = CfKanbanClient(
        config(), lambda _ref: credential[0], transport=httpx.MockTransport(handler)
    )
    assert client.get_issue("CFK-123").version == 3
    credential[0] = ""
    with pytest.raises(CfKanbanProviderError) as error:
        client.get_issue("CFK-123")
    assert error.value.canonical_class == CfKanbanErrorClass.AUTH_REQUIRED
    client.close()


def test_unknown_state_fails_closed_and_done_is_not_accept() -> None:
    assert from_provider_state("done") == "done"
    with pytest.raises(CfKanbanProviderError):
        from_provider_state("made_up")


def test_reopen_is_explicitly_unsupported() -> None:
    assert CFKANBAN_REOPEN_OPERATION == "NOT_SUPPORTED"
    with pytest.raises(CfKanbanProviderError) as error:
        reopen()
    assert error.value.provider_code == "UNSUPPORTED_OPERATION"


def test_identity_mapping_does_not_alias_mission_and_provider_task() -> None:
    ref = CfKanbanTaskRef("mission-1", "project-1", "CFK-123", 3)
    assert ref.mission_id_is_provider_task_id is False
    assert ref.to_dict()["provider_task_id"] == "CFK-123"
    assert derive_idempotency_key(
        "mission-1", "exec-1", "comment", "CFK-123"
    ) == derive_idempotency_key("mission-1", "exec-1", "comment", "CFK-123")


def test_ledger_persists_cursor_and_quarantines_ambiguous_side_effect(tmp_path: Path) -> None:
    ledger = CfKanbanOperationLedger(MissionStore(tmp_path), "mission-1")
    ledger.begin("op-1", "exec-1", "complete", "stable")
    with pytest.raises(CfKanbanProviderError):
        ledger.execute(
            "op-1",
            "exec-1",
            "complete",
            "stable",
            lambda: (_ for _ in ()).throw(
                CfKanbanProviderError(CfKanbanErrorClass.AMBIGUOUS, "timeout")
            ),
        )
    assert ledger.find("op-1").state == SideEffectState.AMBIGUOUS
    ledger.save_cursor("scope-1", "opaque-cursor")
    assert ledger.cursor("scope-1") == "opaque-cursor"


def test_ledger_confirms_json_receipt_and_deduplicates_event_replay(tmp_path: Path) -> None:
    ledger = CfKanbanOperationLedger(MissionStore(tmp_path), "mission-1")
    result = ledger.execute("op-1", "exec-1", "comment", "stable", lambda: {"event_cursor": "e1"})
    assert result == {"event_cursor": "e1"}
    assert ledger.find("op-1").state == SideEffectState.CONFIRMED
    events = [{"event_id": "event-1", "type": "issue.updated"}]
    ledger.persist_events_then_advance("scope-1", events, "cursor-1")
    restarted = CfKanbanOperationLedger(MissionStore(tmp_path), "mission-1")
    restarted.persist_events_then_advance("scope-1", events, "cursor-2")
    records = restarted.store.executions("mission-1")
    assert sum(record.get("event_id") == "event-1" for record in records) == 1
    assert restarted.cursor("scope-1") == "cursor-2"


def test_http_timeout_after_possible_provider_commit_is_ambiguous_and_not_replayed(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("provider may have committed before the ACK")

    client = CfKanbanClient(config(), lambda _ref: "secret", transport=httpx.MockTransport(handler))
    client._write_ready = True
    ledger = CfKanbanOperationLedger(MissionStore(tmp_path), "mission-1")
    with pytest.raises(CfKanbanProviderError) as error:
        ledger.execute(
            "op-timeout",
            "exec-1",
            "update",
            "stable-key",
            lambda: client.update_issue("CFK-123", 3, "stable-key", title="new"),
        )
    assert error.value.canonical_class == CfKanbanErrorClass.AMBIGUOUS
    assert ledger.find("op-timeout").state == SideEffectState.AMBIGUOUS
    assert calls == 1
    restarted = CfKanbanOperationLedger(MissionStore(tmp_path), "mission-1")
    assert (
        restarted.execute("op-timeout", "exec-1", "update", "stable-key", lambda: calls + 1).state
        == SideEffectState.AMBIGUOUS
    )
    assert calls == 1
    client.close()


def test_task_ref_serializes_required_provider_identity() -> None:
    ref = CfKanbanTaskRef("m", "p", "CFK-1", 1, provider_board_id="board")
    assert set(ref.to_dict()) == {
        "mission_id",
        "provider_id",
        "provider_project_id",
        "provider_board_id",
        "provider_task_id",
        "provider_revision",
    }

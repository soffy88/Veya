"""Contract-pinned cfKanban HTTP client.

The client is intentionally boring: paths and payload names are copied from
the Wave 8 matrix, while policy, Mission transitions, and routing stay above it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast
from urllib.parse import quote, urljoin

import httpx

from .errors import CfKanbanErrorClass, CfKanbanProviderError, normalize_error
from .models import (
    CFKANBAN_CONTRACT_VERSION,
    CFKANBAN_OPENAPI_BLOB,
    CfKanbanCursor,
    CfKanbanEvent,
    CfKanbanMutationResult,
    CfKanbanProviderConfig,
    CfKanbanTask,
)

CredentialResolver = Callable[[str], str]

_OPERATIONS: dict[str, tuple[str, str]] = {
    "discoverInstance": ("GET", "/.well-known/cfkanban-instance.json"),
    "getHealth": ("GET", "/healthz"),
    "getMeta": ("GET", "/api/v1/meta"),
    "getMe": ("GET", "/api/v1/me"),
    "listProjects": ("GET", "/api/v1/workspaces/{workspace_id}/projects"),
    "getProject": ("GET", "/api/v1/workspaces/{workspace_id}/projects/{project_id}"),
    "listIssues": ("GET", "/api/v1/issues"),
    "listProjectIssues": ("GET", "/api/v1/workspaces/{workspace_id}/projects/{project_id}/issues"),
    "getIssue": ("GET", "/api/v1/issues/{identifier}"),
    "listComments": ("GET", "/api/v1/issues/{identifier}/comments"),
    "listEvents": ("GET", "/api/v1/events"),
    "assignIssueToMe": ("POST", "/api/v1/issues/{identifier}/commands/assign-to-me"),
    "updateIssue": ("PATCH", "/api/v1/issues/{identifier}"),
    "createComment": ("POST", "/api/v1/issues/{identifier}/comments"),
    "reportIssueBlocked": ("POST", "/api/v1/issues/{identifier}/commands/report-blocked"),
    "clearIssueBlocked": ("POST", "/api/v1/issues/{identifier}/commands/clear-blocked"),
    "completeIssue": ("POST", "/api/v1/issues/{identifier}/commands/complete"),
}


def _path(template: str, **values: str) -> str:
    return template.format(**{key: quote(value, safe="") for key, value in values.items()})


def _pairs(name: str, values: Iterable[str] | None) -> list[tuple[str, str]]:
    return [(name, value) for value in values or ()]


def _task_list(payload: Mapping[str, Any]) -> tuple[list[CfKanbanTask], CfKanbanCursor | None]:
    raw_items = payload.get("items")
    items = [
        CfKanbanTask.from_payload(item) for item in raw_items or [] if isinstance(item, Mapping)
    ]
    next_cursor = payload.get("next_cursor")
    cursor = CfKanbanCursor(str(next_cursor), "page") if next_cursor else None
    return items, cursor


def validate_openapi_document(document: object) -> None:
    """Fail closed if the runtime document is not the pinned contract shape."""

    if not isinstance(document, Mapping):
        raise CfKanbanProviderError(
            CfKanbanErrorClass.UPSTREAM_UNAVAILABLE,
            "cfKanban OpenAPI document is not an object",
            provider_code="CONTRACT_DRIFT_DETECTED",
        )
    info = document.get("info")
    if (
        document.get("openapi") != "3.1.0"
        or not isinstance(info, Mapping)
        or info.get("version") != CFKANBAN_CONTRACT_VERSION
    ):
        raise CfKanbanProviderError(
            CfKanbanErrorClass.UPSTREAM_UNAVAILABLE,
            "cfKanban OpenAPI version is incompatible with the pinned contract",
            provider_code="CONTRACT_DRIFT_DETECTED",
        )
    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        raise CfKanbanProviderError(
            CfKanbanErrorClass.UPSTREAM_UNAVAILABLE,
            "cfKanban OpenAPI document lacks paths",
            provider_code="CONTRACT_DRIFT_DETECTED",
        )
    for operation_id, (method, path) in _OPERATIONS.items():
        operation = paths.get(path)
        if not isinstance(operation, Mapping) or not isinstance(
            operation.get(method.lower()), Mapping
        ):
            raise CfKanbanProviderError(
                CfKanbanErrorClass.UPSTREAM_UNAVAILABLE,
                f"Pinned cfKanban operation is missing: {method} {path}",
                provider_code="CONTRACT_DRIFT_DETECTED",
                details={"operation_id": operation_id, "contract_pin": CFKANBAN_OPENAPI_BLOB},
            )
        if operation[method.lower()].get("operationId") != operation_id:
            raise CfKanbanProviderError(
                CfKanbanErrorClass.UPSTREAM_UNAVAILABLE,
                f"cfKanban operationId drifted: {operation_id}",
                provider_code="CONTRACT_DRIFT_DETECTED",
                details={"contract_pin": CFKANBAN_OPENAPI_BLOB},
            )


class CfKanbanClient:
    def __init__(
        self,
        config: CfKanbanProviderConfig,
        credential_resolver: CredentialResolver,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self._credential_resolver = credential_resolver
        self._http = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(
                timeout=config.request_timeout,
                connect=config.connect_timeout,
                read=config.read_timeout,
            ),
        )
        self._write_ready = False

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CfKanbanClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/"))

    def _request(
        self,
        operation_id: str,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json: Mapping[str, Any] | None = None,
        auth: bool = True,
        idempotency_key: str | None = None,
        write: bool = False,
    ) -> tuple[dict[str, Any], str | None]:
        method = _OPERATIONS[operation_id][0]
        headers = {"Accept": "application/json"}
        if json is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            credential = self._credential_resolver(self.config.credential_ref)
            if not credential:
                raise CfKanbanProviderError(
                    CfKanbanErrorClass.AUTH_REQUIRED,
                    "cfKanban credential resolver returned no credential",
                    provider_code="AUTH_REQUIRED",
                )
            headers["Authorization"] = f"Bearer {credential}"
        if idempotency_key is not None:
            if (
                not 1 <= len(idempotency_key) <= 128
                or not idempotency_key.isascii()
                or any(ord(char) < 0x20 or ord(char) == 0x7F for char in idempotency_key)
            ):
                raise ValueError("Idempotency-Key must be 1..128 printable ASCII characters")
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._http.request(
                method,
                self._url(path),
                params=httpx.QueryParams(cast(Any, params or [])),
                json=json,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise CfKanbanProviderError(
                CfKanbanErrorClass.AMBIGUOUS if write else CfKanbanErrorClass.UPSTREAM_UNAVAILABLE,
                "cfKanban request did not produce a definitive response",
                provider_code="TRANSPORT_FAILURE",
                retryable=False,
            ) from exc
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}
        if response.status_code < 200 or response.status_code >= 300:
            raise normalize_error(response.status_code, response.headers, payload)
        if not isinstance(payload, dict):
            raise CfKanbanProviderError(
                CfKanbanErrorClass.UPSTREAM_UNAVAILABLE,
                f"cfKanban {operation_id} returned a non-object response",
                provider_code="INVALID_RESPONSE",
                provider_http_status=response.status_code,
            )
        return payload, response.headers.get("X-Request-ID")

    def discover_instance(self) -> dict[str, Any]:
        payload, _ = self._request(
            "discoverInstance", "/.well-known/cfkanban-instance.json", auth=False
        )
        if payload.get("discovery_version") != 1 or not payload.get("instance_id"):
            raise CfKanbanProviderError(
                CfKanbanErrorClass.UPSTREAM_UNAVAILABLE,
                "cfKanban instance discovery is incompatible",
                provider_code="CONTRACT_DRIFT_DETECTED",
            )
        return payload

    def get_openapi(self) -> dict[str, Any]:
        try:
            response = self._http.get(
                self._url("/openapi.json"), headers={"Accept": "application/json"}
            )
            payload = response.json() if response.content else {}
        except (httpx.HTTPError, ValueError) as exc:
            raise CfKanbanProviderError(
                CfKanbanErrorClass.UPSTREAM_UNAVAILABLE,
                "Unable to read cfKanban OpenAPI contract",
                provider_code="CONTRACT_DRIFT_DETECTED",
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise normalize_error(response.status_code, response.headers, payload)
        validate_openapi_document(payload)
        return payload

    def health(self) -> dict[str, Any]:
        payload, _ = self._request("getHealth", "/healthz", auth=False)
        if payload.get("d1") not in {"reachable", "unavailable"}:
            raise CfKanbanProviderError(
                CfKanbanErrorClass.UPSTREAM_UNAVAILABLE,
                "cfKanban health response is incompatible",
                provider_code="CONTRACT_DRIFT_DETECTED",
            )
        return payload

    def meta(self) -> dict[str, Any]:
        return self._request("getMeta", "/api/v1/meta")[0]

    def me(self) -> dict[str, Any]:
        return self._request("getMe", "/api/v1/me")[0]

    def ensure_write_ready(self) -> None:
        self.discover_instance()
        self.get_openapi()
        health = self.health()
        if health.get("d1") != "reachable":
            raise CfKanbanProviderError(
                CfKanbanErrorClass.UPSTREAM_UNAVAILABLE,
                "cfKanban health check is unavailable",
                provider_code="PLATFORM_UNAVAILABLE",
                retryable=True,
            )
        self.meta()
        self.me()
        self._write_ready = True

    def _write(
        self,
        operation_id: str,
        path: str,
        *,
        body: Mapping[str, Any],
        idempotency_key: str,
    ) -> CfKanbanMutationResult:
        if not self._write_ready:
            self.ensure_write_ready()
        payload, request_id = self._request(
            operation_id,
            path,
            json=body,
            idempotency_key=idempotency_key,
            write=True,
        )
        return CfKanbanMutationResult.from_payload(payload, request_id=request_id)

    def list_projects(
        self,
        workspace_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        deleted: str | None = None,
    ) -> tuple[list[dict[str, Any]], CfKanbanCursor | None]:
        params = (
            ([] if cursor is None else [("cursor", cursor)])
            + ([] if limit is None else [("limit", str(limit))])
            + ([] if deleted is None else [("deleted", deleted)])
        )
        payload, _ = self._request(
            "listProjects",
            _path(_OPERATIONS["listProjects"][1], workspace_id=workspace_id),
            params=params,
        )
        return list(payload.get("items") or []), CfKanbanCursor(
            str(payload["next_cursor"]), "page"
        ) if payload.get("next_cursor") else None

    def get_project(
        self, workspace_id: str, project_id: str, *, deleted: str | None = None
    ) -> dict[str, Any]:
        params = [("deleted", deleted)] if deleted else None
        return self._request(
            "getProject",
            _path(_OPERATIONS["getProject"][1], workspace_id=workspace_id, project_id=project_id),
            params=params,
        )[0]

    def list_issues(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        blocked: str | None = None,
        deleted: str | None = None,
        project_ids: Iterable[str] | None = None,
        workspace_ids: Iterable[str] | None = None,
        status: Iterable[str] | None = None,
        assignees: Iterable[str] | None = None,
        query: str | None = None,
    ) -> tuple[list[CfKanbanTask], CfKanbanCursor | None]:
        params = (
            _pairs("project", project_ids)
            + _pairs("workspace", workspace_ids)
            + _pairs("status", status)
            + _pairs("assignee", assignees)
        )
        params += ([] if cursor is None else [("cursor", cursor)]) + (
            [] if limit is None else [("limit", str(limit))]
        )
        params += (
            ([] if blocked is None else [("blocked", blocked)])
            + ([] if deleted is None else [("deleted", deleted)])
            + ([] if query is None else [("q", query)])
        )
        payload, _ = self._request("listIssues", "/api/v1/issues", params=params)
        return _task_list(payload)

    def list_project_issues(
        self,
        workspace_id: str,
        project_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        blocked: str | None = None,
        deleted: str | None = None,
        status: Iterable[str] | None = None,
        assignees: Iterable[str] | None = None,
    ) -> tuple[list[CfKanbanTask], CfKanbanCursor | None]:
        params = ([] if cursor is None else [("cursor", cursor)]) + (
            [] if limit is None else [("limit", str(limit))]
        )
        params += _pairs("status", status) + _pairs("assignee", assignees)
        params += ([] if blocked is None else [("blocked", blocked)]) + (
            [] if deleted is None else [("deleted", deleted)]
        )
        payload, _ = self._request(
            "listProjectIssues",
            _path(
                _OPERATIONS["listProjectIssues"][1],
                workspace_id=workspace_id,
                project_id=project_id,
            ),
            params=params,
        )
        return _task_list(payload)

    def get_issue(self, identifier: str, *, deleted: str | None = None) -> CfKanbanTask:
        params = [("deleted", deleted)] if deleted else None
        payload, _ = self._request(
            "getIssue", _path(_OPERATIONS["getIssue"][1], identifier=identifier), params=params
        )
        return CfKanbanTask.from_payload(payload)

    def list_comments(
        self,
        identifier: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        deleted: str | None = None,
    ) -> tuple[list[dict[str, Any]], CfKanbanCursor | None]:
        params = (
            ([] if cursor is None else [("cursor", cursor)])
            + ([] if limit is None else [("limit", str(limit))])
            + ([] if deleted is None else [("deleted", deleted)])
        )
        payload, _ = self._request(
            "listComments",
            _path(_OPERATIONS["listComments"][1], identifier=identifier),
            params=params,
        )
        return list(payload.get("items") or []), CfKanbanCursor(
            str(payload["next_cursor"]), "page"
        ) if payload.get("next_cursor") else None

    def list_events(
        self,
        *,
        after: str | None = None,
        limit: int | None = None,
        project_ids: Iterable[str] | None = None,
        workspace_ids: Iterable[str] | None = None,
    ) -> tuple[list[CfKanbanEvent], CfKanbanCursor | None]:
        params = _pairs("project", project_ids) + _pairs("workspace", workspace_ids)
        params += ([] if after is None else [("after", after)]) + (
            [] if limit is None else [("limit", str(limit))]
        )
        payload, _ = self._request("listEvents", "/api/v1/events", params=params)
        events = [
            CfKanbanEvent.from_payload(item)
            for item in payload.get("items") or []
            if isinstance(item, Mapping)
        ]
        return events, CfKanbanCursor(str(payload["next_cursor"]), "event") if payload.get(
            "next_cursor"
        ) else None

    def assign_issue_to_me(
        self, identifier: str, expected_version: int, idempotency_key: str
    ) -> CfKanbanMutationResult:
        return self._write(
            "assignIssueToMe",
            _path(_OPERATIONS["assignIssueToMe"][1], identifier=identifier),
            body={"expected_version": expected_version},
            idempotency_key=idempotency_key,
        )

    def update_issue(
        self, identifier: str, expected_version: int, idempotency_key: str, **fields: Any
    ) -> CfKanbanMutationResult:
        if not fields:
            raise ValueError("update_issue requires at least one update field")
        if fields.get("status_key") == "done":
            raise ValueError("done is only entered by completeIssue")
        return self._write(
            "updateIssue",
            _path(_OPERATIONS["updateIssue"][1], identifier=identifier),
            body={"expected_version": expected_version, **fields},
            idempotency_key=idempotency_key,
        )

    def create_comment(
        self,
        identifier: str,
        body: str,
        idempotency_key: str,
        *,
        reply_to_comment_id: str | None = None,
    ) -> CfKanbanMutationResult:
        payload: dict[str, Any] = {"body": body}
        if reply_to_comment_id is not None:
            payload["reply_to_comment_id"] = reply_to_comment_id
        return self._write(
            "createComment",
            _path(_OPERATIONS["createComment"][1], identifier=identifier),
            body=payload,
            idempotency_key=idempotency_key,
        )

    def report_blocked(
        self, identifier: str, expected_version: int, reason: str, idempotency_key: str
    ) -> CfKanbanMutationResult:
        return self._write(
            "reportIssueBlocked",
            _path(_OPERATIONS["reportIssueBlocked"][1], identifier=identifier),
            body={"expected_version": expected_version, "reason": reason},
            idempotency_key=idempotency_key,
        )

    def clear_blocked(
        self, identifier: str, expected_version: int, idempotency_key: str
    ) -> CfKanbanMutationResult:
        return self._write(
            "clearIssueBlocked",
            _path(_OPERATIONS["clearIssueBlocked"][1], identifier=identifier),
            body={"expected_version": expected_version},
            idempotency_key=idempotency_key,
        )

    def complete_issue(
        self,
        identifier: str,
        expected_version: int,
        idempotency_key: str,
        *,
        summary: str | None = None,
        verification: list[str] | None = None,
        artifacts: list[dict[str, str]] | None = None,
        follow_ups: list[str] | None = None,
    ) -> CfKanbanMutationResult:
        payload: dict[str, Any] = {"expected_version": expected_version}
        for key, value in (
            ("summary", summary),
            ("verification", verification),
            ("artifacts", artifacts),
            ("follow_ups", follow_ups),
        ):
            if value is not None:
                payload[key] = value
        return self._write(
            "completeIssue",
            _path(_OPERATIONS["completeIssue"][1], identifier=identifier),
            body=payload,
            idempotency_key=idempotency_key,
        )

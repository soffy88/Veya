"""server.wayfinding_github_tools tests.

Thin-wrapper formatting/error-path tests against a minimal FakeGithub
(reuses the same seam as platform/3O/omodul's own tests: monkeypatch
omodul.wayfinding_github._gh_run). Correctness of the underlying GraphQL
operations themselves is covered there and by a live smoke run against
soffy88/Veya — this file only checks that the server-level wrappers format
output sensibly and don't leak exceptions.
"""

from __future__ import annotations

import json

import pytest
from omodul import wayfinding_github as wg

from server.wayfinding_github_tools import (
    wayfind_gh_add_fog,
    wayfind_gh_add_ticket,
    wayfind_gh_chart,
    wayfind_gh_claim,
    wayfind_gh_complete,
    wayfind_gh_decisions,
    wayfind_gh_frontier,
    wayfind_gh_resolve,
    wayfind_gh_wire_blocking,
)

REPO = "fake/repo"


class FakeGithub:
    """Minimal stand-in — just enough for the happy-path chain these tests drive."""

    def __init__(self) -> None:
        self.next_number = 1
        self.issues: dict[int, dict] = {}
        self.labels: set[str] = set()
        self.viewer_login = "test-user"

    def gh_run(self, args: list[str], *, input_text: str | None = None) -> str:
        if args[:2] == ["api", "graphql"]:
            payload = json.loads(input_text)
            return json.dumps({"data": self._graphql(payload["query"], payload["variables"])})
        if args[:2] == ["label", "create"]:
            self.labels.add(args[2])
            return ""
        if args[:2] == ["issue", "edit"]:
            number = int(args[2])
            issue = self.issues[number]
            if "--add-assignee" in args:
                who = args[args.index("--add-assignee") + 1]
                issue["assignees"].append(self.viewer_login if who == "@me" else who)
            if "--body-file" in args:
                issue["body"] = input_text
            return ""
        if args[:2] == ["issue", "comment"]:
            self.issues[int(args[2])].setdefault("comments", []).append(args[args.index("-b") + 1])
            return ""
        if args[:2] == ["issue", "close"]:
            self.issues[int(args[2])]["closed"] = True
            return ""
        raise AssertionError(f"FakeGithub: unhandled gh args: {args}")

    def _number_from_id(self, node_id: str) -> int:
        return int(node_id.split("_", 1)[1])

    def _graphql(self, query: str, variables: dict) -> dict:
        if "createIssue(input:" in query:
            number = self.next_number
            self.next_number += 1
            parent = variables.get("parentIssueId")
            labels = [lid.split("_", 1)[1] for lid in variables["labelIds"]]
            issue = {
                "number": number,
                "id": f"I_{number}",
                "title": variables["title"],
                "body": variables["body"],
                "closed": False,
                "assignees": [],
                "labels": labels,
                "blocked_by": [],
                "sub_issues": [],
                "url": f"https://github.com/fake/repo/issues/{number}",
            }
            self.issues[number] = issue
            if parent:
                self.issues[self._number_from_id(parent)]["sub_issues"].append(number)
            return {
                "createIssue": {
                    "issue": {
                        "id": issue["id"],
                        "number": number,
                        "url": issue["url"],
                        "title": issue["title"],
                    }
                }
            }
        if "addBlockedBy(input:" in query:
            to_n = self._number_from_id(variables["issueId"])
            from_n = self._number_from_id(variables["blockingIssueId"])
            self.issues[to_n]["blocked_by"].append(from_n)
            return {"addBlockedBy": {"clientMutationId": None}}
        if "repository(owner:$owner,name:$name){ id }" in query:
            return {"repository": {"id": "R_fake"}}
        if "label(name:$label)" in query:
            name = variables["label"]
            if name not in self.labels:
                return {"repository": {"label": None}}
            return {"repository": {"label": {"id": f"L_{name}"}}}
        if "subIssues(first:100)" in query:
            number = variables["number"]
            if number not in self.issues:
                return {"repository": {"issue": None}}
            nodes = []
            for n in self.issues[number]["sub_issues"]:
                iss = self.issues[n]
                nodes.append(
                    {
                        "number": iss["number"],
                        "title": iss["title"],
                        "state": "CLOSED" if iss["closed"] else "OPEN",
                        "url": iss["url"],
                        "assignees": {"nodes": [{"login": a} for a in iss["assignees"]]},
                        "labels": {"nodes": [{"name": lbl} for lbl in iss["labels"]]},
                        "blockedBy": {
                            "nodes": [
                                {
                                    "number": b,
                                    "state": "CLOSED" if self.issues[b]["closed"] else "OPEN",
                                }
                                for b in iss["blocked_by"]
                            ]
                        },
                    }
                )
            return {"repository": {"issue": {"subIssues": {"nodes": nodes}}}}
        if "{ body } } }" in query:
            return {"repository": {"issue": {"body": self.issues[variables["number"]]["body"]}}}
        if "closed assignees" in query:
            iss = self.issues[variables["number"]]
            return {
                "repository": {
                    "issue": {
                        "id": iss["id"],
                        "title": iss["title"],
                        "closed": iss["closed"],
                        "assignees": {"nodes": [{"login": a} for a in iss["assignees"]]},
                    }
                }
            }
        if "{ id } } }" in query:
            return {"repository": {"issue": {"id": self.issues[variables["number"]]["id"]}}}
        if "viewer{ login }" in query:
            return {"viewer": {"login": self.viewer_login}}
        raise AssertionError(f"FakeGithub: unhandled query: {query}")


@pytest.fixture
def fake(monkeypatch):
    gh = FakeGithub()
    monkeypatch.setattr(wg, "_gh_run", gh.gh_run)
    return gh


class TestHappyPathChain:
    def test_chart_add_ticket_frontier(self, fake):
        chart_out = wayfind_gh_chart(REPO, "pick a queue")
        assert "✅" in chart_out
        map_number = int(chart_out.split("#")[1].split()[0])

        add_out = wayfind_gh_add_ticket(REPO, map_number, "pick db", "which db?")
        assert "✅" in add_out
        ticket_number = int(add_out.split("#")[1].split()[0])

        frontier_out = wayfind_gh_frontier(REPO, map_number)
        assert "pick db" in frontier_out

        claim_out = wayfind_gh_claim(REPO, ticket_number)
        assert "✅" in claim_out

        resolve_out = wayfind_gh_resolve(
            REPO, map_number, ticket_number, "chose postgres", "use postgres"
        )
        assert "✅" in resolve_out

        decisions_out = wayfind_gh_decisions(REPO, map_number)
        assert "use postgres" in decisions_out

        complete_out = wayfind_gh_complete(REPO, map_number)
        assert "✅" in complete_out

    def test_wire_blocking_hides_from_frontier(self, fake):
        chart_out = wayfind_gh_chart(REPO, "d")
        map_number = int(chart_out.split("#")[1].split()[0])
        a_out = wayfind_gh_add_ticket(REPO, map_number, "A", "qa")
        a = int(a_out.split("#")[1].split()[0])
        b_out = wayfind_gh_add_ticket(REPO, map_number, "B", "qb")
        b = int(b_out.split("#")[1].split()[0])

        wire_out = wayfind_gh_wire_blocking(REPO, a, b)
        assert "✅" in wire_out

        frontier_out = wayfind_gh_frontier(REPO, map_number)
        assert "A" in frontier_out and "B" not in frontier_out


class TestErrorPathsAreReadableNotExceptions:
    def test_claim_conflict(self, fake):
        chart_out = wayfind_gh_chart(REPO, "d")
        map_number = int(chart_out.split("#")[1].split()[0])
        add_out = wayfind_gh_add_ticket(REPO, map_number, "A", "qa")
        ticket_number = int(add_out.split("#")[1].split()[0])

        wayfind_gh_claim(REPO, ticket_number, login="alice")
        r2 = wayfind_gh_claim(REPO, ticket_number, login="bob")
        assert "✅" not in r2
        assert "alice" in r2

    def test_resolve_without_claim(self, fake):
        chart_out = wayfind_gh_chart(REPO, "d")
        map_number = int(chart_out.split("#")[1].split()[0])
        add_out = wayfind_gh_add_ticket(REPO, map_number, "A", "qa")
        ticket_number = int(add_out.split("#")[1].split()[0])

        out = wayfind_gh_resolve(REPO, map_number, ticket_number, "x", "y")
        assert "✅" not in out
        assert "not claimed" in out

    def test_add_fog_and_no_crash_on_unknown_map(self, fake):
        out = wayfind_gh_frontier(REPO, 9999)
        assert "✅" not in out
        assert "not found" in out

        chart_out = wayfind_gh_chart(REPO, "d")
        map_number = int(chart_out.split("#")[1].split()[0])
        fog_out = wayfind_gh_add_fog(REPO, map_number, "auth strategy unclear")
        assert "✅" in fog_out

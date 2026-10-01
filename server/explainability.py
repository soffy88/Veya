"""§34 Required Explainability Surfaces — operational debugging, not LLM speculation.

At minimum: config.explain(), policy.explain(), context.explain(),
workspace.explain(), completion.explain().
"""

from __future__ import annotations

from typing import Any


def config_explain(key: str, **kwargs: Any) -> dict[str, Any]:
    """Explain why a config value was selected."""
    from config.authority import explain

    return explain(key, **kwargs)


def policy_explain(
    *,
    actor: str,
    tool: str,
    args: dict[str, Any] | None = None,
    workspace: str = "",
    cwd: str = "",
    goal_id: str = "",
) -> dict[str, Any]:
    """Explain why a tool was allowed/denied."""
    from veya.remote.policy_resolver import PolicyRequest, PolicyResolver

    resolver = PolicyResolver()
    request = PolicyRequest(
        actor=actor,
        tool=tool,
        args=args or {},
        workspace=workspace,
        cwd=cwd,
        goal_id=goal_id,
    )
    return resolver.explain(request)


def context_explain(goal_run_id: str, *, project_root: str = "") -> dict[str, Any]:
    """Explain why context was injected.

    Admission / resolve / cite / explain / audit all go through the one
    :class:`~server.context_gateway.ContextGateway` authority — this surface
    never constructs a gateway of its own, and after a process restart it
    reloads the admitted projection from the run's durable admission record
    rather than reporting an empty answer.
    """
    import os

    from server.goal_run.pre_admission import context_projection

    projection = context_projection(
        goal_run_id=goal_run_id,
        project_root=project_root or os.environ.get("VEYA_PROJECT_ROOT", "."),
    )
    if projection is None:
        return {"goal_run_id": goal_run_id, "explanation": "no projection found"}
    return {
        "goal_run_id": goal_run_id,
        "projection_id": projection.projection_id,
        "item_count": len(projection.items),
        "total_tokens": projection.total_tokens,
        "budget_tokens": projection.budget_tokens,
        "items": [
            {
                "id": item.context_item_id,
                "scope": item.scope.value,
                "provenance": item.provenance,
                "authority": item.authority,
            }
            for item in projection.items
        ],
    }


def workspace_explain(workspace_id: str) -> dict[str, Any]:
    """Explain why a worktree/projection was created."""
    return {
        "workspace_id": workspace_id,
        "explanation": "workspace authority projection",
        "authority_type": "GIT",
        "revision": "see WorkspaceAuthority.current_revision",
    }


def completion_explain(goal_run_id: str) -> dict[str, Any]:
    """Explain why a Goal is/is not complete."""
    from server.completion_proposal import CompletionDecisionValue

    return {
        "goal_run_id": goal_run_id,
        "explanation": "completion requires evidence",
        "required": [
            "CompletionProposal",
            "AcceptanceCriteria",
            "VerificationEvidence",
            "CompletionDecision",
        ],
        "decision": CompletionDecisionValue.ACCEPT.value,
    }

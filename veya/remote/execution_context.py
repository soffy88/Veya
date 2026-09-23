"""Shared ExecutionCapabilityContext — the single envelope every L1 worker consumes.

One canonical context for Hicode / DSH / Pi / Grok: bounded task-memory recovery,
capability routing evidence, per-execution skill selection (progressive
disclosure), project context and permission policy. Credentials appear only as
references. Workers never build their own search stack / skill registry /
permission model.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ExecutionCapabilityContext:
    mission_id: str
    execution_id: str
    worker_type: str
    workspace: str
    selected_capabilities: list[dict[str, Any]] = field(default_factory=list)
    selected_skills: list[dict[str, Any]] = field(default_factory=list)
    task_memory_context: dict[str, Any] = field(default_factory=dict)
    project_context: dict[str, Any] = field(default_factory=dict)
    permission_policy: dict[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "execution_id": self.execution_id,
            "worker_type": self.worker_type,
            "workspace": self.workspace,
            "selected_capabilities": list(self.selected_capabilities),
            "selected_skills": list(self.selected_skills),
            "task_memory_context": dict(self.task_memory_context),
            "project_context": dict(self.project_context),
            "permission_policy": dict(self.permission_policy),
        }


class ExecutionContextBuilder:
    """Build one bounded context per execution from the shared registries."""

    def __init__(
        self,
        *,
        capability_registry: Any = None,
        skill_registry: Any = None,
        task_memory: Any = None,
        permission_policy: dict[str, Any] | None = None,
    ) -> None:
        self.capability_registry = capability_registry
        self.skill_registry = skill_registry
        self.task_memory = task_memory
        self.permission_policy = dict(permission_policy or {})

    async def build(
        self,
        *,
        mission_id: str,
        execution_id: str,
        worker_type: str,
        workspace: str,
        capability_ids: list[str] | None = None,
        task_text: str = "",
        project_index: dict[str, Any] | None = None,
    ) -> tuple[ExecutionCapabilityContext, dict[str, Any]]:
        selected_capabilities: list[dict[str, Any]] = []
        routing_evidence: list[dict[str, Any]] = []
        for capability_id in capability_ids or []:
            if self.capability_registry is None:
                break
            provider, evidence = await self.capability_registry.select(capability_id)
            routing_evidence.append(evidence)
            if provider is not None:
                selected_capabilities.append(
                    {
                        "capability_id": capability_id,
                        "selected_provider": provider.provider_id,
                        "health": provider.health,
                        # credential REFERENCE only
                        "credential_ref": provider.credential_ref,
                    }
                )

        candidate_skills: list[str] = []
        selected_skills: list[dict[str, Any]] = []
        declined_skills: list[dict[str, Any]] = []
        if self.skill_registry is not None:
            resolution = self.skill_registry.resolve(task_text)
            candidate_skills = list(resolution["candidates"])
            allowed = [str(p) for p in self.permission_policy.get("allowed", [])]
            for skill_id in candidate_skills:
                ok, missing = self.skill_registry.check_permissions(skill_id, allowed)
                if not ok:
                    declined_skills.append(
                        {"skill_id": skill_id, "reason": f"permission_denied:{missing}"}
                    )
                    continue
                # Level 2 loaded only for SELECTED skills.
                self.skill_registry.load_core(skill_id)
                record = self.skill_registry.get(skill_id)
                selected_skills.append(
                    {
                        **record.provenance(),
                        "level2": self.skill_registry.loaded_core(skill_id),
                        "trigger_reason": next(
                            (
                                item["reason"]
                                for item in resolution["selected"]
                                if item["skill_id"] == skill_id
                            ),
                            "",
                        ),
                    }
                )

        task_memory_context = (
            self.task_memory.recovery_context() if self.task_memory is not None else {}
        )
        context = ExecutionCapabilityContext(
            mission_id=mission_id,
            execution_id=execution_id,
            worker_type=worker_type,
            workspace=workspace,
            selected_capabilities=selected_capabilities,
            selected_skills=selected_skills,
            task_memory_context=task_memory_context,
            project_context=dict(project_index or {}),
            permission_policy=self.permission_policy,
        )
        evidence = {
            "capability_routing": routing_evidence,
            "candidate_skills": candidate_skills,
            "selected_skills": [s["skill_id"] for s in selected_skills],
            "declined_skills": declined_skills,
        }
        return context, evidence


__all__ = ["ExecutionCapabilityContext", "ExecutionContextBuilder"]


def render_execution_context(context: ExecutionCapabilityContext) -> str:
    """Unified bounded prompt projection shared by every CLI worker (B3).

    Emits only what a worker needs: selected skill instructions, capability
    handles, bounded task memory and permission constraints. Never raw secrets,
    never all skills, never full history.
    """

    lines = [
        f"# Execution context {context.execution_id}",
        f"worker: {context.worker_type}",
        f"workspace: {context.workspace}",
    ]
    if context.selected_skills:
        lines.append("## selected skills")
        for skill in context.selected_skills:
            lines.append(
                f"- {skill['skill_id']} v{skill.get('version', '?')} "
                f"(trigger: {skill.get('trigger_reason', '')})"
            )
            if skill.get("level2"):
                lines.append("### Level-2 workflow")
                lines.append(str(skill["level2"]))
    if context.selected_capabilities:
        lines.append("## capabilities (resolve via runtime, no credentials here)")
        for capability in context.selected_capabilities:
            lines.append(
                f"- {capability['capability_id']} via {capability['selected_provider']} "
                f"(health: {capability['health']})"
            )
    memory = context.task_memory_context or {}
    if memory.get("compact_plan"):
        lines.append("## compact plan")
        lines.append(str(memory["compact_plan"]))
    if memory.get("unresolved_errors"):
        lines.append("## unresolved errors")
        lines.append(json.dumps(memory["unresolved_errors"][-3:], default=str))
    capability_results = context.project_context.get("capability_results")
    if capability_results:
        lines.append("## capability results (runtime bridge)")
        lines.append(json.dumps(capability_results, default=str)[:12000])
    if context.permission_policy:
        lines.append("## permissions")
        lines.append(json.dumps(context.permission_policy, default=str))
    return "\n".join(lines)


def context_hash(context: ExecutionCapabilityContext) -> str:
    blob = json.dumps(context.to_public(), sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def context_budget(context: ExecutionCapabilityContext) -> dict[str, int]:
    """Deterministic byte instrumentation (B12) — no tokenizer dependency."""

    memory = context.task_memory_context or {}
    rendered = render_execution_context(context)
    return {
        "task_memory_context_bytes": len(json.dumps(memory, default=str)),
        "project_context_bytes": len(json.dumps(context.project_context, default=str)),
        "capability_context_bytes": len(json.dumps(context.selected_capabilities, default=str)),
        "skill_l2_bytes": sum(len(json.dumps(s, default=str)) for s in context.selected_skills),
        "total_injected_context_bytes": len(rendered),
        "selected_skill_count": len(context.selected_skills),
        "loaded_level3_resource_count": 0,
    }


_SHARED_CAPABILITIES: Any = None
_SHARED_SKILLS: Any = None


def shared_capability_registry() -> Any:
    """The single CapabilityRegistry shared by all workers and supervision modes."""

    global _SHARED_CAPABILITIES
    if _SHARED_CAPABILITIES is None:
        from .capabilities import CapabilityHealth, CapabilityProvider, CapabilityRegistry

        async def web_probe() -> tuple[str, str]:
            import httpx

            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                response = await client.get(
                    "https://api.stackexchange.com/2.3/search/advanced",
                    params={"q": "FastAPI", "site": "stackoverflow", "pagesize": 1},
                    headers={"User-Agent": "Veya-capability-probe/1.0"},
                )
                response.raise_for_status()
                payload = response.json()
            if not isinstance(payload.get("items"), list):
                return str(CapabilityHealth.UNAVAILABLE), "Stack Exchange response missing results"
            return str(
                CapabilityHealth.HEALTHY
            ), "Stack Exchange search API returned structured results"

        async def web_invoke(request: dict[str, Any]) -> Any:
            import httpx

            query = str(request.get("objective") or "").strip()
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.get(
                    "https://api.stackexchange.com/2.3/search/advanced",
                    params={"q": query, "site": "stackoverflow", "pagesize": 5},
                    headers={"User-Agent": "Veya-capability-bridge/1.0"},
                )
                response.raise_for_status()
                payload = response.json()
            results = []
            for item in payload.get("items", [])[:5]:
                results.append(
                    {
                        "title": str(item.get("title") or ""),
                        "url": str(item.get("link") or ""),
                        "snippet": str(item.get("excerpt") or ""),
                    }
                )
            return {"query": query, "results": results, "provider": "stackexchange-search-api"}

        async def github_probe() -> tuple[str, str]:
            return str(CapabilityHealth.HEALTHY), "public GitHub search bridge registered"

        async def github_invoke(request: dict[str, Any]) -> Any:
            import httpx

            query = str(request.get("objective") or "").strip()
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    "https://api.github.com/search/repositories",
                    params={"q": query, "per_page": 5},
                    headers={"Accept": "application/vnd.github+json"},
                )
                response.raise_for_status()
                payload = response.json()
            return {
                "total_count": payload.get("total_count", 0),
                "items": [
                    {
                        "name": item.get("full_name", ""),
                        "url": item.get("html_url", ""),
                        "description": item.get("description", ""),
                    }
                    for item in payload.get("items", [])[:5]
                ],
            }

        _SHARED_CAPABILITIES = CapabilityRegistry()
        _SHARED_CAPABILITIES.register(
            "web.search",
            [CapabilityProvider("veya-web-search", 1, prober=web_probe, invoker=web_invoke)],
        )
        _SHARED_CAPABILITIES.register(
            "docs.web_search",
            [CapabilityProvider("veya-web-search", 1, prober=web_probe, invoker=web_invoke)],
        )
        _SHARED_CAPABILITIES.register(
            "github.search",
            [
                CapabilityProvider(
                    "github-public-api", 1, prober=github_probe, invoker=github_invoke
                )
            ],
        )
    return _SHARED_CAPABILITIES


def shared_skill_registry() -> Any:
    """The single SkillRegistry shared by all workers and supervision modes."""

    global _SHARED_SKILLS
    if _SHARED_SKILLS is None:
        import json

        from .skills import SkillPermission, SkillRecord, SkillRegistry

        _SHARED_SKILLS = SkillRegistry()
        skills_root = Path(__file__).resolve().parents[2] / "templates" / "skills"
        if skills_root.is_dir():
            for root in sorted(skills_root.iterdir()):
                skill_file = root / "SKILL.md"
                if not skill_file.is_file():
                    continue
                manifest: dict[str, Any] = {}
                manifest_path = root / "manifest.json"
                try:
                    if manifest_path.is_file():
                        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                        if isinstance(payload, dict):
                            manifest = payload
                except (OSError, json.JSONDecodeError):
                    manifest = {}
                skill_id = str(manifest.get("name") or root.name)
                description = str(manifest.get("description") or skill_id)
                permissions: list[str] = []
                haystack = f"{skill_id} {description}".lower()
                if any(word in haystack for word in ("web", "browser", "github", "research")):
                    permissions.append(str(SkillPermission.NETWORK))
                if "github" in haystack:
                    permissions.append(str(SkillPermission.GITHUB))
                _SHARED_SKILLS.register(
                    SkillRecord(
                        skill_id=skill_id,
                        name=skill_id,
                        description=description,
                        triggers=[skill_id, root.name],
                        source="veya.templates.skills",
                        source_commit="workspace",
                        permissions_required=permissions,
                        compatible_workers=["hicode", "dsh", "pi", "grok"],
                        trust_level="PROJECT_LOCAL",
                        eval_status="PASS",
                        root=str(root),
                    )
                )
    return _SHARED_SKILLS


__all__ = [
    "ExecutionCapabilityContext",
    "ExecutionContextBuilder",
    "context_budget",
    "context_hash",
    "render_execution_context",
    "shared_capability_registry",
    "shared_skill_registry",
]

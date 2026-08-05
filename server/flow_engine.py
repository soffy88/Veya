"""server/flow_engine.py — Phase 2 (manifest mapping) + Phase 3 (Genesis execution + assembly).

Phase 1 (requirement research/proposal) lives in server.coordinator.RequirementCoordinator,
which reuses the full ReAct/reflection loop. Phase 2 is a single tool-forced LLM call (no
loop needed — one structured translation), and Phase 3 drives server.agents.genesis_agent.GenesisAgent
per manifest element, then does the final assembly call with the caller's own key/config —
the "cognitive decoupling" point: GENESIS_API_KEY forges the elements, the user's own key
glues them together.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from pydantic import ValidationError

from server.notification_center import global_notifier
from server.schemas import GenesisManifest, RequirementDoc
from server.sse import emit
from veya.llm import llm_call

logger = logging.getLogger("flow_engine")

_MANIFEST_SYSTEM_PROMPT = (
    "You translate an approved product requirement document into a strict 3O Engine "
    "construction manifest. Identify exactly which oprim / oskill / omodul / obase / oservi "
    "elements are required to implement the core_features. Call 'propose_manifest' exactly once."
)

_MANIFEST_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "propose_manifest",
        "description": "Submit the 3O construction manifest mapped from the requirement doc.",
        "parameters": {
            "type": "object",
            "properties": {
                "mission_id": {"type": "string"},
                "elements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "layer": {
                                "type": "string",
                                "description": "one of obase, oprim, oskill, omodul, oservi",
                            },
                            "name": {
                                "type": "string",
                                "description": "element path relative to the layer root, e.g. 'factor/dual_ma.py'",
                            },
                            "specs": {
                                "type": "string",
                                "description": "hard math formula / business logic specification",
                            },
                        },
                        "required": ["layer", "name", "specs"],
                    },
                },
            },
            "required": ["mission_id", "elements"],
        },
    },
}


async def propose_manifest(
    doc: RequirementDoc,
    *,
    session_id: str,
    model: str | None = None,
    provider: str | None = None,
    config: dict[str, Any] | None = None,
) -> GenesisManifest:
    """Phase 2: translate an approved RequirementDoc into a GenesisManifest (single tool-forced call)."""
    messages = [
        {"role": "system", "content": _MANIFEST_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(doc.model_dump(), ensure_ascii=False)},
    ]
    response = await llm_call(
        messages,
        tools=[_MANIFEST_TOOL_SCHEMA],
        model=model,
        provider=provider,
        config=config,
        max_tokens=2048,
    )
    choice = (response.get("choices") or [{}])[0]
    tool_calls = (choice.get("message") or {}).get("tool_calls") or []
    if not tool_calls:
        raise ValueError("propose_manifest: model returned no tool call (no API key configured?)")

    raw_args = (tool_calls[0].get("function") or {}).get("arguments") or "{}"
    try:
        parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        manifest = GenesisManifest.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"propose_manifest: invalid manifest returned by model: {exc}") from exc

    emit(session_id, "manifest", {"manifest": manifest.model_dump()})
    return manifest


async def run_phase3(
    manifest: GenesisManifest,
    *,
    session_id: str,
    config: dict[str, Any] | None = None,
) -> None:
    """Phase 3 (background task): forge each manifest element via GenesisAgent, then assemble
    the glue code using the caller's own config. Progress streams over the same session's
    SSE queue that Phase 1's cognitive_round/requirement_doc events already use.
    """
    from server.agents.genesis_agent import GenesisAgent

    genesis_results: list[dict[str, Any]] = []
    for element in manifest.elements:
        emit(session_id, "genesis_element_start", {"layer": element.layer, "name": element.name})
        try:
            agent = GenesisAgent(dedicated_api_key=os.environ.get("GENESIS_API_KEY"))
        except ValueError as exc:
            entry = {
                "layer": element.layer,
                "name": element.name,
                "status": "failed",
                "error": str(exc),
            }
            emit(session_id, "genesis_element_done", entry)
            genesis_results.append(entry)
            continue

        agent.wake_up()
        mission = (
            f"Check if {element.name} exists in {element.layer}. If not, implement it "
            f"matching these specs: {element.specs}"
        )
        result = await agent.handle_mission(mission)
        agent.sleep()

        entry = {"layer": element.layer, "name": element.name, **result}
        emit(session_id, "genesis_element_done", entry)
        genesis_results.append(entry)

    assembly_prompt = (
        "Genesis has completed the following 3O elements:\n"
        f"{json.dumps(genesis_results, ensure_ascii=False, indent=2)}\n\n"
        "Write the final integration script (Python) that glues these elements together into "
        "a single working entry point. Return only the code, no commentary."
    )
    try:
        response = await llm_call(
            [{"role": "user", "content": assembly_prompt}],
            config=config,
            max_tokens=4096,
        )
        code = ((response.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Exception as exc:
        logger.error("[flow_engine] assembly LLM call failed: %s", exc)
        emit(session_id, "flow_error", {"stage": "assembly", "error": str(exc)})
        global_notifier.push(
            "ERROR",
            "Genesis 施工失败",
            f"{manifest.mission_id}: {exc}",
            {"session_id": session_id, "mission_id": manifest.mission_id},
        )
        return

    emit(session_id, "assembly_done", {"code": code, "mission_id": manifest.mission_id})
    global_notifier.push(
        "SUCCESS",
        "Genesis 施工完成",
        f"{manifest.mission_id} 已组装完成，可以查看结果了。",
        {"session_id": session_id, "mission_id": manifest.mission_id},
    )

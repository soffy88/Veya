from agents._base import Persona
from agents.build import BUILD
from agents.plan import PLAN
from agents.research import RESEARCH

_PERSONAS: dict[str, Persona] = {"build": BUILD, "plan": PLAN, "research": RESEARCH}


def resolve_persona(name: str) -> Persona:
    return _PERSONAS.get(name, BUILD)


__all__ = ["Persona", "resolve_persona"]

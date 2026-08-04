from __future__ import annotations

import re

from hooks.types import HookInput, HookOutput

_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9]{20,})", re.IGNORECASE),
    re.compile(r"(AKIA[A-Z0-9]{16})", re.IGNORECASE),
    re.compile(r"(ghp_[A-Za-z0-9]{36})", re.IGNORECASE),
    re.compile(r"([A-Za-z0-9+/]{40,}=*)", re.IGNORECASE),  # base64-ish
]


def _redact_str(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


async def redact_hook(inp: HookInput) -> HookOutput:
    if inp.result is None:
        return HookOutput(decision="pass")

    result = inp.result
    if isinstance(result, str):
        result = _redact_str(result)
    elif isinstance(result, dict) and "stdout" in result:
        result = dict(result)
        result["stdout"] = _redact_str(str(result["stdout"]))

    return HookOutput(decision="pass", modified_result=result)

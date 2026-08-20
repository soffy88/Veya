"""spec-pack skill — thin wrapper over server.spec_pack."""

from __future__ import annotations

from typing import Any


def main(
    action: str,
    slug: str = "",
    title: str = "",
    brief: str = "",
    stage: str = "",
    body: str = "",
    query: str = "",
    **_: Any,
) -> dict[str, Any]:
    from server.spec_pack import dispatch

    return dispatch(
        action,
        slug=slug,
        title=title,
        brief=brief,
        stage=stage,
        body=body,
        query=query,
    )

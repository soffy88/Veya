"""3O Paradigm SPEC v3.0 Appendix B — CI lint suite (stdlib-only).

Each check module exposes ``check_*(dir) -> list[str]``; the unified entry point
is :mod:`runner`. Checks may also be invoked individually (pre-commit / CI).
"""

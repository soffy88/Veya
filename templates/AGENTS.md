# Project Harness Guide

PROJECT:
LANGUAGE:
BUILD:
TEST:
LINT:
TYPECHECK:
FORMAT:

RULES:
- Keep changes inside the task worktree.

ANTI_PATTERNS:
- Do not publish, deploy, release, or push without explicit approval.

PERMISSIONS:
- Local reads are allowed.
- Task-worktree writes are allowed.
- Remote side effects require approval.

SENSORS:
- List deterministic checks and their expected commands here.

ESCALATION:
- Stop and report when required evidence is unavailable or rules conflict.

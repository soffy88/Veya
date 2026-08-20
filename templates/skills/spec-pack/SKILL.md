---
name: spec-pack
description: Durable spec directory for large/fuzzy requirements. Resume by reading status.json. Not a second agent runtime and not required for small tasks.
---

# spec-pack

Use when the user has a **fuzzy, multi-step product/engineering request** that should survive a crash or a new session. Skip for one-shot questions, typo fixes, or a single `hicode_run`.

Do **not** invent a Coordinator. You still pick tools. This skill only reads/writes files under `~/.veya/specs/<slug>/`.

## Actions

- `start(title, brief)` → slug + empty pack
- `index(slug, query?)` → `codebase.md` from Graft/code map + workspace fingerprint (call this before research if the work touches existing code)
- `advance(slug, stage, body)` → write that stage's markdown; does **not** auto-jump to the next stage
- `resume(slug)` → current stage, missing files, whether the index is stale
- `status` / `list`

## Stages (optional sequence, not a funnel)

`triage → research → requirements → design → tasks → implementation`

Fill a stage with `advance`. Implementation is executed with existing tools (`hicode_run`, `run_in_sandbox`), not inside this skill.

If `resume` says `index_stale`, run `index` again before trusting `codebase.md`.

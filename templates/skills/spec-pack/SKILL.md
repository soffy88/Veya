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
- `export_speckit(slug, project_root?)` → writes `tasks.md`'s content (must already
  be Spec Kit checkbox format — see below) plus a `constitution.md` built from
  `requirements.md`+`design.md` into `<project_root>/.speckit/`. `goal_run`
  already picks up `.speckit/{tasks,constitution}.md` automatically on its next
  `project_run_goal` call for that project — this action only exports the files,
  it does not run the goal.

## Stages (optional sequence, not a funnel)

`triage → research → requirements → design → tasks → implementation`

Fill a stage with `advance`. Implementation is executed with existing tools (`hicode_run`, `run_in_sandbox`), not inside this skill.

The `tasks` stage body must use Spec Kit checkbox syntax if you intend to
`export_speckit` it: `- [ ] T1: title`, optional sub-lines `Depends: T2,T3` /
`Accept: ...`, indentation implies a parent dependency. `export_speckit`
validates the DAG before writing and refuses on zero tasks or a cycle.

Optionally mark a task safe to run concurrently with other marked tasks
(smart-ralph-style, see `server/goal_run/parallel_markers.py`): put `[P]`
right after the checkbox, before the id — `- [ ] [P] T2: title`. Unmarked
tasks always run alone; goal_run never guesses which tasks are safe to
parallelize.

## Coming from a cleared wayfinder map

`wayfind_to_spec(map_id)` (see `server.wayfinding_tools`) collapses a cleared
map's `decisions_so_far` into a new pack's `requirements` stage. From there,
fill `design`/`tasks` normally with `advance`, then `export_speckit` to hand
off to `goal_run`.

If `resume` says `index_stale`, run `index` again before trusting `codebase.md`.

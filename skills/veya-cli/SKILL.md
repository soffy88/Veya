---
name: veya-cli
description: >-
  Drive Veya from an agent: branch rooms, routines and event triggers, ACP
  members, hash-chained audit verification, cited search, goal runs, and
  execution-trajectory replay. Every command takes --json and returns
  {"status": "ok"|"error", ...}; errors exit non-zero.
---

## How to use this skill

1. **Always pass `--json`.** Every command prints exactly one JSON object on
   stdout: `{"status": "ok", ...}` or `{"status": "error", "error": {...}}`.
   Nothing else is ever written to stdout, so the output can be parsed without
   filtering.
2. **Check `status`.** A non-zero exit always accompanies `status: "error"`.
   `error.code` is stable and machine-readable (`FEATURE_DISABLED`,
   `INVALID_INPUT`, `CHAIN_BROKEN`, `NOT_ACP_MEMBER`, …).
3. **Feature flags gate most surfaces.** `veya flags --json` reports them.
   When a command answers `FEATURE_DISABLED`, the surface is switched off by
   design — do not retry, tell the user which flag to enable.
4. **Transport.** With no `--base-url`/`VEYA_BASE_URL` the CLI runs in-process
   (no server needed). With `--base-url` it calls the REST API and sends
   `Authorization: Bearer $VEYA_TOKEN` (the existing token mechanism).
5. **Interactive mode.** Running `veya` with no subcommand opens a REPL where
   each line is a full command; `help <group>` explains a group, `quit` exits.
6. **Quoting.** Pass prompts and queries as single argv entries; prefer
   `--json` parsing over scraping human output.

## Command groups


### `acp`

- `veya acp add BACKEND --owner OWNER`
  - Register an ACP backend as a P1 bot (a group member).
  - JSON schema: `{"status","member"}`
- `veya acp members`
  - List registered ACP members and detectable ACP agents.
  - JSON schema: `{"status","members","candidates"}`
- `veya acp turn MEMBER PROMPT --goal GOAL`
  - Forward one turn to an ACP member inside the existing GoalRun.
  - JSON schema: `{"status","goal_id"}`

### `audit`

- `veya audit records --limit LIMIT`
  - Show the most recent chained audit records.
  - JSON schema: `{"status","records"}`
- `veya audit verify`
  - Re-derive every audit hash link; reports the first break.
  - JSON schema: `{"status","ok","length"}`

### `chat`

- `veya chat messages GROUP`
  - Read back a group transcript (GROUP id).
  - JSON schema: `{"status","messages"}`
- `veya chat send TEXT --bot BOT --group GROUP`
  - Post TEXT into a P2 group as the user role.
  - JSON schema: `{"status","result"}`

### `cli-gen`

- `veya cli-gen --repo REPO`
  - Run the 7-phase agent-native flow (gated by VEYA_CLI_GEN).
  - JSON schema: `{"status","phases","experimental"}`

### `flags`

- `veya flags`
  - Report every NANO-W9 feature flag and whether it is on.
  - JSON schema: `{"status","flags"}`

### `goal`

- `veya goal start GOAL --project-root PROJECT_ROOT`
  - Enqueue a GoalRun through the existing runner (never blocks).
  - JSON schema: `{"status","result"}`
- `veya goal status GOAL_ID --project-root PROJECT_ROOT`
  - Read a GoalRun status; read-only, never mutates.
  - JSON schema: `{"status","result"}`

### `room`

- `veya room list`
  - List rooms whose name carries the branch: prefix.
  - JSON schema: `{"status","rooms"}`
- `veya room open BRANCH --bot BOT --repo REPO`
  - Create or reuse the branch:<name> P2 room.
  - JSON schema: `{"status","group_id","room_name"}`
- `veya room publish --branch BRANCH --group GROUP --repo REPO --goal GOAL --project-root PROJECT_ROOT --decision DECISION`
  - Post commits, CI results, annotations, AI review and the merge decision.
  - JSON schema: `{"status","posted","skipped"}`

### `routine`

- `veya routine list`
  - List every registered routine, disabled included.
  - JSON schema: `{"status","routines"}`
- `veya routine signal KIND --text TEXT --reaction REACTION`
  - Route one webhook/message/reaction signal to the routines it fires.
  - JSON schema: `{"status","fired"}`

### `search`

- `veya search QUERY --limit LIMIT --source SOURCE`
  - Answer "have we seen this before?" with a cited answer.
  - JSON schema: `{"status","answer","receipts","cited"}`

### `trajectory`

- `veya trajectory record --step STEP --command COMMAND --artifact ARTIFACT --preview PREVIEW --goal GOAL --kind KIND`
  - Emit a preview bundle + trajectory entry on the existing event log.
  - JSON schema: `{"status","trajectory"}`
- `veya trajectory replay GOAL`
  - Walk a run into ordered command -> artifact states.
  - JSON schema: `{"status","states","artifacts"}`

## Examples

```bash
# What is switched on?
veya flags --json

# Open (or reuse) the room for a branch, then publish its facts.
veya room open feat/nano-w9 --bot branch-bot --json
veya room publish --branch feat/nano-w9 --group grp_123 --decision squash --json

# "Have we seen this before?" — every answer line cites its source.
veya search "flaky login test" --limit 10 --json

# Tamper-evident audit: verify the append-only hash chain.
veya audit verify --json

# Fire an event trigger (webhook / message / reaction).
veya routine signal message --text "ship it" --json
veya routine signal reaction --reaction "👍" --json

# Register an ACP agent as a group member, then forward one turn to it
# (the turn runs inside the existing GoalRun; ACP is never an authority).
veya acp members --json
veya acp add goose --owner "$USER" --json
veya acp turn acp:goose "review the diff" --json

# Record artifact-producing steps, then replay command -> artifact states.
veya trajectory record --step 1 --command "pnpm build" --artifact build/app.js --goal g1 --json
veya trajectory replay g1 --json

# Enqueue a goal run and poll its status.
veya goal start "fix the flaky test" --project-root . --json
veya goal status <goal_id> --project-root . --json

# Experimental: make a repository agent-native (7 phases), gated by VEYA_CLI_GEN.
veya cli-gen --repo . --json
```


<!-- generated by scripts/gen_veya_cli_skill.py from veya_cli 0.1.0 -->

<!-- commands: 19; parser: veya -->

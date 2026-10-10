# ORCA-W1 Design Document

## Overview

This document describes the implementation design for ORCA-W1, which adds three features to the Veya workspace:

1. **Worktree Shared Paths** - A manifest system to copy untracked files and symlink shared directories into new worktrees
2. **Any CLI Coding Agent** - A registry of CLI agent profiles with PATH-based detection and version reporting
3. **Quota Meter** - Per-backend/account usage tracking with reset time

---

## 1. Worktree Shared Paths

### Manifest Format

A committed file at repo root: `.veya/worktree.toml`

```toml
# Files to copy from source checkout into each new worktree (untracked, skip missing)
copy = [".env", ".env.local", "config/local.yaml"]

# Directories to symlink from source checkout into each new worktree
share = ["node_modules", "apps/web/node_modules", ".venv"]
```

### Implementation

**New module**: `runtime/coding/worktree_manifest.py`
- Parse TOML manifest (stdlib `tomllib` in Python 3.11+)
- Validate paths: relative only, no `..`, no absolute, no symlink escapes
- Apply during `WorktreeManager.create()` after worktree is created
- Idempotent: skip missing copy sources, replace existing symlinks for share
- Log operations without file contents

**Integration point**: `WorktreeManager.create()` in `worktree.py` - call manifest apply after `git worktree add` and before status return.

### REST API

**GET `/api/v1/workspace/shared-paths`**
- Returns current manifest (parsed from `.veya/worktree.toml` at repo root)

**PUT `/api/v1/workspace/shared-paths`**
- Accepts `{copy: string[], share: string[]}`
- Validates each path: relative, no `..`, no absolute, no NUL
- Writes to `.veya/worktree.toml` atomically
- Returns written manifest

### Web UI

Small settings section in workspace/settings showing current copy/share arrays with edit capability.

---

## 2. Any CLI Coding Agent

### Agent Profiles

Built-in profiles (detected via PATH + `--version`):
- `claude-code` → command `claude`, version via `claude --version`
- `codex` → command `codex`, version via `codex --version`
- `opencode` → command `opencode`, version via `opencode --version`
- `cursor-agent` → command `cursor-agent`, version via `cursor-agent --version`
- `gemini` → command `gemini`, version via `gemini --version`
- `qwen` → command `qwen`, version via `qwen --version`

Custom profile: user-defined command template with `{prompt}` and `{cwd}` placeholders.

### Implementation

**New module**: `runtime/coding/cli_agents.py`
- `CLIAgentProfile` dataclass: name, command_template, detected_path, version, is_custom
- `CLIAgentRegistry` class:
  - `discover()` - scan PATH for known binaries, run `--version`, cache results
  - `get(name)` - return profile by name
  - `list()` - return all profiles (built-in + custom)
  - `register_custom(name, command_template)` - store custom profile in `.veya/cli_agents.json`
  - `render_command(profile, prompt, cwd)` - substitute placeholders

**Persistence**: Custom profiles stored in `.veya/cli_agents.json` at repo root.

### Integration with Backend/GoalRun

Extend `server/backends.py`:
- Add `cli` backend kind that uses `CLIAgentRegistry` to render command
- Execution goes through existing `BackendRegistry.run()` → canonical chain (worker.dispatch)
- No keys stored; uses user's own installed login

### REST API

**GET `/api/v1/agents/cli`**
- Returns `{profiles: [{name, command_template, detected_path, version, is_custom, available}]}`

**POST `/api/v1/agents/cli/custom`**
- Accepts `{name, command_template}`
- Validates template contains `{prompt}` and `{cwd}`
- Stores in `.veya/cli_agents.json`
- Returns created profile

### Web UI

Agent picker dropdown wherever a coding task/workspace is started (reuses existing workspace creation UI).

---

## 3. Quota Meter

### Data Model

Per backend/account:
- `backend` - backend name (e.g., "claude-code", "opencode")
- `account` - account identifier (email, user ID, or "unknown")
- `used` - usage in current window (tokens, requests, or "unknown")
- `limit` - limit in current window (or "unknown")
- `reset_at` - ISO timestamp when window resets (or "unknown")
- `unit` - "tokens" | "requests" | "unknown"

### Implementation

**New module**: `runtime/coding/quota.py`
- `QuotaInfo` dataclass
- `QuotaMeter` class:
  - `parse_cli_status(backend_name, cli_output)` - parse known CLI status output formats
  - `parse_response_headers(backend_name, headers)` - parse known API response headers
  - `get_quota(backend_name)` - returns QuotaInfo, falls back to "unknown" for unavailable data
  - Never invents numbers; only reports what CLI/API exposes

**Integration**: Called from backend status or dedicated endpoint.

### REST API

**GET `/api/v1/usage/quota`**
- Returns `{quotas: [QuotaInfo...]}` for all detected/registered backends

### Web UI

Compact status-bar strip showing backend name, used/limit, reset time (or "unknown").

---

## File Structure

```
runtime/coding/
├── worktree_manifest.py     # New: manifest parsing & apply
├── cli_agents.py            # New: CLI agent registry & detection
├── quota.py                 # New: quota meter
├── worktree.py              # Modified: integrate manifest apply in create()
└── models.py                # May add QuotaInfo if shared

server/
├── backends.py              # Modified: CLI backend execution path
├── routes/
│   ├── workspace.py         # New: shared-paths endpoints
│   ├── cli_agents.py        # New: CLI agent endpoints
│   └── usage.py             # New: quota endpoint
└── app.py                   # Modified: include new routers

tests/
└── test_orca_w1_workspace.py  # New: test file
```

---

## Testing Strategy

Tests in `tests/test_orca_w1_workspace.py`:
1. Manifest parsing/apply with tmp git repos
2. Path validation (reject `..`, absolute, etc.)
3. CLI detection with fake PATH
4. Custom template rendering with placeholders
5. Quota "unknown" vs parsed values

---

## Security Considerations

- Path validation prevents directory traversal in all three features
- CLI agent execution goes through canonical chain (worker.dispatch) with permission checks
- No API keys stored; CLI agents use user's own login
- Custom command templates validated for required placeholders only
- Quota meter never invents numbers

---

## Rollout

Feature flags default OFF where behavior would change for existing users:
- Worktree manifest apply: OFF by default (opt-in via manifest existing)
- CLI agent registry: ON (read-only discovery)
- Quota meter: ON (read-only, "unknown" by default)
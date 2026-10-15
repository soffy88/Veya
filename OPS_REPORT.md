# OPS implementation report

- Date: 2026-10-15 (UTC)
- Worktree: `/data/soffy/projects/veya-ops`
- Branch: `feat/ops-golive`
- Status: repository artifacts implemented and committed; **not deployed to the live host**.

## Deliverables

| Item | Result | Details |
|---|---|---|
| Backend resource isolation | Done | `deploy/resources.override.yml` limits Compose service `backend` to 6 GiB memory, 6 GiB total memory+swap, 4 CPUs and 1024 PIDs. The override is separate; the existing Compose file and live launcher were not changed. |
| Uptime/memory monitoring | Done | `scripts/ops/veya_uptime_check.sh`; `deploy/systemd/veya-uptime.service`; `deploy/systemd/veya-uptime.timer`. First run is after 1 minute, then every 2 minutes. Local health requires HTTP 200 and JSON `status=ok`; public `/team` requires HTTP 200. Availability notification triggers once after two consecutive failed runs and resets on recovery. MemAvailable below 2 GiB raises a separate warning. |
| Existing notification channel | Wired in, live delivery unverified | Script calls the existing `POST /integrations/notify?event=veya_uptime_alert` route. If the endpoint call fails, the event goes to the journal/stdout and the state JSON records the check status, failure count and alert flags. No second scheduler or notification authority was added. |
| Free-only gateway failover | Done as opt-in config | `deploy/gateway-free-fallback.env.example` uses existing `VEYA_GATEWAY_FAILOVER` parser chains to try the existing free pools and then `cliproxy/gemini-3-flash`, followed by `cliproxy/gemini-3.5-flash-lite`. No gateway code or model catalog is hardcoded/changed. Example is not auto-loaded; default behavior remains unchanged until an operator merges its assignment into the root `.env`. |
| Deployment and rollback | Done | `OPS_DEPLOY.md` describes promotion, guarded launcher edit to add the resource overlay, protected backups, env opt-in, gateway refresh, user-timer setup, validation and rollback. It explicitly keeps live changes as operator actions. |

## Evidence and tests

| Check | Result | Evidence / limitation |
|---|---|---|
| Resource override YAML parse | PASS | Parsed with `yaml.safe_load`; exact memory/swap/CPU/PID values asserted. |
| Uptime shell syntax | PASS | `bash -n scripts/ops/veya_uptime_check.sh`. |
| Uptime consecutive-failure logic | PASS | Two isolated invocations used loopback port `127.0.0.1:1` as intentionally unreachable test URLs and a temporary state file under `$TMPDIR`. First failure produced count 1 and no availability alert; second produced count 2, set `failure_alerted=true`, and logged channel-unavailable fallback. No production URL or notification service was called in this test. |
| Existing gateway parser | PASS | Executed the source AST for `_failover_chain` alone (not the gateway application) and asserted ordering for `veya-fllm`, `veya-wb`, `veya-free`, `veya1.2`, `veya1.2-flash`, and `veya1.2-free`. The two CLIProxy IDs are last in the required order. |
| systemd static validation | PASS with unrelated warning | `systemd-analyze --user verify deploy/systemd/veya-uptime.service deploy/systemd/veya-uptime.timer` exited successfully. It emitted an existing unrelated warning for `/usr/lib/systemd/user/spice-vdagent.service` (`StandardError` in `[Install]`); neither new unit produced a validation error. |
| Whitespace | PASS | `git diff --check` and staged diff checks passed. |
| Full merged Compose config | Not validated here | An initial `docker compose config --quiet` attempt could not interpolate required production values absent in this worktree (`VEYA_DURABLE_EXECUTION`, `VEYA_EXECUTION_DATABASE_URL`, `VEYA_PSEUDO_SECRET`, and `VEYA_EXECUTION_DB_*`). Only the overlay YAML itself was parsed. `OPS_DEPLOY.md` contains the quiet validation command for the operator's configured deployment checkout. |
| Live checks / quota | Not performed | Public `/team`, live `/health`, notification delivery, actual CLIProxyAPI model availability, the account's free-tier/quota status and gateway reachability from inside `veya-gateway-internal` were intentionally not exercised. The runbook requires these checks before enabling the config. |

The `docker stats --no-stream` observation at the start of the task was `veya-backend 23.89MiB / 30.42GiB`, CPU approximately `0.08%` while idle. This is only an idle sample, not peak workload evidence; the 6 GiB limit is a proposed guardrail and must be reviewed against peak behavior before deployment.

## Safety / deployment state

- No push was performed.
- No running service or container was restarted, recreated, or rebuilt.
- No host config (`~/.config/veya`, root `.env`, Caddy, `/etc`) was changed by this task.
- No gateway code, protected auth/app files, CI workflows, protected tests, or bot/HARN files were changed.
- All created/committed repository artifacts are inside this worktree. Existing untracked `.venv` remains untouched and unstaged.
- Fallback remains opt-in. Before enabling it, the operator must verify both exact model IDs and their free-tier status, and verify the configured CLIProxyAPI base is reachable inside the gateway container. No credentials are present in the new artifacts.

## Commits

- `a343d44a` — `ops: add backend resource limits override`
- `a0e86cab` — `ops: add two-minute uptime and memory monitor`
- `b0340e11` — `ops: configure free CLIProxy fallback chain`
- `dbbb921e` — `docs: add ops rollout backup and rollback runbook`
- This report is committed separately after its content and the final worktree status are checked.

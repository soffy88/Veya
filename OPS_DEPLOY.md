# Veya Ops rollout runbook

This change set is deliberately **not applied to the running host by this task**. It contains a resource overlay, a user-systemd uptime timer, and an opt-in LLM failover example. Do not push from this worktree. Keep this worktree available while the uptime unit's `ExecStart` points into it.

## 0. Promote the reviewed worktree changes

Run on the host only when the deployment branch is ready. First check the canonical checkout. If `git status` shows any tracked or untracked work, stop and preserve it; do not stash, reset, clean, or discard it. From the intended deployment branch in the canonical checkout:

```bash
cd /data/soffy/projects/veya
git status --short --branch
# Continue only after the output confirms there is no local WIP.
git merge --no-ff feat/ops-golive
```

No push is needed. The merge makes `deploy/resources.override.yml` available beside the canonical Compose file. It also ensures the gateway config example and documentation are in that checkout. The uptime script and user unit are intentionally run from `/data/soffy/projects/veya-ops`; preserve that worktree until the unit is deliberately migrated to another stable path.

## 1. Back up host configuration before changing anything

Run this backup block before editing host state. `umask 077` protects the copy of the `.env` file; never print that file because it contains credentials. The selected backup path is persisted so rollback can be run from another shell.

```bash
set -eu
umask 077
REPO=/data/soffy/projects/veya
OPS_WORKTREE=/data/soffy/projects/veya-ops
test -f "$HOME/.config/veya/backend-up.sh"
test -f "$REPO/.env"
BACKUP="$HOME/.local/state/veya-ops-backup-$(date +%Y%m%d%H%M%S%N)"
mkdir -p "$BACKUP" "$HOME/.local/state"
cp -a "$HOME/.config/veya/backend-up.sh" "$BACKUP/backend-up.sh"
cp -a "$REPO/.env" "$BACKUP/root.env"
for unit in veya-uptime.service veya-uptime.timer; do
  if [ -e "$HOME/.config/systemd/user/$unit" ]; then
    cp -a "$HOME/.config/systemd/user/$unit" "$BACKUP/$unit"
  fi
done
if systemctl --user is-enabled veya-uptime.timer >"$BACKUP/uptime-timer-enabled-state" 2>/dev/null; then :; else printf 'disabled-or-absent\n' >"$BACKUP/uptime-timer-enabled-state"; fi
printf '%s\n' "$BACKUP" > "$HOME/.local/state/veya-ops-last-backup"
printf 'Backup saved at: %s\n' "$BACKUP"
```

Keep the printed backup path for rollback. This step only reads/copies files; it does not reload or restart any service.

## 2. Add backend container resource limits

The overlay is additive and targets the Compose service `backend` / container `veya-backend`: `mem_limit: 6g`, `memswap_limit: 6g`, `cpus: 4.0`, `pids_limit: 1024`. The read-only `docker stats` observation for this work was approximately 23.89 MiB / 30.42 GiB while idle; that is not a peak-load measurement. Review peak usage before rollout.

The host launcher currently builds its Compose command with `docker-compose.yml`, `~/.config/veya/backend-override.yml`, and the Tailnet override. After the backup above, insert the resource overlay into that existing `-f` list without dropping either existing override. The following guarded edit expects the known current command shape and stops rather than guessing if it differs:

```bash
python3 - "$HOME/.config/veya/backend-up.sh" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
source = p.read_text(encoding="utf-8")
needle = '-f "$HOME/.config/veya/backend-override.yml" -f /data/soffy/projects/veya-tnw/deploy/tnw-tailnet.override.yml'
replacement = '-f "$HOME/.config/veya/backend-override.yml" -f /data/soffy/projects/veya/deploy/resources.override.yml -f /data/soffy/projects/veya-tnw/deploy/tnw-tailnet.override.yml'
if source.count("resources.override.yml"):
    raise SystemExit("resources.override.yml already appears in backend-up.sh; inspect it manually")
if source.count(needle) != 1:
    raise SystemExit("Expected Compose argument sequence was not found exactly once; no file changed")
p.write_text(source.replace(needle, replacement), encoding="utf-8")
PY
bash -n "$HOME/.config/veya/backend-up.sh"
grep -n 'resources.override.yml' "$HOME/.config/veya/backend-up.sh"
```

Validate the merged Compose configuration quietly before applying it; do not run `docker compose config` without `--quiet` because the rendered config can contain secrets. This preserves both existing overrides and their ordering:

```bash
cd "$REPO/deploy"
docker compose -p deploy \
  -f docker-compose.yml \
  -f "$HOME/.config/veya/backend-override.yml" \
  -f "$REPO/deploy/resources.override.yml" \
  -f /data/soffy/projects/veya-tnw/deploy/tnw-tailnet.override.yml \
  --env-file .env --env-file "$HOME/.config/veya/backend.interp.env" \
  config --quiet
```

When the maintenance window is approved, run the existing launcher to apply the backend-only change (it uses `--no-deps --no-build`; this can recreate the backend container, so do not run outside that window):

```bash
"$HOME/.config/veya/backend-up.sh"
docker stats --no-stream veya-backend
```

## 3. Enable free-only CLIProxyAPI fallback (opt-in)

The `veya-llm-gateway` Compose service already reads the canonical checkout's root `.env` via `env_file: ../.env`. The example file adds one `VEYA_GATEWAY_FAILOVER` assignment; the existing variable defaults to empty, so no behavior changes until that assignment is enabled. It keeps the normal free-pool discovery enabled; **do not set `VEYA_FREE_POOL_STATIC`**, as that option replaces the discovered pool rather than adding to it.

Before enabling the example, verify through the current CLIProxyAPI account that the exact IDs `gemini-3-flash` and `gemini-3.5-flash-lite` are exposed by `/v1/models` and really use the intended free quota/tier. Check that the configured `VEYA_CLIPROXY_BASE` is reachable from inside `veya-gateway-internal`; `127.0.0.1` inside a container is the container itself, not automatically the host. Do not print or copy API keys. If either model ID or free-tier eligibility cannot be verified, leave fallback disabled.

Only append the assignment when the canonical root `.env` does not already define it. This command will stop without writing if a prior assignment exists, so merge intentionally rather than creating duplicate definitions:

```bash
set -eu
REPO=/data/soffy/projects/veya
if grep -q '^VEYA_GATEWAY_FAILOVER=' "$REPO/.env"; then
  echo 'VEYA_GATEWAY_FAILOVER already exists; review and merge manually. Nothing appended.' >&2
  exit 1
fi
sed -n '/^VEYA_GATEWAY_FAILOVER=/p' "$REPO/deploy/gateway-free-fallback.env.example" >> "$REPO/.env"
```

The chain uses the gateway's existing exact alias parser and discovered catalog IDs: `veya-fllm` and `veya-wb` can try the sibling free pool before `cliproxy/gemini-3-flash`, then `cliproxy/gemini-3.5-flash-lite`. `veya-free`, `veya1.2`, `veya1.2-flash`, and `veya1.2-free` are covered too. No OpenAI, Anthropic, DeepSeek, paid Zen model, or arbitrary `auto:` entry is added. The current parser does not interpret `auto:` as a catalog alias; use the exact `cliproxy/<model-id>` names discovered at startup.

After the env file is backed up and the variable is enabled, recreate only the internal gateway during the approved maintenance window so it rereads the env file (this is an operator action, not performed by this task):

```bash
cd "$REPO/deploy"
docker compose -p deploy \
  -f docker-compose.yml \
  -f "$HOME/.config/veya/backend-override.yml" \
  -f "$REPO/deploy/resources.override.yml" \
  -f /data/soffy/projects/veya-tnw/deploy/tnw-tailnet.override.yml \
  --env-file .env --env-file "$HOME/.config/veya/backend.interp.env" \
  up -d --no-deps --no-build veya-llm-gateway
```

Then verify the two exact IDs appear in the gateway catalog without printing credentials:

```bash
docker exec veya-gateway-internal python -c 'import json,urllib.request; data=json.load(urllib.request.urlopen("http://127.0.0.1:8791/v1/models", timeout=5)); ids={item.get("id", "") for item in data.get("data", [])}; wanted={"cliproxy/gemini-3-flash", "cliproxy/gemini-3.5-flash-lite"}; print("required CLIProxyAPI fallback IDs present:", wanted <= ids); raise SystemExit(0 if wanted <= ids else 1)'
```

The check confirms catalog discovery, not provider quota; free-tier status must have been verified independently above.

## 4. Install the uptime timer

The two-minute timer checks local `/health` for HTTP 200 plus `{"status":"ok"}`, public `/team` for HTTP 200, and host `MemAvailable` below 2 GiB. It raises one availability notification after two consecutive failed checks and resets that incident when both endpoints recover. It calls the existing `POST /integrations/notify` endpoint. If that endpoint/channel is down, the event is logged to journald and check/alert state is retained in `$HOME/.local/state/veya-uptime.json`.

Install the repository files as user units; the service runs the script with Bash, so a separate chmod is not required:

```bash
set -eu
OPS_WORKTREE=/data/soffy/projects/veya-ops
mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/state"
cp -a "$OPS_WORKTREE/deploy/systemd/veya-uptime.service" "$HOME/.config/systemd/user/veya-uptime.service"
cp -a "$OPS_WORKTREE/deploy/systemd/veya-uptime.timer" "$HOME/.config/systemd/user/veya-uptime.timer"
systemd-analyze --user verify \
  "$HOME/.config/systemd/user/veya-uptime.service" \
  "$HOME/.config/systemd/user/veya-uptime.timer"
systemctl --user daemon-reload
systemctl --user enable --now veya-uptime.timer
```

Check status and the latest logs:

```bash
systemctl --user list-timers veya-uptime.timer
systemctl --user status veya-uptime.timer --no-pager
journalctl --user -t veya-uptime --since '30 minutes ago' --no-pager
python3 -m json.tool "$HOME/.local/state/veya-uptime.json"
curl -fsS http://127.0.0.1:8767/health
curl -sS -o /dev/null -w '%{http_code}\n' https://veya.aiinote.com/team
```

## 5. Rollback

Use the exact backup directory printed in Step 1. These steps intentionally retain files rather than delete them.

```bash
set -eu
REPO=/data/soffy/projects/veya
BACKUP="$(cat "$HOME/.local/state/veya-ops-last-backup")"
test -f "$BACKUP/backend-up.sh"
test -f "$BACKUP/root.env"
systemctl --user disable --now veya-uptime.timer || true
cp -a "$BACKUP/backend-up.sh" "$HOME/.config/veya/backend-up.sh"
cp -a "$BACKUP/root.env" "$REPO/.env"
for unit in veya-uptime.service veya-uptime.timer; do
  if [ -f "$BACKUP/$unit" ]; then
    cp -a "$BACKUP/$unit" "$HOME/.config/systemd/user/$unit"
  fi
done
systemctl --user daemon-reload
if grep -qxE 'enabled|enabled-runtime' "$BACKUP/uptime-timer-enabled-state" 2>/dev/null; then
  systemctl --user enable --now veya-uptime.timer
fi
```

During an approved rollback window, apply the restored launcher/config to return the backend to its former Compose resource settings, and recreate the internal gateway so it rereads the restored environment. Re-run the health checks in Step 4. If the code merge itself must be reverted, inspect the merge commit and use `git revert -m 1 <merge-commit-sha>`; do not use reset/clean or discard later work. No push is part of this runbook.

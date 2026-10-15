# TNW deployment runbook (manual; not executed by this change)

## Preconditions — keep production unchanged until all pass

1. Confirm this host still owns Tailnet IPv4 `100.86.95.70` (`tailscale ip -4`). The current read-only identity check reported `ubuntu26.taile7dda2.ts.net.` and `100.86.95.70`; verify again immediately before deployment.
2. Resolve the deploying owner's exact `user_id` through the existing Veya auth owner resolution/database, read-only. The inspected `~/.config/veya/backend.interp.env` had no `VEYA_OWNER_USER_ID` entry. Do not guess an ID. Add `VEYA_TAILNET_OWNER_USER_ID=<the existing owner user_id>` to that local interpolation env file only during the operator-approved deployment. Set `VEYA_TAILNET_NODE=ubuntu26.taile7dda2.ts.net` (without the optional trailing dot) there too. Those local values are deliberately not committed here.
3. Before deploying, establish that Docker's publish path preserves the original Tailnet source address. This was not checked here because this task forbids Docker operations. The HTTP handler intentionally rejects Docker bridge/gateway addresses rather than trusting forwarded headers. If actual requests arrive from a bridge address due to userland-proxy/NAT, stop: do not deploy/enable this override until a separately reviewed host-side source-preserving forwarding path is available.
4. Confirm port 7767 is unused on the Tailnet address. Do not use port 7766.

## Install the host-side whois-only proxy

Review `deploy/tailnet_whois_proxy.py` and this unit before installation. The raw Tailscale LocalAPI socket must not be mounted into the app container: read-only mount flags do not restrict API operations. The proxy runs as root because tailscaled's socket is root-owned, but exposes only GET whois queries for Tailnet IPs on `/run/veya/tailnet-whois.sock`.

During the approved maintenance window, install and enable the unit:

```sh
sudo install -m 0644 /data/soffy/projects/veya-tnw/deploy/veya-tailnet-whois-proxy.service /etc/systemd/system/veya-tailnet-whois-proxy.service
sudo systemctl daemon-reload
sudo systemctl enable --now veya-tailnet-whois-proxy.service
```

Verify the service state and that `/run/veya/tailnet-whois.sock` is a Unix socket owned by root with mode `0660`; do not expose or mount `/var/run/tailscale/tailscaled.sock` into `veya-backend`. The proxy uses the host's `/var/run/tailscale/tailscaled.sock` internally. It does not run arbitrary LocalAPI paths or forward bodies.

## Review/apply the backend script patch manually

The repository copy of the deployment override is `deploy/tnw-tailnet.override.yml`. The patch file only changes how the existing local backend script includes that override; this task did not modify `~/.config/veya/backend-up.sh`.

First inspect the patch and run a dry-run:

```sh
sed -n '1,80p' /data/soffy/projects/veya-tnw/deploy/backend-up.tailnet.patch
patch --dry-run -p0 -d "$HOME/.config/veya" < /data/soffy/projects/veya-tnw/deploy/backend-up.tailnet.patch
```

Only after the preconditions, owner values, source-IP preservation, and patch context are confirmed, the operator may apply it:

```sh
patch -p0 -d "$HOME/.config/veya" < /data/soffy/projects/veya-tnw/deploy/backend-up.tailnet.patch
```

The override publishes only `100.86.95.70:7767:7767`; container bind `0.0.0.0` is accepted only with the explicit trust switch, exact publish host, configured proxy socket, and node name. Compose must use the normal project files and this additional override. Review the resolved compose configuration in the approved change window before invoking the existing `backend-up.sh`; this task intentionally did not run it or any Docker command.

## Owner policy and live verification

After backend deployment, use the already-authenticated owner API client to GET `/api/v1/tailnet/policy` first. Review existing values. Ensure `mode` is `allowlist`, `allow` contains only `user:soffy88@gmail.com`, and `deny` contains the intended denies; deny always wins. Do not overwrite unrelated bindings/wake policy blindly. Apply the policy through the existing authenticated `PUT /api/v1/tailnet/policy` route; never insert a guessed owner ID or raw API credential in the repository. Verify `/api/v1/tailnet/status` reports `enabled=true`, `listening=true`, and port 7767.

From another device on the same Tailnet, confirm its source IP is in the Tailnet range and that its Tailscale login is `soffy88@gmail.com`. Use any installed, trusted tailmsg v1 client pointed at `100.86.95.70:7767`; an equivalent minimal JSON test can POST to `http://100.86.95.70:7767/msg` with `Content-Type: application/json` and this envelope (replace `from` with the sender node's DNS name, `to` with `ubuntu26.taile7dda2.ts.net`, and `ts` with current UTC):

```json
{
  "v": 1,
  "id": "tnwtest000000001",
  "from": "sender-node.tailnet.ts.net",
  "to": "ubuntu26.taile7dda2.ts.net",
  "ts": "2026-10-15T00:00:00Z",
  "kind": "text",
  "text": "TNW tailmsg v1 connectivity test"
}
```

The message content is inert input data and must never be interpreted as a shell command. Expect HTTP 202 for an accepted message; use a fresh unique ID each try. Check the response and existing channel receipts/logs. If the source resolves to a non-Tailnet gateway IP, whois fails, or the sender login is not in the allowlist, expect rejection; do not add a proxy header workaround or weaken authentication. `.ref/yobiru/tailmsg.py` is not present in this worktree, so a copy of that client was not assumed or invented.

## Rollback

Do not automatically restart or alter production during this task. In an approved maintenance window, remove only the additional compose override from the local backend script to stop publishing port 7767, then disable the dedicated whois-proxy unit if no other consumer depends on it. Preserve the project files and all existing local overrides; do not remove the user's worktree or any outside path.

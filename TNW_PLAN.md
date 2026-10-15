# TNW implementation plan

## Decision

Keep the existing W20 HTTP/tailmsg-v1 receiver and its policy/receipt path. Do not add a second dispatcher, approval engine, scheduler, or event store. Add the container networking contract, a constrained host-side whois adapter, and regression tests.

The backend container has no Tailscale interface or CLI. Support explicit VEYA_TAILNET_BIND; the default remains binding to a verified Tailscale IP. A container wildcard bind is permitted only when the explicit Docker-publish trust switch, exact Tailnet publish host, whois-proxy socket, and local node name are all configured.

Publish host port 7767 only at 100.86.95.70:7767:7767; keep 7766 untouched for Yobiru. The receiver rejects any inbound client_address outside Tailscale IPv4/IPv6 ranges and fails closed if whois cannot identify a peer. Docker gateway source addresses cannot be safely mapped back to an authenticated Tailnet source.

- The Compose overlay explicitly sets `VEYA_TAILNET_CHANNEL=1`; `deploy/bot_landing_flags.env` is not assumed to be injected into the backend container by the existing wrapper.

## Whois transport

Do not mount /var/run/tailscale/tailscaled.sock directly into veya-backend. A read-only bind mount does not restrict API methods; this host's socket was observed as root-owned mode 0666. Run deploy/tailnet_whois_proxy.py on the host. It creates a separate Unix socket and forwards only a bounded, body-less GET /localapi/v0/whois?addr=<Tailnet-IP>; other paths/methods/addresses are rejected. Mount only the proxy socket in the backend container. The root-running proxy still has privileged LocalAPI access, so its strict allowlist, socket permissions, timeouts and response-size limits are security boundaries.

## Policy

A fresh policy defaults to mode=allowlist, allow=["user:soffy88@gmail.com"], and empty deny; existing stored policy is not silently rewritten. authorize_peer retains deny-first evaluation. The management API keeps existing owner resolution and accepts VEYA_TAILNET_OWNER_USER_ID / VEYA_OWNER_USER_ID or existing owner/admin claims. Deployment must populate the strict owner ID from the existing auth owner resolution, never invent one.

## Production gates / explicit non-actions

No Docker commands, deployment script, systemd installation, service start/restart, or ~/.config file edits are performed by this change. Docker source-IP preservation has not been empirically verified because this task forbids Docker operations. Before applying the deployment patch, verify published connections arrive at the handler with the actual 100.64.0.0/10 or fd7a:115c:a1e0::/48 peer IP. If Docker userland-proxy rewrites it to a bridge/gateway address, this receiver will return 403; do not weaken the source check. Keep deployment gated until the Docker path preserves source IP or a separately reviewed host-side forwarding design is adopted.

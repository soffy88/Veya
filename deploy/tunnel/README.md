# Veya OpenAI Secure MCP Tunnel

ChatGPT Web reaches the local Veya MCP through the **OpenAI Secure MCP Tunnel**,
without exposing anything:

```
ChatGPT Web
  -> OpenAI Secure MCP Tunnel (control plane)
  -> official tunnel-client runtime   (this unit)
  -> http://127.0.0.1:8790/mcp        (Veya Remote MCP, loopback only)
  -> existing Veya ToolRuntime / Hicode
  -> local workspace (isolated task-remote-* worktree)
```

The public `https://veya.aiinote.com/mcp` stays as an **optional external**
transport; it is not the ChatGPT Web path and is not removed.

## Official client

Downloaded from `https://github.com/openai/tunnel-client` release `v0.0.14`
(SHA256-verified, not forked):

| | |
|---|---|
| `~/.local/bin/tunnel-client` | full client (doctor/admin/profiles) |
| `~/.local/bin/tunnel-client-runtime` | runtime-only flavor used by the service |

## Secrets (two independent credentials)

| | file | used by |
|---|---|---|
| OpenAI tunnel runtime key (Restricted: Tunnels Read + Use) | `~/.veya/openai-tunnel/runtime_api_key` (0600) | tunnel control plane only |
| Veya Remote MCP bearer | `~/.veya/remote_mcp_token.secret` (0600) → `~/.veya/remote_mcp_auth_header` (0600: `Bearer <token>`) | tunnel-client → 127.0.0.1:8790 only |

Never reuse one for the other. The Veya bearer is referenced with the official
`file:` mechanism as a **static MCP header**; tunnel-client only sends it to the
configured MCP origin, never to the control plane. No secret goes into Git, the
unit file, argv, or any report.

## Owner inputs (must exist before the tunnel can connect)

```text
~/.veya/openai-tunnel/runtime_api_key   -> restricted Runtime API key   (0600)
~/.veya/openai-tunnel/tunnel.env        -> CONTROL_PLANE_TUNNEL_ID=tunnel_...  (0600)
```

Create the tunnel in <https://platform.openai.com/settings/organization/tunnels>
and a Runtime API key (Tunnels **Read** + **Use**) in
<https://platform.openai.com/settings/organization/api-keys>. The unit is skipped
(`ConditionPathExists`) until `tunnel.env` exists, so a missing input cannot
cause a restart loop.

## Service

```bash
cp deploy/tunnel/veya-openai-tunnel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now veya-openai-tunnel
systemctl --user status  veya-openai-tunnel     # start/stop/restart/status
journalctl --user -u veya-openai-tunnel -f      # logs
python scripts/doctor_openai_tunnel.py          # doctor
python scripts/qualify_openai_secure_tunnel.py  # qualification
```

## ChatGPT App

After the tunnel reports ready, the manual last step is create-once in ChatGPT:
App name **Veya Local**, Authentication **No Auth**, connect by **Tunnel ID**
(never the localhost URL). The Veya bearer stays inside the tunnel-client.

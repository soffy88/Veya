# W13 — Cross-device agent control (draft, 2026-10-13)

Goal: from phone/web, drive agents running on ubuntu26 and other tailnet hosts — start tasks (incl. voice), watch several agents in parallel, hand a task from one agent to another; get notified when an agent finishes or needs approval. After W13 Veya is release-ready (plus stable brain decision).

Inspiration (borrow ideas only):
- Paseo (getpaseo/paseo, AGPL-3.0) → CLEAN-ROOM ONLY. No code, no copied text.
- cmux → done / needs-approval notifications, session restore.
- Yobiru (chekusu/yobiru, MIT) → may borrow with attribution. Adopt these three designs:
  1. Machine identity via Tailscale: a remote host/agent posting to Veya is identified by `tailscale whois` on the peer address (node + user login), checked against a local policy (tailnet default, optional allowlist, deny wins; rules by hostname/FQDN/tag:/user:). Message access never implies permission to wake, execute or auto-reply.
  2. Wake policy on the receiver: sender may request wake now/batch/none; the receiving side decides, per sender+agent, with quiet hours and rate limits. Integrate with existing routines/approval — no new scheduler or approval authority.
  3. File handoff with integrity: attachments staged on the sender behind an expiring link (default 24h) restricted to the recipient machine; recipient fetches and verifies size + SHA-256 before use. Contents are data, never instructions.
- Delivery semantics (from Yobiru): accepted (202) ≠ woken ≠ acked ≠ done; never claim completion without verifier evidence (ties into W12 supervisor).

Constraints: behind flag VEYA_REMOTE_CONTROL default OFF; reuse agent_mailbox / GoalRun / routines / existing approval; no second scheduler, event store, approval or capability registry; don't touch platform/3O or others' veya/remote/* WIP (coordinate first). New REST routes need owner auth checks. Flag new model-callable tools.

Full spec to be written after W12 lands.

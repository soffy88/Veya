# THIRD_PARTY_NOTICES

Veya is MIT licensed (see `LICENSE`). This file records third-party material
that Veya has drawn on — **design-level borrowing only**, unless a section
explicitly says code was vendored. Entries are **append-only**: a section is
never rewritten or removed, because the record of what an earlier wave borrowed
is part of the audit trail.

Nothing in this file grants a licence to upstream code. Where upstream code
was not read or copied, this file says so, and that statement is the point.

---

## NANO-W10 group A (turn runtime core)

Sources consulted for this work: `W10_GROUP_A.md`, `NANO_W10_SPEC.md`, and
this repository. **No third-party source code was read, vendored, or copied.**

| Upstream named in the spec | Licence | What was actually done |
|---|---|---|
| Lee Robinson, Stanford CS146S lecture "How always-on, proactive agents work" (Oct 2026) | n/a — a talk, not a code base | The **feature prose** in `NANO_W10_SPEC.md` item 1/2 and addendum W3–W5 informed *what to build*. No transcript source, no slides, no code was read. The design is derived from Veya's own codebase. |
| `FeiZhuLulu/DeepSeek-Bot` | Apache-2.0 | Listed in the spec ADDENDUM. **Its `dsh` TypeScript plugin was not vendored and not read as source.** None of group A's items (turn queue, wake router, Goal/Loop, live stream, finish card) came from it. |
| `Panniantong/Agent-Reach` | MIT | Listed in the spec ADDENDUM (item V, channel ladders). **Group A does not implement item V**; no upstream tool was vendored. |
| `totec448-spec/chat-on-steroids` | MIT | Addendum W1–W5 informed *what to build* for group A (goal vs loop, live stream, finish checkpoint). **Its ChatGPT web-UI automation/recording was explicitly not consulted or copied** (provider-terms risk, and out of scope). |

Group A implements: spec item 1 (turn queue), item 2 (wake router + "worth
bugging the user?" policy), and addendum W3 (Goal vs Loop), W4 (live
tool-result stream + plan list + correction box), W5 (finish checkpoint card).
All design was written against this repository's existing stores, engines and
frozen decisions — see `docs/NANO_W10_A_DESIGN.md` §1 for what was deliberately
*not* introduced (no second scheduler, task store, approval system, event store,
side-effect system or capability registry).

### Attribution

Design influence is credited to the projects named above as required by their
licences. Because no upstream code was copied, none of their licence texts are
reproduced here, and no NOTICE file from any upstream project is redistributed.

---

# Third-Party Notices

Design-level attributions for ideas borrowed from external projects. Entries are
**append-only**: add new projects at the end, never rewrite or drop an existing
entry, and never remove a notice when the borrowing code is later rewritten.

## Scope of this file

Only work actually derived from an external project is listed. "Design borrowed"
means an idea was reimplemented in this repo's own architecture; **no third-party
source code is vendored** into this repository for any entry below. Where a
project is TypeScript-only, the idea was ported by hand into the repo's Python
3O layers rather than translated line by line.

---

## DeepSeek-Bot

- **Upstream:** FeiZhuLulu/DeepSeek-Bot
- **License:** Apache-2.0
- **Use:** design borrowed, no code vendored.
- **Borrowed by:** NANO-W10 group B (M, N, O, S)
- **What was borrowed:**
  - **M — Context steward.** The idea of a single small model call at turn end
    that chooses between *checkpoint* (condense in place) and *handoff* (start a
    new part) once a conversation crosses a token line. Reimplemented in
    `veya/omodul/context_steward.py` + `veya/oskill/pure/steward_decide.py`
    with an injected `LLMCaller`, an 0.8/0.95 check/hard threshold and a
    per-part frozen context.
  - **N — `read_own_chat`.** Searching a bot's own earlier conversation with
    citable `part N #id` anchors. Reimplemented in
    `server/bot_team/own_chat.py` over the existing session FTS index; anchor
    formatting in `veya/oskill/pure/chat_anchors.py`.
  - **O — Memory review + team memory.** A per-turn review pass that keeps only
    what the user said or decided, refuses credential-shaped entries, and
    journals every change. Reimplemented in
    `veya/omodul/memory_review.py`, `veya/oskill/pure/memory_layers.py` and
    `server/bot_team/memory_layers.py`.
  - **S — Usage page token detail.** Token totals by time range / bot / model
    derived from existing usage events. Reimplemented in
    `veya/omodul/token_usage.py` and `GET /api/v1/usage/tokens`.
- **Notes:** DeepSeek-Bot is a TypeScript `dsh` plugin; the ideas were ported
  into this repo's Python 3O layers by hand. No upstream code is present.

## chat-on-steroids

- **Upstream:** totec448-spec/chat-on-steroids
- **License:** MIT
- **Use:** design borrowed, no code vendored.
- **Borrowed by:** NANO-W10 group B (W1, W2)
- **What was borrowed:**
  - **W1 — Persistent workers.** The idea that a delegated worker keeps its own
    context across tasks so the next task resumes rather than cold-starts.
    Reimplemented in `server/bot_team/workers.py` +
    `veya/oskill/pure/worker_brief.py` (rows in `workers.json`, a
    `context_size` budget, reset/retire) on top of the existing
    `DelegateRuntime`. Not borrowed: any ChatGPT web-UI automation or recording
    (provider-terms risk; explicitly out of scope per the spec).
  - **W2 — Compact & resume carries workers.** The idea that the single
    compact/handoff brief should also carry each worker's summary and open items
    so a fresh session continues all of them. Reimplemented via
    `resume_brief_for` and `merge_worker_briefs` reusing the existing
    fork/handoff brief (no new store).
- **Notes:** Web-UI automation/recording from the upstream was deliberately not
  borrowed.

## yobiru

- **Upstream:** chekusu/yobiru
- **License:** MIT
- **Use:** design borrowed, no code vendored.
- **Borrowed by:** W13 (cross-device agent control)
- **What was borrowed** (three ideas, reimplemented in Veya's own architecture
  under `server/remote_control/`):
  - **Machine identity via Tailscale.** A remote peer is identified by
    `tailscale whois --json <ip>` (node + user login), checked against a local
    policy — tailnet default, optional allowlist, deny wins, rules by
    hostname / FQDN / `tag:` / `user:`. Reimplemented in
    `server/remote_control/identity.py` behind an injectable runner seam.
    Message access never implies permission to wake, execute or auto-reply.
  - **Receiver-side wake policy per sender + agent.** The sender may *request*
    wake now / batch / none; the receiving side decides, with quiet hours and a
    rate limit. Reimplemented in `server/remote_control/wake.py` on top of the
    existing `RoutineRegistry` + `RoutineClock` (no new scheduler).
  - **File handoff with integrity.** A staged attachment is fetched behind an
    expiring (default 24h) link bound to one recipient; the recipient verifies
    size **and** SHA-256 before use, and the contents are treated as data, never
    instructions. Reimplemented in `server/remote_control/files.py`.
- **Notes:** yobiru is a TypeScript project; the ideas were ported into this
  repo's Python layers by hand. No upstream source code is present.
- **Not borrowed:** the delivery vocabulary (`accepted → woken → acked → done`,
  never claiming completion without verifier evidence) is credited in
  `W13_REMOTE_CONTROL_SPEC.md` §3 to `cmux` as inspiration prose; the
  implementation reuses this repository's own verifier (`server/goal_run/verify.py`)
  and needs no upstream material.

## Paseo (clean-room — no borrowing)

- **Upstream:** getpaseo/paseo
- **License:** AGPL-3.0
- **Use:** **clean-room only.** Named in `W13_REMOTE_CONTROL_SPEC_DRAFT.md` as
  inspiration, but **no upstream file was opened, no code and no prose was
  copied**, and nothing in this repository derives from Paseo source. W13's
  remote-control code was written from the requirements in
  `W13_REMOTE_CONTROL_SPEC.md` and from Veya's own code.

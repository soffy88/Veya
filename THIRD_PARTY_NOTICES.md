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

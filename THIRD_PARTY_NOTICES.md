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

## gstack

- **Upstream:** garrytan/gstack
- **License:** MIT
- **Use:** design borrowed, Python reimplementation, no code vendored.
- **Borrowed by:** Veya GSK (5 capabilities: Evidence Ledger, Egress Receipts, Trust Envelope, Careful/Freeze Guard, Domain Skills)
- **Copyright & License (from garrytan/gstack):**
  ```
  MIT License

  Copyright (c) 2026 Garry Tan

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the "Software"), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in all
  copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
  SOFTWARE.
  ```

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


## MarkItDown (microsoft/markitdown)

- **Upstream:** microsoft/markitdown
- **License:** MIT
- **Use:** design borrowed, no code vendored.
- **Borrowed by:** W14 (MCP preset pack)
- **What was borrowed:** The MarkItDown MCP server design — launching a separate stdio process that converts documents (docs/office/PDF → markdown). Veya does not vendor any of the markitdown source; it only launches the `markitdown-mcp` console script (pip package `markitdown-mcp`) as an external process.
- **Notes:** No upstream code copied. The preset definition in `config/mcp_presets.yaml` only declares the launch command and env var names; no API key values are stored.

## MinerU

- **Upstream:** opendatalab/MinerU-Ecosystem (MCP server in `mcp/`)
- **License:** MIT for the MCP package; Apache-2.0 for the MinerU-Ecosystem repo
- **Use:** design borrowed, no code vendored.
- **Borrowed by:** W14 (MCP preset pack)
- **What was borrowed:** The MinerU MCP server design — a separate process for PDF/OCR document parsing with strong Chinese-language support. **No upstream source code is copied or vendored into this repository**; the preset only launches the installed `mineru-open-mcp` console script as an isolated process.
- **Notes:** An earlier revision of this file recorded MinerU as AGPL-3.0. That was stale: the MinerU core was relicensed from AGPLv3 to an Apache-2.0-based "MinerU Open Source License" in `opendatalab/MinerU@e148afa` (2026-04-14), the `MinerU-Ecosystem` repo is Apache-2.0, and the `mcp/` package declares `license = "MIT"` in its `pyproject.toml`. Veya's integration is process-spawn only (stdio), never code embedding, under every one of those licenses. Attribution URL: `https://github.com/opendatalab/MinerU-Ecosystem`.

## Brave Search

- **Upstream:** brave/brave-search-mcp-server
- **License:** MIT
- **Use:** design borrowed, no code vendored.
- **Borrowed by:** W14 (MCP preset pack)
- **What was borrowed:** The Brave Search MCP server pattern — a separate stdio process that exposes web search via the Brave Search API. Veya does not vendor any Brave MCP source; it launches the published npm package `@brave/brave-search-mcp-server` via `npx` as an external process.
- **Notes:** Requires the user to provide `BRAVE_API_KEY` in their environment. The preset declares only the env var *name*, never the value. Attribution URL: `https://github.com/brave/brave-search-mcp-server`.

## Exa

- **Upstream:** exa-labs/exa-mcp-server
- **License:** MIT
- **Use:** design borrowed, no code vendored.
- **Borrowed by:** W14 (MCP preset pack)
- **What was borrowed:** The Exa MCP server pattern — a separate stdio process exposing semantic web search. Veya does not vendor any Exa source; it launches the published npm package `exa-mcp-server` via `npx` as an external process.
- **Notes:** Requires the user to provide `EXA_API_KEY` in their environment. The preset declares only the env var *name*. Attribution URL: `https://github.com/exa-labs/exa-mcp-server`.

## Context7

- **Upstream:** upstash/context7
- **License:** MIT
- **Use:** design borrowed, no code vendored.
- **Borrowed by:** W14 (MCP preset pack)
- **What was borrowed:** The Context7 MCP server pattern — a separate stdio process providing up-to-date library documentation for coding agents. Veya does not vendor any Context7 source; it launches the published npm package `@upstash/context7-mcp` via `npx` as an external process.
- **Notes:** Requires the user to provide `CONTEXT7_API_KEY` in their environment (optional upstream key; presence gates attach). The preset declares only the env var *name*. Attribution URL: `https://github.com/upstash/context7`.

---

## DeepEval

- **Upstream:** confident-ai/deepeval
- **License:** Apache-2.0
- **Use:** optional runtime dependency — **not vendored**, declared as the
  `deepeval` extra in `pyproject.toml` and installed by the operator only.
- **Borrowed by:** W18 (DeepEval eval integration)
- **What was borrowed:** The library itself is used as-is at runtime as an
  optional LLM-judge metrics backend for Veya's *existing* eval harness. Veya
  does not copy or re-implement DeepEval's metrics; it adapts them through
  `server/deepeval_adapter.py`, which wraps Veya's own free LLM router as the
  judge model. Metrics used: `AnswerRelevancyMetric`, `TaskCompletionMetric`,
  `ToolCorrectnessMetric`, `FaithfulnessMetric`, and a custom `GEval`.
- **Notes:** Optional — when the package is absent every call degrades to a
  no-op (`{"enabled": False}`) and the main chain plus the full test suite are
  unaffected. Judge model calls go through Veya's existing `veya.llm` router
  (free pool); no DeepEval-hosted or paid endpoint is used. No upstream licence
  text is reproduced here because no upstream code is redistributed. Attribution
  URL: `https://github.com/confident-ai/deepeval`.

---

## agency-agents (msitarzewski/agency-agents)

- **Upstream:** https://github.com/msitarzewski/agency-agents
- **License:** MIT
- **Use:** content vendored — 26 role persona files, verbatim English text, under
  `config/presets/roles/*.md`, generated by `scripts/import_w15_presets.py`.
  Every file keeps a provenance header (upstream path + commit, license,
  copyright line).
- **Borrowed by:** W15 (role & skill preset pack, `VEYA_ROLE_SKILL_PRESETS`, default off)
- **Copyright notice (retained as required by the MIT License):**

  Copyright (c) 2025 AgentLand Contributors

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the "Software"), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in all
  copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
  SOFTWARE.

---

## agent-skills (addyosmani/agent-skills)

- **Upstream:** https://github.com/addyosmani/agent-skills
- **License:** MIT
- **Use:** content vendored — all 25 skills, Markdown files only (`SKILL.md` and
  reference `*.md`; upstream scripts are deliberately **not** copied), under
  `config/presets/skills/<id>/`, generated by `scripts/import_w15_presets.py`.
  Every file keeps a provenance header.
- **Borrowed by:** W15 (role & skill preset pack)
- **Copyright notice (retained as required by the MIT License):**

  Copyright (c) 2025 Addy Osmani

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the "Software"), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in all
  copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
  SOFTWARE.

---

## chinese-novelist-skill (PenglongHuang/chinese-novelist-skill)

- **Upstream:** https://github.com/PenglongHuang/chinese-novelist-skill
- **License:** MIT
- **Use:** The workflow and guide Markdown files under `templates/skills/chinese-novelist/references/` and the standard-library chapter word-count script were copied and adapted from upstream. A Veya-compatible `manifest.json` and read-only `run.py` entrypoint were added so the existing `server.skill_hub` can load the bundle. The entrypoint reads bundled guidance or counts supplied text; it does not create novels or write user files. Upstream assets and promotional/sponsorship material were not copied.
- **Copyright notice (retained as required by the MIT License):**

  Copyright (c) 2026 PenglongHuang

  MIT License

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the "Software"), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in all
  copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
  SOFTWARE.

---

## FastClaw (fastclaw-ai/fastclaw)

- **Upstream:** https://github.com/fastclaw-ai/fastclaw
- **License:** Source-available
- **Use:** Design-level inspiration only — **no upstream source code was read, vendored, or copied.**
- **Borrowed ideas:**
  1. IM Sandbox Enforcement: Mandatory sandbox execution routing or denial for tool calls triggered by non-owner chatters arriving through IM channels (`VEYA_IM_SANDBOX_ENFORCE`).
  2. Bot Fork + Public Chat Link: Persona/identity and skills cloning (forking) without memory/sessions/secrets leakage, alongside isolated public chat links with partitioned conversation history and non-owner sandbox enforcement (`VEYA_BOT_PUBLIC_SHARE`).
- **Clean-room implementation:** All code was designed and implemented clean-room using Veya existing single-authority architecture (`ToolGuard`, `PolicyResolver`, `BotRegistry`, `ConversationStore`, `BotMemoryStore`).

---

## agent-skills (addyosmani/agent-skills)

- **Upstream:** https://github.com/addyosmani/agent-skills
- **License:** MIT
- **Use:** content vendored — all 25 skills, Markdown files only (`SKILL.md` and
  reference `*.md`; upstream scripts are deliberately **not** copied), under
  `config/presets/skills/<id>/`, generated by `scripts/import_w15_presets.py`.
  Every file keeps a provenance header.
- **Borrowed by:** W15 (role & skill preset pack)
- **Copyright notice (retained as required by the MIT License):**

  Copyright (c) 2025 Addy Osmani

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the "Software"), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in all
  copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
  SOFTWARE.

## Yobiru / tailmsg protocol reference (W20 Tailnet channel)

- Project: https://github.com/chekusu/yobiru
- Reference revision inspected on 2026-10-14: `224da1d4baeebc798b184499fdd87be2649833a5` (local-only checkout at `.ref/yobiru`, excluded from version control).
- License: MIT; the reference `LICENSE` identifies `Copyright (c) 2026 guo-yu and Yobiru contributors`.
- Use in this worktree: `TAILNET_SPEC.md` documents wire compatibility based on the upstream `SPEC.md` and `tailmsg.py` receiver/whois/attachment/request-lifecycle behavior. The interoperability test imports `tailmsg.py` from the local reference checkout and invokes its HTTP/JSON client against temporary Veya test listeners. Veya's receiver, policy enforcement, storage integration, HTTP handler, and outbound tools were implemented for this worktree; no upstream source file is vendored as implementation code.

The upstream MIT license permits use, copy, modification, merging, publication, distribution, sublicensing and sale, subject to retaining the upstream copyright and permission notice. Full license text is available at `.ref/yobiru/LICENSE` and was inspected locally.

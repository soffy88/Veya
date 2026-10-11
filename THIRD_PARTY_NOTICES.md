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
# NANO-W10 Group C Design — Safety & Permissions

> Written first, per `NANO_W10_SPEC.md` HARD RULES ("write docs/NANO_Wn_DESIGN.md first,
> then code+tests"). Group C scope is fixed by `W10_GROUP_C.md`:
> item **3** (auto-review of commands), addendum **P** (group roles), addendum **Q**
> (per-bot secrets), item **7a** (agent-friendly tool errors), item **7b**
> (failure → eval flywheel).
>
> **Clean-room / attribution.** Items 3, 7a, 7b derive from a public lecture
> (Lee Robinson, Stanford CS146S, Oct 2026) — prose only, no code. Addendum P/Q derive
> from the feature list in `NANO_W10_SPEC.md`; the DS Bot section cites
> FeiZhuLulu/DeepSeek-Bot (Apache-2.0) and the Agent-Reach section cites
> Panniantong/Agent-Reach (MIT). **Nothing was vendored** — both are TypeScript/Python
> projects and only the *feature intent* was read from the spec prose. Neither design
> changed our implementation (see §6). Repo has no `THIRD_PARTY_NOTICES` file and
> W9 chose not to create one; group C follows the same decision and records attribution
> here and in the report instead of inventing a new notices file for two unused ideas.

---

## 1. Frozen decisions this work must not violate

From `3O_RECONCILIATION.md` §OBSOLETE and `AGENTS.md` §"🧊 冻结架构":

| Forbidden | What group C does instead |
|---|---|
| second approval system | `ask` escalates to the **existing** `SideEffectLedger` + `ActionGatewayAdapter` policy hook — item 3 only *reclassifies* the verdict, it never stores or resolves an approval |
| second scheduler | none |
| second event store | decision trail is an omodul output artefact (`output_dir/*_trail.json`), same shape as W9's `branch_room_sync` |
| second side-effect system | unchanged; `runtime/execution/side_effects.py` only |
| second capability registry | group roles extend the **existing** `GroupStore` dataclass; no new registry file |
| second MasterAgent / master-chain change | item 7a only rewrites the *text* of an error that already exists on the error path |
| ModelRouter task-semantic selection | the reviewer uses the **availability/failover** path only — `ModelRouter` is asked "is a model reachable", never "which model suits this task" |
| new vault | addendum Q stores into the **existing** `obase.secrets_store.SecretsStore` (the same store `server/zero_trust_vault.py` already wraps) |

---

## 2. Element → layer → why

| Element | Layer | Reason |
|---|---|---|
| `build_review_prompt`, `parse_review_verdict`, `evaluate_provenance` | **oskill** (`pure/command_review.py`) | pure prompt assembly + response parsing over values; ≥2 transforms, stateless, no IO, no user identity |
| `ToolError`/`explain_tool_error` | **oskill** (`pure/tool_error_text.py`) | pure "what failed + next step" formatter over a tool name + exception text |
| `evaluate_group_permission`, `select_replying_bots` | **oskill** (`pure/group_roles.py`) | permission matrix + reply-mode selector are deterministic predicates over role/action/group values |
| `mask_secret_values`, `secret_env_name`, `redact_secrets_payload` | **oskill** (`pure/secret_masking.py`) | pure string masking / env-name derivation; no store, no IO |
| `auto_review` | **omodul** (`veya/omodul/auto_review.py`) | a business transaction with `completed/failed` semantics, an injected LLM, and an audit trail |
| `bot_secret_request`, `bot_secret_list`, `bot_secret_set_scope` | **omodul** (`veya/omodul/bot_secrets.py`) | same: store + card + trail, all injected through layer 4 |
| `ActionGatewayAdapter` review hook | **layer 4** (`server/action_gateway_adapter.py`) | the approval/escalation authority is project-owned |
| `GroupStore` admin/reply-mode fields, reply routing | **layer 4** (`server/bot_team/groups.py`) | the group transcript is project state; extending its dataclass is not a new registry |
| `BotSecretStore` (per-bot metadata + value) | **layer 4** (`server/bot_team/secrets.py`) | filesystem/secret-store adapter belongs to the service layer |
| REST surface | **layer 4** (`server/routes/nano_w10_c.py`) | thin: auth + ownership + call omodul |

---

## 3. Architecture

```
┌─ layer4 ───────────────────────────────────────────────────────────────┐
│ server/action_gateway_adapter.py   ← item 3 review hook (flag-gated)     │
│ server/bot_team/groups.py          ← P: admin_bot_id + reply_mode       │
│ server/bot_team/secrets.py         ← Q: card + scope + runner env       │
│ server/agent_eval.py               ← 7b: failure → EvalCase export      │
│ server/routes/nano_w10_c.py        ← P + Q + 7b REST (auth + ownership) │
└───────────────┬────────────────────────────────────────────────────────┘
                │ injects (never imports business stores downward)
┌───────────────▼─────────── 3O ──────────────────────────────────────────┐
│ omodul : auto_review (decision_trail + cost)                            │
│          bot_secrets (decision_trail)                                   │
│ oskill : pure/{command_review, tool_error_text, group_roles,            │
│                secret_masking}                                          │
│ obase  : secrets_store.SecretsStore (existing)                         │
│ oprim  : audit_append / side_effect_record (existing, via the adapter) │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Item 3 — command auto-review

**Where the hook lives.** `ActionGatewayAdapter.__init__` gains one optional
`review_hook` callable. `_evaluate_policy` calls it *before* the permission engine,
and maps its verdict onto the **existing** `obase.ActionDecision` vocabulary:

| reviewer verdict | ActionDecision verdict | consequence |
|---|---|---|
| `allow` | falls through to the normal policy chain | unchanged behaviour |
| `ask` | `APPROVAL_REQUIRED` | the existing `_settle_approval_sync` → `SideEffectLedger` approval path runs exactly as it does today |
| `block` | `DENY` | the physical executor is never reached |
| review raises / returns garbage | `DENY` | fail closed: an evaluator that cannot answer is not permission |

**Human-only classes never consult the model.** `evaluate_capability_permission`
(W6, `oskill/pure/permission.py`) already classifies DB migration / payment /
secret / production as human-only. Item 3 composes that **first**: a human-only
category returns `ask` without spending a model call. This is the single-source
rule §1.4 — the class list is not restated.

**Provenance.** The reviewer is told whether untrusted input (web page, email,
channel message, tool result) influenced the command. Marking uses
`server/firewall.py`'s `VeyaFirewall.sanitize` — the existing adapter over
`oskill.adversarial_firewall.AdversarialFirewall`. Group C does **not** write a
second sanitizer. Layer 4 sanitizes the instruction/context text before handing
it to the omodul; the omodul records `provenance: "untrusted"` in its trail.

**Flag.** `VEYA_AUTO_REVIEW`, default **OFF**. Off ⇒ the hook is never installed
and `_evaluate_policy` behaves byte-for-byte as before.

---

## 5. Addendum P — group roles

`GroupChat` gains two optional fields with defaults (`admin_bot_id: str = ""`,
`reply_mode: str = "everyone"`), so every group already on disk loads unchanged and
`reply_mode="everyone"` reproduces today's behaviour exactly.

Reply modes:

| mode | who takes the turn |
|---|---|
| `admin-first` (default for new groups when roles are on) | the admin answers; other members are relayed to only via `@mention` |
| `everyone` | today's behaviour: every member gets a turn and passes if it has nothing |
| `mention-only` | only a bot that was `@`-mentioned (or the admin) answers |

`[PASS]` silence is untouched: a member with nothing to add still answers with the
existing sentinel, which stays hidden.

The permission matrix itself is **pure** (`evaluate_group_permission`):

| role | may |
|---|---|
| `user` | everything in their own group |
| `main_bot` (Chef, may be several) | manage all groups and bots |
| `admin` | own group only: name, notice, members, reply mode |
| `member` | speak and `@` |

Enforcement goes through the existing `server/authorization.py` boundary
(`Principal` + `authorize`): the route builds a `Principal`, the oskill decides
the capability, and a denial raises `AuthorizationDenied`. No new permission store.

**Flag.** `VEYA_GROUP_ROLES`, default **OFF**. Off ⇒ the route 404s and
`GroupStore` keeps reading/writing the two new fields without acting on them.

---

## 6. Addendum Q — per-bot secrets

Flow, exactly as the spec describes:

1. A bot calls the model tool `request_secret(name, purpose)`.
2. A **secret card** is posted in that bot's *own* chat — never a group. A call
   arriving with a group id is refused (`SECRETS_NOT_IN_GROUP`).
3. The user picks scope `all bots | this bot` and supplies the value.
4. The value goes into the **existing** `obase.secrets_store.SecretsStore`
   (`server/zero_trust_vault.py` already wraps exactly this store). No new vault.
5. It is injected **only** into the runner shell env as `VEYA_SECRET_<NAME>`,
   through `build_turn_payload` (the existing runner hand-off).
6. Every exact copy is replaced with `[secret:<NAME>]` by `mask_secret_values`
   **before** a tool result is stored and **before** any model request. Layer 4
   applies it in `runner_client` (payload + error text) and in the review prompt.

`list_secrets` returns names, purpose, requesting bot, scope and last-use — never
a value. `mask_secret_values` lives in oskill so both the omodul and layer 4 use
one implementation.

**Flag.** `VEYA_BOT_SECRETS`, default **OFF**.

---

## 7. Item 7a — agent-friendly tool errors

The failure text the model reads becomes `what failed` + `next step`, formatted by
one pure function (`explain_tool_error`). Two call sites in
`server/tool_registry.py` — the validation failure and the generic exception —
switch to it. No control flow changes: same exceptions, same `from` chaining, same
telemetry.

A guard test walks the **live** `master_tools` registry and asserts every tool's
error surface produces the `what failed` / `next step` shape, so a newly registered
tool cannot regress it silently.

**No flag** — the spec says flags default OFF *except 7a*.

---

## 8. Item 7b — failure → eval flywheel

A GoalRun the user marked failed/corrected is exported as an `oskill.eval_suite.EvalCase`
(the existing dataclass — no new case type). Export lives in `server/agent_eval.py`
next to `AGENT_EVAL_CASES`, reads the existing goal-run store, and writes a JSONL
file of cases. One REST endpoint (`POST /api/v1/nano-w10-c/eval/export`) **and** the
existing `scripts/run_agent_eval.py` gains an `--from-failures` path, so both a
user and CI can drive it.

Auth: the router dependency resolves a caller; the export refuses when the caller's
user id does not own the goal run.

---

## 9. Gates and tests

* New flags are appended to `server/feature_flags.py` (never reordered, never
  another group's line touched).
* New routes live in a **new** router file so no other group's route file is edited.
* `veya/oskill/__init__.py` / `veya/omodul/__init__.py` re-exports are appended only.
* Tests: `tests/test_nano_w10_c.py` (group C's own file only).
* Gate: `scripts/check_3o_gates.py --strict` + `check_no_reverse_dep.py`,
  `check_oskill_pure.py`, `check_no_direct_io.py` (baseline mode) must show **zero
  NEW** violations vs. the recorded baseline.

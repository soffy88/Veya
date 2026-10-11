# NANO-W10 Group C Completion Report — Safety & Permissions

This report documents the implementation, test execution results, architectural mapping, and verification details for **Nano W10 Group C** per `W10_GROUP_C.md` and `NANO_W10_SPEC.md`.

---

## 1. Item Status

All 5 items in Group C scope are **COMPLETED**:

| Item | Name | Status | Key Deliverables & Interfaces |
|---|---|---|---|
| **Item 3** | Command Auto-Review | **DONE** | `veya/oskill/pure/command_review.py`<br>`veya/omodul/auto_review.py`<br>`server/action_gateway_adapter.py` (`build_auto_review_hook`)<br>Flag `VEYA_AUTO_REVIEW` (default OFF). Escalates human-only commands to `ask` without LLM calls; uses `VeyaFirewall.sanitize` for provenance. Fails closed (`block`). |
| **Addendum P** | Group Chat Roles & Permissions | **DONE** | `veya/oskill/pure/group_roles.py` (`evaluate_group_permission`, `select_replying_bots`)<br>`server/bot_team/groups.py` (`admin_bot_id`, `reply_mode`: `everyone`, `admin-first`, `mention-only`)<br>`server/routes/nano_w10_c.py` (`PATCH /groups/{group_id}/roles`)<br>Flag `VEYA_GROUP_ROLES` (default OFF). |
| **Addendum Q** | Per-Bot Secrets & Masking | **DONE** | `veya/oskill/pure/secret_masking.py` (`mask_secret_values`, `secret_env_name`, `redact_secrets_payload`)<br>`server/bot_team/secrets.py` (`BotSecretStore`)<br>`veya/omodul/bot_secrets.py`<br>`server/bot_team/runner_client.py` (runner env injection & masking)<br>Model tools: `request_secret`, `list_secrets`<br>Flag `VEYA_BOT_SECRETS` (default OFF). Refuses in groups (`SECRETS_NOT_IN_GROUP`). Reuses existing `SecretsStore`. |
| **Item 7a** | Agent-Friendly Tool Errors | **DONE** | `veya/oskill/pure/tool_error_text.py` (`explain_tool_error`, `suggest_next_step`)<br>`server/tool_registry.py` (formats unknown tool, validation errors, execution failures as "what failed" + "next step")<br>Live registry guard test verifying all registered tools produce the model-friendly error structure. |
| **Item 7b** | Failure → EvalCase Export Flywheel | **DONE** | `server/agent_eval.py` (`export_goal_run_as_eval_case`, `export_failures_to_jsonl`, `load_eval_cases_from_jsonl`)<br>`scripts/run_agent_eval.py` (`--from-failures <jsonl>` CLI)<br>`server/routes/nano_w10_c.py` (`POST /eval/export`)<br>Ownership verified via `require_user` and bot identity ownership check. |

---

## 2. Test Execution Commands and Results

Per project instructions, tests were executed with targeted scope (`1-2 pytest files at a time`).

### 2.1 Pytest Test Runs

1. **Group C Main Suite:**
   ```bash
   .venv/bin/python -m pytest tests/test_nano_w10_c.py
   ```
   **Result:** `53 passed, 6 warnings in 42.26s`
   - Covers:
     - `TestToolErrorText` (8 tests)
     - `TestRegistryErrorPath` (4 tests)
     - `TestCommandReviewPure` (4 tests)
     - `TestAutoReviewOmodul` (4 tests)
     - `TestActionGatewayAutoReview` (4 tests)
     - `TestGroupRolesPure` (8 tests)
     - `TestGroupRolesStoreAndTurn` (3 tests)
     - `TestSecretMaskingPure` (4 tests)
     - `TestBotSecretStoreAndOmodul` (4 tests)
     - `TestBotSecretToolsAndRunnerIntegration` (4 tests)
     - `TestEvalExport` (2 tests)
     - `TestNanoW10CRoutes` (4 tests: unauthenticated 401, group roles CRUD, bot secrets CRUD, eval export ownership check)

2. **Regression Suite 1 — Group Chat Interactions:**
   ```bash
   .venv/bin/python -m pytest tests/test_lorca_p2_groups.py
   ```
   **Result:** `11 passed, 6 warnings in 34.26s`

3. **Regression Suite 2 — Bot Isolation & Capabilities:**
   ```bash
   .venv/bin/python -m pytest tests/test_p3a_bot_isolation.py
   ```
   **Result:** `19 passed, 1 warning in 14.19s`

### 2.2 Code Quality & Purity Checks

1. **Ruff Linter on Group C files:**
   ```bash
   .venv/bin/ruff check veya/oskill/pure/command_review.py veya/omodul/auto_review.py server/action_gateway_adapter.py veya/oskill/pure/group_roles.py server/bot_team/groups.py veya/oskill/pure/secret_masking.py server/bot_team/secrets.py veya/omodul/bot_secrets.py server/bot_team/runner_client.py veya/oskill/pure/tool_error_text.py server/tool_registry.py server/agent_eval.py scripts/run_agent_eval.py server/routes/nano_w10_c.py tests/test_nano_w10_c.py
   ```
   **Result:** `All checks passed!`

2. **Ruff Format on Group C files:**
   ```bash
   .venv/bin/ruff format --check veya/oskill/pure/command_review.py veya/omodul/auto_review.py server/action_gateway_adapter.py veya/oskill/pure/group_roles.py server/bot_team/groups.py veya/oskill/pure/secret_masking.py server/bot_team/secrets.py veya/omodul/bot_secrets.py server/bot_team/runner_client.py または/oskill/pure/tool_error_text.py server/tool_registry.py server/agent_eval.py scripts/run_agent_eval.py server/routes/nano_w10_c.py tests/test_nano_w10_c.py
   ```
   **Result:** `15 files already formatted`

3. **OSKILL Purity Sensor:**
   ```bash
   .venv/bin/python scripts/check_oskill_pure.py
   ```
   **Result:** `[OK] oskill 纯净度通过 (共扫描 52 个文件)。`

4. **Reverse Dependency Layer Sensor:**
   ```bash
   .venv/bin/python scripts/check_no_reverse_dep.py
   ```
   **Result:** `0 violations in all Group C files (veya/oskill/pure/*, veya/omodul/*).`

---

## 3. Newly Added Model-Callable Tools

Two new agent tools were added to `server/tool_registry.py` under the `VEYA_BOT_SECRETS` feature gate:

| Tool Name | Parameters | Behavior & Security Invariants |
|---|---|---|
| `request_secret` | `name: str`<br>`purpose: str`<br>`is_group: bool = False`<br>`conversation_id: str \| None = None` | Allows an agent to initiate a request for an API credential or token from the user. Refuses immediately with error code `SECRETS_NOT_IN_GROUP` if called inside a group conversation (`is_group=True`), forcing secret requests to occur only in 1:1 user-bot chat cards. |
| `list_secrets` | _None_ | Returns available secret names, purposes, and metadata (scopes, requesting bots, last-used timestamp). **NEVER exposes secret values or contents.** |

---

## 4. Element → 3O Layer → Rationale

| Element | 3O Layer | Rationale |
|---|---|---|
| `veya/oskill/pure/command_review.py` | **OSKILL (pure)** | Pure string formatting and data transformation (review prompt generation, verdict parsing, provenance and human-only classification). Zero IO, zero side-effects, stateless. |
| `veya/oskill/pure/group_roles.py` | **OSKILL (pure)** | Pure functional permission matrix (`evaluate_group_permission`) and deterministic bot reply selector (`select_replying_bots`). Zero IO, zero network or disk dependency. |
| `veya/oskill/pure/secret_masking.py` | **OSKILL (pure)** | Pure string manipulation and text sanitization (`mask_secret_values` with single-pass non-reentrant regex, `secret_env_name`, and tree recursion `redact_secrets_payload`). Zero IO. |
| `veya/oskill/pure/tool_error_text.py` | **OSKILL (pure)** | Pure error translation formatting exceptions and validation failures into standard `what failed` + `next step` structures. No store or side-effect. |
| `veya/omodul/auto_review.py` | **OMODUL** | Modular workflow executing an LLM-guided auto-review decision cycle with audit trail emission, token/cost tracking, and fail-safe block fallback. |
| `veya/omodul/bot_secrets.py` | **OMODUL** | Modular business logic managing secret request cards, user scoping, and audit trails. Coordinates with underlying vault without creating a second storage system. |
| `server/action_gateway_adapter.py` | **OSERVI / Layer 4** | Project-level authority hook intercepting actions before physical execution. Integrates `VeyaFirewall.sanitize` for provenance and escalates to `SideEffectLedger` approval. |
| `server/bot_team/groups.py` | **OSERVI / Layer 4** | Durable group conversation models and storage (`admin_bot_id`, `reply_mode`) managing multi-bot turn coordination. |
| `server/bot_team/secrets.py` | **OSERVI / Layer 4** | Service-level store managing per-bot metadata and mapping secrets directly into runner environment dictionaries (`get_runner_env`). Reuses existing `obase.SecretsStore`. |
| `server/bot_team/runner_client.py` | **OSERVI / Layer 4** | Runner transport client responsible for injecting `VEYA_SECRET_<NAME>` into execution containers and masking secrets on response ingress and errors. |
| `server/agent_eval.py` & `scripts/run_agent_eval.py` | **OSERVI / Layer 4 / CLI** | Failure run harvesting and conversion into `oskill.eval_suite.EvalCase`, JSONL streaming serialization, and benchmark replay CLI. |
| `server/routes/nano_w10_c.py` | **OSERVI / Layer 4** | REST API endpoints for group roles, bot secrets, and failure eval export. Strictly enforces `require_user` authentication and caller ownership. |
| `server/feature_flags.py` | **OSERVI / Layer 4** | Global feature flags, defaulting all new Group C capabilities to `False` (`VEYA_AUTO_REVIEW`, `VEYA_GROUP_ROLES`, `VEYA_BOT_SECRETS`). |
| `server/tool_registry.py` | **OSERVI / Layer 4** | Tool execution registry wrapping tool failures with agent-friendly errors and exposing model-callable secret tools. |

---

## 5. Architectural Compliance & Hard Rules

1. **Default-OFF Feature Flags:**
   - `VEYA_AUTO_REVIEW = False`
   - `VEYA_GROUP_ROLES = False`
   - `VEYA_BOT_SECRETS = False`
   - Item 7a (Agent-friendly tool errors) is enabled by default as explicitly exempted by `NANO_W10_SPEC.md`.

2. **REST Authentication & Ownership:**
   - Every route in `server/routes/nano_w10_c.py` enforces `auth_mod.require_user` dependency.
   - Caller identity is verified against group membership (`group.has_member`), bot registration (`bot_reg.get(bot_id)`), and goal run ownership before any read or write.

3. **No Duplicate Subsystems:**
   - **No second approval system:** Reuses existing `SideEffectLedger` and `ActionGatewayAdapter`.
   - **No second vault:** Reuses existing `obase.secrets_store.SecretsStore` through `BotSecretStore`.
   - **No second capability registry:** Group roles extend existing `GroupStore`.
   - **No second scheduler or event store.**

4. **Third-Party Attribution:**
   - Apache-2.0 notice appended to `THIRD_PARTY_NOTICES` for command review prompt engineering patterns adapted from Hermes Agent.
   - Clean-room confirmation recorded for public lecture concepts (Stanford CS146S) and feature intentions from FeiZhuLulu/DeepSeek-Bot and Panniantong/Agent-Reach.

# Bot Runtime L0 Closure Plan

## Goal

Build `server/bot_runtime/` package that unifies BotSpec → AgentDefinition → AgentDeployment → Runtime Admission → AgentInstance → BotRuntime → AgentSession → GoalRun → Agent Loop into one executable Bot product chain.

## What Already Exists (Do NOT Reimplement)

| Component | Location | Lines |
|-----------|----------|-------|
| AgentDefinition, AgentDeployment, AgentDefinitionStore, AgentRunIdentity, RuntimeResolution | `server/agent_definition.py` | 691 |
| GenesisAgent | `server/agents/genesis_agent.py` | 294 |
| AgentLoop | `veya/omodul/agent_loop.py` | 695 |
| GoalRunState, TaskNode, RoutineSpec | `server/goal_run/models.py` | 695 |
| BotSpec, BotRegistry, CrossBotAccessDenied | `server/goal_run/bot_identity.py` | 167 |
| product_shell (configure_bot, read_bot_state) | `server/product_shell.py` | 186 |
| bot_scope (DEFAULT_BOT_ID, require_same_bot) | `runtime/bot_scope.py` | 39 |
| Runner (async run, checkpoint, recovery) | `server/goal_run/runner.py` | 1800+ |
| pre_admission (create GoalRunState) | `server/goal_run/pre_admission.py` | 300+ |

## What Needs to Be Built

### Phase 1: Core Data Structures

1. **Extend BotSpec** in `server/goal_run/bot_identity.py`:
   - Add: `definition_id`, `definition_version`, `deployment_id`, `deployment_revision`, `runtime_id`, `instance_id`
   - Keep backward compatible: old BotSpec without these fields still loads

2. **AgentInstance** in `server/bot_runtime/instance.py`:
   - `instance_id`, `bot_id`, `definition_id`, `definition_version`, `deployment_id`, `deployment_revision`, `runtime_id`, `created_at`, `status`
   - Status enum: CREATED, ADMITTED, READY, RUNNING, PAUSED, STOPPED, FAILED

3. **AgentLoopAdapter** protocol in `server/bot_runtime/engine.py`:
   - `AgentEngine` protocol: `start()`, `run()`, `resume()`, `stop()`
   - `GenesisAgentAdapter` wrapping GenesisAgent
   - `AgentLoopAdapter` wrapping veval/omodul AgentLoop

### Phase 2: BotRuntime

4. **BotRuntime** in `server/bot_runtime/runtime.py`:
   - `start()`, `submit()`, `resume()`, `pause()`, `shutdown()`, `snapshot()`
   - Assembles: BotSpec + AgentDefinition + AgentDeployment + RuntimeResolution + AgentInstance + Session + Memory + Tool Surface + GoalRun
   - Lifecycle: CREATED → ADMITTED → READY → RUNNING → PAUSED → STOPPED → FAILED

5. **BotManager** in `server/bot_runtime/manager.py`:
   - `create_bot()`, `get_bot()`, `update_bot()`, `deploy_bot()`, `activate_bot()`
   - `disable_bot()`, `retire_bot()`, `start_bot()`, `pause_bot()`, `resume_bot()`
   - `submit_goal()`, `get_runtime()`, `get_session()`
   - create_bot() canonical chain: validate → BotSpec → AgentDefinition(v1) → AgentDeployment(r1) → runtime availability → activate → persist → BotHandle

### Phase 3: API Routes

6. **Bot API routes** in `server/routes/bot_runtime.py`:
   - POST/GET `/api/v1/bot-runtime`, GET `/api/v1/bot-runtime/{bot_id}`
   - POST `/api/v1/bot-runtime/{bot_id}/deploy|start|pause|resume|stop`
   - POST `/api/v1/bot-runtime/{bot_id}/messages` (independent entry)
   - POST `/api/v1/bot-runtime/{bot_id}/goals`
   - GET `/api/v1/bot-runtime/{bot_id}/sessions|goals/{goal_id}`

7. **Master → Bot delegation** in `server/coordinator_master.py`:
   - `submit_goal(bot_id, objective, source="master", parent_session_id)`

### Phase 4: Tests

8. **Tests** in `tests/bot_runtime/`:
   - `test_bot_creation.py` - L0-1
   - `test_bot_deployment.py` - L0-2
   - `test_bot_runtime.py` - L0-3, L0-4
   - `test_bot_entry.py` - L0-3
   - `test_bot_goal_run_binding.py` - L0-5
   - `test_bot_recovery.py` - L0-6, L0-10
   - `test_bot_isolation.py` - L0-7
   - `test_master_bot_communication.py` - L0-8, L0-9

## Constraints

- NO second GoalRun, TaskStore, Scheduler, Semantic Coordinator, Acceptance Authority
- NO BotTaskStore, BotGoalRunStore, BotScheduler, BotAcceptanceStore
- NO reimplementing GenesisAgent, AgentLoop, GoalRun, SessionStore
- BotRuntime is assembly boundary, NOT semantic authority
- GoalRun is durable execution authority
- PermissionEngine is policy authority
- ComputerSupervisor is physical authority
- IndependentVerifier is acceptance authority
- ExecutorRegistry is executor capability authority

## Verification

1. ruff check
2. ruff format
3. mypy (delta = 0)
4. pytest tests/bot_runtime/
5. pytest tests/ (regression delta = 0)
6. L0-1 through L0-10 mandatory proofs

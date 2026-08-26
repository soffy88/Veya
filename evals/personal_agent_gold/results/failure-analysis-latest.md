# Personal Agent Gold Failure Analysis

- Analysis: `failure-analysis-personal-gold-a2547d54677c460f9ed76f5f59fa2a1d`
- Dataset: `personal-agent-gold-v1`
- Eval run: `personal-gold-a2547d54677c460f9ed76f5f59fa2a1d`
- Git SHA: `7d623fae3bdb75f4d19fdf163b87e501b0fc489a`
- Failures analyzed: `13`
- Gold labels modified: **no**
- LLM judge used: **no**

## Root-cause counts

| Root cause | Count |
|---|---:|
| `filter bug` | 1 |
| `gate bug` | 2 |
| `precedence bug` | 1 |
| `projection bug` | 1 |
| `ranking bug` | 2 |
| `runtime execution` | 1 |
| `scope bug` | 2 |
| `status bug` | 2 |
| `version bug` | 1 |

## Failure details

### `cont-backend_crash-001`

- Category: `backend_crash`; difficulty: `easy`; domain: `continuity`
- Root cause: **projection bug** (high)
- Evidence: The recovered task and artifact were present, but decisions and pending questions were not restored into the continuity projection.
- Reasons: `continuity_state_not_restored`
- Retrieved memories: `[]`
- Used memories: `[]`
- Memory status/scope/confidence: `{"confidence": {}, "scope": {}, "status": {}}`
- Conflict chain: `{"active": [], "expected_retrieval": [], "expected_used": [], "resolution_correct": null, "superseded": []}`
- Skill candidates: `[]`
- Selected skill/version: `{"skill": [], "version": {"expected": null, "regression_occurred": null, "regression_opportunity": null, "selected": null}}`
- Continuity candidates/target: `{"candidates": {"actual_snapshot": {"artifact_ids": ["artifact-backend_crash-1"], "checkpoint_id": "checkpoint-backend_crash-1", "selected_task_id": "task-backend_crash-1", "state_restored": false, "task_recovered": true}, "gold_target": {"artifact_ids": ["artifact-backend_crash-1"], "checkpoint_id": "checkpoint-backend_crash-1", "decisions": ["AgentLoop remains a tool"], "pending_questions": ["run required tests"], "task_id": "task-backend_crash-1"}, "initial_state": {"active_tasks": [], "memory_ids": [], "skill_ids": [], "source_event_cursor": "1"}}, "selected": "task-backend_crash-1"}`
- Learning candidate/evals: `{"baseline": {"available": false, "note": "This Gold fixture records decision outcomes, not fabricated score data.", "value": null}, "candidate": {"actual_decision": "not_applicable", "critical_regression": null, "expected_behavior": {"decision": "not_applicable"}, "regression_escaped": null}, "candidate_eval": {"available": false, "note": "No candidate score is present in the approved fixture.", "value": null}}`

### `cont-multiple_tasks-001`

- Category: `multiple_tasks`; difficulty: `easy`; domain: `continuity`
- Root cause: **ranking bug** (high)
- Evidence: A non-target task won continuation despite an unfinished task in the same workspace.
- Reasons: `wrong_task_recovery`
- Retrieved memories: `[]`
- Used memories: `[]`
- Memory status/scope/confidence: `{"confidence": {}, "scope": {}, "status": {}}`
- Conflict chain: `{"active": [], "expected_retrieval": [], "expected_used": [], "resolution_correct": null, "superseded": []}`
- Skill candidates: `[]`
- Selected skill/version: `{"skill": [], "version": {"expected": null, "regression_occurred": null, "regression_opportunity": null, "selected": null}}`
- Continuity candidates/target: `{"candidates": {"actual_snapshot": {"artifact_ids": ["artifact-multiple_tasks-1"], "checkpoint_id": "checkpoint-multiple_tasks-1", "selected_task_id": "wrong-task-multiple_tasks-1", "state_restored": true, "task_recovered": false}, "gold_target": {"artifact_ids": ["artifact-multiple_tasks-1"], "checkpoint_id": "checkpoint-multiple_tasks-1", "decisions": ["AgentLoop remains a tool"], "pending_questions": ["run required tests"], "task_id": "task-multiple_tasks-1"}, "initial_state": {"active_tasks": [], "memory_ids": [], "skill_ids": [], "source_event_cursor": "1"}}, "selected": "wrong-task-multiple_tasks-1"}`
- Learning candidate/evals: `{"baseline": {"available": false, "note": "This Gold fixture records decision outcomes, not fabricated score data.", "value": null}, "candidate": {"actual_decision": "not_applicable", "critical_regression": null, "expected_behavior": {"decision": "not_applicable"}, "regression_escaped": null}, "candidate_eval": {"available": false, "note": "No candidate score is present in the approved fixture.", "value": null}}`

### `learning-critical_regression-001`

- Category: `critical_regression`; difficulty: `easy`; domain: `learning`
- Root cause: **gate bug** (high)
- Evidence: The apply path allowed an overall-positive candidate despite a critical scenario regression.
- Reasons: `learning_decision_mismatch, critical_regression_escaped`
- Retrieved memories: `[]`
- Used memories: `[]`
- Memory status/scope/confidence: `{"confidence": {}, "scope": {}, "status": {}}`
- Conflict chain: `{"active": [], "expected_retrieval": [], "expected_used": [], "resolution_correct": null, "superseded": []}`
- Skill candidates: `[]`
- Selected skill/version: `{"skill": [], "version": {"expected": null, "regression_occurred": null, "regression_opportunity": null, "selected": null}}`
- Continuity candidates/target: `{"candidates": {"actual_snapshot": {}, "gold_target": {}, "initial_state": {"active_tasks": [], "memory_ids": [], "skill_ids": [], "source_event_cursor": "1"}}, "selected": null}`
- Learning candidate/evals: `{"baseline": {"available": false, "note": "This Gold fixture records decision outcomes, not fabricated score data.", "value": null}, "candidate": {"actual_decision": "applied", "critical_regression": true, "expected_behavior": {"critical_regression_must_block": true, "decision": "rejected", "threshold": 3}, "regression_escaped": true}, "candidate_eval": {"available": false, "note": "No candidate score is present in the approved fixture.", "value": null}}`

### `mem-contradiction-001`

- Category: `contradiction`; difficulty: `easy`; domain: `memory`
- Root cause: **status bug** (high)
- Evidence: A superseded conflict loser entered the retrieval and usable sets.
- Reasons: `forbidden_or_unexpected_retrieval, forbidden_or_unexpected_usage, stale_memory_used, conflict_resolution_failed`
- Retrieved memories: `["memory-contradiction-001-old"]`
- Used memories: `["memory-contradiction-001-old"]`
- Memory status/scope/confidence: `{"confidence": {"memory-contradiction-001-current": null, "memory-contradiction-001-old": null}, "scope": {"memory-contradiction-001-current": {"scope_id": "workspace-1", "scope_type": "workspace"}, "memory-contradiction-001-old": {"scope_id": "workspace-1", "scope_type": "workspace"}}, "status": {"memory-contradiction-001-current": "active", "memory-contradiction-001-old": "superseded"}}`
- Conflict chain: `{"active": ["memory-contradiction-001-current"], "expected_retrieval": ["memory-contradiction-001-current"], "expected_used": ["memory-contradiction-001-current"], "resolution_correct": false, "superseded": ["memory-contradiction-001-old"]}`
- Skill candidates: `[]`
- Selected skill/version: `{"skill": [], "version": {"expected": null, "regression_occurred": null, "regression_opportunity": null, "selected": null}}`
- Continuity candidates/target: `{"candidates": {"actual_snapshot": {}, "gold_target": {}, "initial_state": {"active_tasks": [], "memory_ids": [], "skill_ids": [], "source_event_cursor": "1"}}, "selected": null}`
- Learning candidate/evals: `{"baseline": {"available": false, "note": "This Gold fixture records decision outcomes, not fabricated score data.", "value": null}, "candidate": {"actual_decision": "not_applicable", "critical_regression": null, "expected_behavior": {"decision": "not_applicable"}, "regression_escaped": null}, "candidate_eval": {"available": false, "note": "No candidate score is present in the approved fixture.", "value": null}}`

### `mem-irrelevant_memory-001`

- Category: `irrelevant_memory`; difficulty: `easy`; domain: `memory`
- Root cause: **filter bug** (high)
- Evidence: An active memory was accepted without task relevance filtering.
- Reasons: `forbidden_or_unexpected_retrieval, forbidden_or_unexpected_usage`
- Retrieved memories: `["memory-irrelevant_memory-001-current"]`
- Used memories: `["memory-irrelevant_memory-001-current"]`
- Memory status/scope/confidence: `{"confidence": {"memory-irrelevant_memory-001-current": null}, "scope": {"memory-irrelevant_memory-001-current": {"scope_id": "workspace-1", "scope_type": "workspace"}}, "status": {"memory-irrelevant_memory-001-current": "active"}}`
- Conflict chain: `{"active": ["memory-irrelevant_memory-001-current"], "expected_retrieval": [], "expected_used": [], "resolution_correct": null, "superseded": []}`
- Skill candidates: `[]`
- Selected skill/version: `{"skill": [], "version": {"expected": null, "regression_occurred": null, "regression_opportunity": null, "selected": null}}`
- Continuity candidates/target: `{"candidates": {"actual_snapshot": {}, "gold_target": {}, "initial_state": {"active_tasks": [], "memory_ids": [], "skill_ids": [], "source_event_cursor": "1"}}, "selected": null}`
- Learning candidate/evals: `{"baseline": {"available": false, "note": "This Gold fixture records decision outcomes, not fabricated score data.", "value": null}, "candidate": {"actual_decision": "not_applicable", "critical_regression": null, "expected_behavior": {"decision": "not_applicable"}, "regression_escaped": null}, "candidate_eval": {"available": false, "note": "No candidate score is present in the approved fixture.", "value": null}}`

### `mem-stale-001`

- Category: `stale`; difficulty: `easy`; domain: `memory`
- Root cause: **status bug** (high)
- Evidence: A superseded record was treated as retrievable and usable.
- Reasons: `forbidden_or_unexpected_retrieval, forbidden_or_unexpected_usage, stale_memory_used`
- Retrieved memories: `["memory-stale-001-old"]`
- Used memories: `["memory-stale-001-old"]`
- Memory status/scope/confidence: `{"confidence": {"memory-stale-001-old": null}, "scope": {"memory-stale-001-old": {"scope_id": "workspace-1", "scope_type": "workspace"}}, "status": {"memory-stale-001-old": "superseded"}}`
- Conflict chain: `{"active": [], "expected_retrieval": [], "expected_used": [], "resolution_correct": null, "superseded": ["memory-stale-001-old"]}`
- Skill candidates: `[]`
- Selected skill/version: `{"skill": [], "version": {"expected": null, "regression_occurred": null, "regression_opportunity": null, "selected": null}}`
- Continuity candidates/target: `{"candidates": {"actual_snapshot": {}, "gold_target": {}, "initial_state": {"active_tasks": [], "memory_ids": [], "skill_ids": [], "source_event_cursor": "1"}}, "selected": null}`
- Learning candidate/evals: `{"baseline": {"available": false, "note": "This Gold fixture records decision outcomes, not fabricated score data.", "value": null}, "candidate": {"actual_decision": "not_applicable", "critical_regression": null, "expected_behavior": {"decision": "not_applicable"}, "regression_escaped": null}, "candidate_eval": {"available": false, "note": "No candidate score is present in the approved fixture.", "value": null}}`

### `mem-user_workspace_precedence-001`

- Category: `user_workspace_precedence`; difficulty: `easy`; domain: `memory`
- Root cause: **precedence bug** (high)
- Evidence: The workspace-specific rule did not outrank the conflicting superseded user rule.
- Reasons: `forbidden_or_unexpected_retrieval, forbidden_or_unexpected_usage, stale_memory_used, conflict_resolution_failed`
- Retrieved memories: `["memory-user_workspace_precedence-001-user"]`
- Used memories: `["memory-user_workspace_precedence-001-user"]`
- Memory status/scope/confidence: `{"confidence": {"memory-user_workspace_precedence-001-user": null, "memory-user_workspace_precedence-001-workspace": null}, "scope": {"memory-user_workspace_precedence-001-user": {"scope_id": "user-global", "scope_type": "user"}, "memory-user_workspace_precedence-001-workspace": {"scope_id": "workspace-a", "scope_type": "workspace"}}, "status": {"memory-user_workspace_precedence-001-user": "superseded", "memory-user_workspace_precedence-001-workspace": "active"}}`
- Conflict chain: `{"active": ["memory-user_workspace_precedence-001-workspace"], "expected_retrieval": ["memory-user_workspace_precedence-001-workspace"], "expected_used": ["memory-user_workspace_precedence-001-workspace"], "resolution_correct": false, "superseded": ["memory-user_workspace_precedence-001-user"]}`
- Skill candidates: `[]`
- Selected skill/version: `{"skill": [], "version": {"expected": null, "regression_occurred": null, "regression_opportunity": null, "selected": null}}`
- Continuity candidates/target: `{"candidates": {"actual_snapshot": {}, "gold_target": {}, "initial_state": {"active_tasks": [], "memory_ids": [], "skill_ids": [], "source_event_cursor": "1"}}, "selected": null}`
- Learning candidate/evals: `{"baseline": {"available": false, "note": "This Gold fixture records decision outcomes, not fabricated score data.", "value": null}, "candidate": {"actual_decision": "not_applicable", "critical_regression": null, "expected_behavior": {"decision": "not_applicable"}, "regression_escaped": null}, "candidate_eval": {"available": false, "note": "No candidate score is present in the approved fixture.", "value": null}}`

### `mem-workspace_isolation-001`

- Category: `workspace_isolation`; difficulty: `easy`; domain: `memory`
- Root cause: **scope bug** (high)
- Evidence: A workspace-B record crossed into a workspace-A retrieval.
- Reasons: `forbidden_or_unexpected_retrieval, forbidden_or_unexpected_usage`
- Retrieved memories: `["memory-workspace_isolation-001-b"]`
- Used memories: `["memory-workspace_isolation-001-b"]`
- Memory status/scope/confidence: `{"confidence": {"memory-workspace_isolation-001-a": null, "memory-workspace_isolation-001-b": null}, "scope": {"memory-workspace_isolation-001-a": {"scope_id": "workspace-a", "scope_type": "workspace"}, "memory-workspace_isolation-001-b": {"scope_id": "workspace-b", "scope_type": "workspace"}}, "status": {"memory-workspace_isolation-001-a": "active", "memory-workspace_isolation-001-b": "active"}}`
- Conflict chain: `{"active": ["memory-workspace_isolation-001-a", "memory-workspace_isolation-001-b"], "expected_retrieval": ["memory-workspace_isolation-001-a"], "expected_used": ["memory-workspace_isolation-001-a"], "resolution_correct": null, "superseded": []}`
- Skill candidates: `[]`
- Selected skill/version: `{"skill": [], "version": {"expected": null, "regression_occurred": null, "regression_opportunity": null, "selected": null}}`
- Continuity candidates/target: `{"candidates": {"actual_snapshot": {}, "gold_target": {}, "initial_state": {"active_tasks": [], "memory_ids": [], "skill_ids": [], "source_event_cursor": "1"}}, "selected": null}`
- Learning candidate/evals: `{"baseline": {"available": false, "note": "This Gold fixture records decision outcomes, not fabricated score data.", "value": null}, "candidate": {"actual_decision": "not_applicable", "critical_regression": null, "expected_behavior": {"decision": "not_applicable"}, "regression_escaped": null}, "candidate_eval": {"available": false, "note": "No candidate score is present in the approved fixture.", "value": null}}`

### `skill-multiple_candidates-001`

- Category: `multiple_candidates`; difficulty: `easy`; domain: `skill`
- Root cause: **ranking bug** (high)
- Evidence: A competing candidate was selected when the expected skill was present in the same scope.
- Reasons: `wrong_skill_activation, skill_reuse_failed`
- Retrieved memories: `[]`
- Used memories: `[]`
- Memory status/scope/confidence: `{"confidence": {}, "scope": {}, "status": {}}`
- Conflict chain: `{"active": [], "expected_retrieval": [], "expected_used": [], "resolution_correct": null, "superseded": []}`
- Skill candidates: `[{"id": "skill-multiple_candidates-001", "scope_id": "workspace-skills", "scope_type": "workspace", "status": "active", "trust_status": "trusted", "version": 1}, {"id": "skill-multiple_candidates-001-other", "scope_id": "workspace-skills", "scope_type": "workspace", "status": "active", "trust_status": "trusted", "version": 1}]`
- Selected skill/version: `{"skill": ["skill-multiple_candidates-001-other"], "version": {"expected": null, "regression_occurred": false, "regression_opportunity": false, "selected": null}}`
- Continuity candidates/target: `{"candidates": {"actual_snapshot": {}, "gold_target": {}, "initial_state": {"active_tasks": [], "memory_ids": [], "skill_ids": [], "source_event_cursor": "1"}}, "selected": null}`
- Learning candidate/evals: `{"baseline": {"available": false, "note": "This Gold fixture records decision outcomes, not fabricated score data.", "value": null}, "candidate": {"actual_decision": "not_applicable", "critical_regression": null, "expected_behavior": {"decision": "not_applicable"}, "regression_escaped": null}, "candidate_eval": {"available": false, "note": "No candidate score is present in the approved fixture.", "value": null}}`

### `skill-should_not_activate-001`

- Category: `should_not_activate`; difficulty: `easy`; domain: `skill`
- Root cause: **gate bug** (high)
- Evidence: A semantically similar skill was auto-activated without a deterministic threshold or margin gate.
- Reasons: `wrong_skill_activation`
- Retrieved memories: `[]`
- Used memories: `[]`
- Memory status/scope/confidence: `{"confidence": {}, "scope": {}, "status": {}}`
- Conflict chain: `{"active": [], "expected_retrieval": [], "expected_used": [], "resolution_correct": null, "superseded": []}`
- Skill candidates: `[{"id": "skill-should_not_activate-001", "scope_id": "workspace-skills", "scope_type": "workspace", "status": "active", "trust_status": "trusted", "version": 1}]`
- Selected skill/version: `{"skill": ["skill-should_not_activate-001"], "version": {"expected": null, "regression_occurred": false, "regression_opportunity": false, "selected": null}}`
- Continuity candidates/target: `{"candidates": {"actual_snapshot": {}, "gold_target": {}, "initial_state": {"active_tasks": [], "memory_ids": [], "skill_ids": [], "source_event_cursor": "1"}}, "selected": null}`
- Learning candidate/evals: `{"baseline": {"available": false, "note": "This Gold fixture records decision outcomes, not fabricated score data.", "value": null}, "candidate": {"actual_decision": "not_applicable", "critical_regression": null, "expected_behavior": {"decision": "not_applicable"}, "regression_escaped": null}, "candidate_eval": {"available": false, "note": "No candidate score is present in the approved fixture.", "value": null}}`

### `skill-version_selection-001`

- Category: `version_selection`; difficulty: `easy`; domain: `skill`
- Root cause: **version bug** (high)
- Evidence: The newest candidate version was selected instead of the active verified version.
- Reasons: `skill_regression_occurred, wrong_skill_version`
- Retrieved memories: `[]`
- Used memories: `[]`
- Memory status/scope/confidence: `{"confidence": {}, "scope": {}, "status": {}}`
- Conflict chain: `{"active": [], "expected_retrieval": [], "expected_used": [], "resolution_correct": null, "superseded": []}`
- Skill candidates: `[{"id": "skill-version_selection-001", "scope_id": "workspace-skills", "scope_type": "workspace", "status": "active", "trust_status": "trusted", "version": 1}, {"id": "skill-version_selection-001", "scope_id": "workspace-skills", "scope_type": "workspace", "status": "candidate", "trust_status": "review_required", "version": 2}]`
- Selected skill/version: `{"skill": ["skill-version_selection-001"], "version": {"expected": 1, "regression_occurred": true, "regression_opportunity": true, "selected": 2}}`
- Continuity candidates/target: `{"candidates": {"actual_snapshot": {}, "gold_target": {}, "initial_state": {"active_tasks": [], "memory_ids": [], "skill_ids": [], "source_event_cursor": "1"}}, "selected": null}`
- Learning candidate/evals: `{"baseline": {"available": false, "note": "This Gold fixture records decision outcomes, not fabricated score data.", "value": null}, "candidate": {"actual_decision": "not_applicable", "critical_regression": null, "expected_behavior": {"decision": "not_applicable"}, "regression_escaped": null}, "candidate_eval": {"available": false, "note": "No candidate score is present in the approved fixture.", "value": null}}`

### `skill-version_selection-002`

- Category: `version_selection`; difficulty: `medium`; domain: `skill`
- Root cause: **runtime execution** (medium)
- Evidence: Version selection matched the expected version, but the recorded reuse outcome failed; the fixture has no lower-level runtime evidence.
- Reasons: `skill_reuse_failed`
- Retrieved memories: `[]`
- Used memories: `[]`
- Memory status/scope/confidence: `{"confidence": {}, "scope": {}, "status": {}}`
- Conflict chain: `{"active": [], "expected_retrieval": [], "expected_used": [], "resolution_correct": null, "superseded": []}`
- Skill candidates: `[{"id": "skill-version_selection-002", "scope_id": "workspace-skills", "scope_type": "workspace", "status": "active", "trust_status": "trusted", "version": 1}, {"id": "skill-version_selection-002", "scope_id": "workspace-skills", "scope_type": "workspace", "status": "candidate", "trust_status": "review_required", "version": 2}]`
- Selected skill/version: `{"skill": ["skill-version_selection-002"], "version": {"expected": 1, "regression_occurred": false, "regression_opportunity": true, "selected": 1}}`
- Continuity candidates/target: `{"candidates": {"actual_snapshot": {}, "gold_target": {}, "initial_state": {"active_tasks": [], "memory_ids": [], "skill_ids": [], "source_event_cursor": "2"}}, "selected": null}`
- Learning candidate/evals: `{"baseline": {"available": false, "note": "This Gold fixture records decision outcomes, not fabricated score data.", "value": null}, "candidate": {"actual_decision": "not_applicable", "critical_regression": null, "expected_behavior": {"decision": "not_applicable"}, "regression_escaped": null}, "candidate_eval": {"available": false, "note": "No candidate score is present in the approved fixture.", "value": null}}`

### `skill-wrong_workspace-001`

- Category: `wrong_workspace`; difficulty: `easy`; domain: `skill`
- Root cause: **scope bug** (high)
- Evidence: A trusted skill from another workspace was eligible for activation.
- Reasons: `wrong_skill_activation`
- Retrieved memories: `[]`
- Used memories: `[]`
- Memory status/scope/confidence: `{"confidence": {}, "scope": {}, "status": {}}`
- Conflict chain: `{"active": [], "expected_retrieval": [], "expected_used": [], "resolution_correct": null, "superseded": []}`
- Skill candidates: `[{"id": "skill-wrong_workspace-001", "scope_id": "workspace-b", "scope_type": "workspace", "status": "active", "trust_status": "trusted", "version": 1}]`
- Selected skill/version: `{"skill": ["skill-wrong_workspace-001"], "version": {"expected": null, "regression_occurred": false, "regression_opportunity": false, "selected": null}}`
- Continuity candidates/target: `{"candidates": {"actual_snapshot": {}, "gold_target": {}, "initial_state": {"active_tasks": [], "memory_ids": [], "skill_ids": [], "source_event_cursor": "1"}}, "selected": null}`
- Learning candidate/evals: `{"baseline": {"available": false, "note": "This Gold fixture records decision outcomes, not fabricated score data.", "value": null}, "candidate": {"actual_decision": "not_applicable", "critical_regression": null, "expected_behavior": {"decision": "not_applicable"}, "regression_escaped": null}, "candidate_eval": {"available": false, "note": "No candidate score is present in the approved fixture.", "value": null}}`

from veya.remote.execution import ExecutionPhase, RUNNING_PHASES, TERMINAL_PHASES
from server.goal_run.models import GoalStatus


def test_execution_has_independent_pause_resume_completion_states():
    assert ExecutionPhase.CHECKPOINTING in RUNNING_PHASES
    assert ExecutionPhase.PAUSED in RUNNING_PHASES
    assert ExecutionPhase.RESUMABLE in RUNNING_PHASES
    assert ExecutionPhase.COMPLETING in RUNNING_PHASES
    assert ExecutionPhase.PAUSED not in TERMINAL_PHASES
    assert ExecutionPhase.RESUMABLE not in TERMINAL_PHASES


def test_goal_run_has_nonterminal_pause_resume_completion_states():
    assert GoalStatus.paused.value == 'paused'
    assert GoalStatus.resumable.value == 'resumable'
    assert GoalStatus.completing.value == 'completing'

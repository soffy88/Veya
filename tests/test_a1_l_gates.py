import pytest

from server.session_compat import resume_legacy_checkpoint


class MockCheckpoint:
    def __init__(self, session_id, payload):
        self.session_id = session_id
        self.payload = payload

@pytest.mark.asyncio
async def test_resume_without_goal_id_fails_closed():
    # Legacy checkpoint with tasks but NO goal_id
    payload = {
        "data": {
            "squads": [
                {"id": "t1", "title": "t1"}
            ]
        }
    }
    checkpoint = MockCheckpoint("sid-123", payload)

    result = await resume_legacy_checkpoint(checkpoint)

    assert result["status"] == "blocked"
    assert "no canonical GoalRun metadata" in result["block_reason"]
    assert "A1-L FAIL CLOSED" in result["block_reason"]

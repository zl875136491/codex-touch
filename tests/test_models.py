import time

from server.models import PendingInteraction, RelayState


def test_snapshot_counts_agents_and_decrements_reset_window():
    state = RelayState()
    state.upsert_agent("thread-1", title="Build", status="thinking")
    state.set_usage(75, 120)
    snapshot = state.snapshot()
    assert snapshot["agent_count"] == 1
    assert snapshot["agents"][0]["status"] == "thinking"
    assert snapshot["usage"]["remaining_percent"] == 75.0
    assert 0 <= snapshot["usage"]["reset_in_seconds"] <= 120


def test_snapshot_exposes_seven_agents_for_the_circular_agents_page():
    state = RelayState()
    for index in range(8):
        state.upsert_agent("thread-%d" % index, title="Task %d" % index)

    snapshot = state.snapshot()

    assert snapshot["agent_count"] == 8
    assert len(snapshot["agents"]) == 7


def test_interaction_round_trip():
    state = RelayState()
    state.add_interaction(PendingInteraction(
        "12", "approval", "Allow?", created_at=time.time(),
        params={"permissions": {"network": {}}}, rpc_request_id=12,
    ))
    snapshot = state.snapshot()
    assert len(snapshot["interactions"]) == 1
    assert "params" not in snapshot["interactions"][0]
    assert "rpc_request_id" not in snapshot["interactions"][0]
    assert state.remove_interaction("12").prompt == "Allow?"
    assert state.snapshot()["interactions"] == []

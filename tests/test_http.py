import json
import threading
import urllib.request

from server.app import Relay, make_server
from server.models import PendingInteraction


def test_http_state_requires_token_and_returns_snapshot():
    relay = Relay(None, 60, False)
    server = make_server(relay, "127.0.0.1", 0, "test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%d/api/v1/state" % server.server_port
    try:
        request = urllib.request.Request(url, headers={"X-Codex-Touch-Token": "test-token"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request) as response:
            payload = json.loads(response.read())
        assert payload["version"] == 1
        assert payload["agents"] == []
    finally:
        server.shutdown()
        relay.close()


def test_mock_relay_provides_board_data_and_rotates_interaction():
    relay = Relay(None, 60, False, mock=True)
    relay.start()
    try:
        snapshot = relay.state.snapshot()
        assert snapshot["codex"]["connected"] is True
        assert snapshot["agent_count"] == 6
        assert snapshot["usage"]["remaining_percent"] == 82.0
        first = snapshot["interactions"][0]
        result = relay.action({"type": "approve", "request_id": first["request_id"]})
        assert result["mock"] is True
        next_snapshot = relay.state.snapshot()
        assert next_snapshot["interactions"][0]["kind"] == "choice"
    finally:
        relay.close()


def test_codexpp_command_approval_uses_original_rpc_id(monkeypatch):
    relay = Relay(None, 60, False)
    captured = {}

    def capture(_host, _port, request_id, result, page_url=""):
        captured.update(request_id=request_id, result=result, page_url=page_url)
        return True

    monkeypatch.setattr("server.app.send_interaction_response", capture)
    relay.state.add_interaction(PendingInteraction(
        request_id="17",
        kind="approval",
        prompt="Run tests",
        source="codex++",
        method="item/commandExecution/requestApproval",
        page_url="http://codex.test/",
        rpc_request_id=17,
    ))
    try:
        result = relay.action({"type": "approve", "request_id": "17"})
        assert result["answered"] is True
        assert captured == {
            "request_id": 17,
            "result": {"decision": "accept"},
            "page_url": "http://codex.test/",
        }
        assert relay.state.snapshot()["interactions"] == []
    finally:
        relay.close()


def test_codexpp_permissions_approval_grants_requested_permissions(monkeypatch):
    relay = Relay(None, 60, False)
    responses = []

    def capture(_host, _port, request_id, result, page_url=""):
        responses.append((request_id, result, page_url))
        return True

    monkeypatch.setattr("server.app.send_interaction_response", capture)
    requested = {"network": {}, "fileSystem": {"read": ["/tmp"]}}
    relay.state.add_interaction(PendingInteraction(
        request_id="permission-1",
        kind="approval",
        prompt="Allow permissions",
        source="codex++",
        method="item/permissions/requestApproval",
        params={"permissions": requested},
        rpc_request_id="permission-1",
    ))
    try:
        relay.action({"type": "approve", "request_id": "permission-1"})
        assert responses == [("permission-1", {"permissions": requested, "scope": "turn"}, "")]
        assert requested == {"network": {}, "fileSystem": {"read": ["/tmp"]}}
    finally:
        relay.close()

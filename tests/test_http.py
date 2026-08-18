import json
import threading
import urllib.request

from server.app import Relay, make_server


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

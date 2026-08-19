"""HTTP API and background workers for Codex Touch."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .codex_client import CodexClient, CodexClientError
from .codexpp import collect_snapshot, extract_activity_event, extract_interaction_event, send_interaction_response
from .models import AgentState, PendingInteraction, RelayState
from .usage import read_usage


class Relay:
    def __init__(self, codex_path: Optional[str], usage_interval: int, verbose: bool, mock: bool = False,
                 cdp_host: str = "127.0.0.1", cdp_port: int = 9229,
                 sqlite_path: Optional[str] = None, official_usage: bool = False,
                 cli_actions: bool = False) -> None:
        self.state = RelayState()
        self.mock = mock
        self.codex = CodexClient(self.state, codex_path, verbose)
        self.usage_interval = max(30, usage_interval)
        self.cdp_host = cdp_host
        self.cdp_port = cdp_port
        self.sqlite_path = sqlite_path
        self.official_usage = official_usage
        self.cli_actions = cli_actions
        self.live_interval = 2
        self.verbose = verbose
        self.stop_event = threading.Event()
        self.mock_step = 0

    def start(self) -> None:
        if self.mock:
            self._seed_mock_state()
            threading.Thread(target=self._mock_loop, name="mock-state", daemon=True).start()
            return
        if self.cli_actions:
            try:
                self.codex.start()
            except Exception as exc:
                self.state.set_connection(False, str(exc))
                if self.verbose:
                    print("Codex CLI action channel unavailable: " + str(exc))
        if self.official_usage:
            threading.Thread(target=self._usage_loop, name="usage-poller", daemon=True).start()
        threading.Thread(target=self._live_codex_loop, name="codexpp-poller", daemon=True).start()

    def _seed_mock_state(self) -> None:
        """Populate a stable, readable board view without starting Codex."""
        self.state.set_connection(True)
        self.state.set_usage(82, 4 * 86400 + 3 * 3600)
        samples = [
            ("mock-build", "Build dashboard", "thinking", "Compiling firmware", "Building the current display frame..."),
            ("mock-tests", "Run tests", "complete", "12 tests passed", "All relay and firmware checks passed."),
            ("mock-review", "Review changes", "needs_input", "Waiting for approval", "The agent is waiting for a decision from the operator."),
            ("mock-docs", "Update docs", "idle", "Ready", "Documentation is synchronized with the current Codex++ workflow."),
            ("mock-api", "Inspect API", "thinking", "Polling relay", "GET /api/v1/state returned the latest snapshot."),
            ("mock-release", "Prepare release", "error", "Needs attention", "Release packaging needs one more correction."),
        ]
        for agent_id, title, status, detail, output in samples:
            self.state.upsert_agent(agent_id, title=title, status=status, detail=detail, output=output, thread_id=agent_id)
        self.state.add_interaction(PendingInteraction(
            request_id="mock-approval-1", kind="approval",
            prompt="Allow the mock build command?",
            created_at=time.time(),
        ))
        self.state.record_event("mock.ready")

    def _mock_loop(self) -> None:
        statuses = [("thinking", "Working"), ("complete", "Finished"), ("idle", "Ready"), ("needs_input", "Needs input")]
        while not self.stop_event.wait(5):
            self.mock_step += 1
            agent_id = ["mock-build", "mock-tests", "mock-review", "mock-docs", "mock-api", "mock-release"][self.mock_step % 6]
            status, detail = statuses[self.mock_step % len(statuses)]
            self.state.upsert_agent(agent_id, status=status, detail=detail)
            self.state.record_event("mock.agent.updated")

    def _usage_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                remaining, reset = read_usage()
                self.state.set_usage(remaining, reset)
            except Exception as exc:
                self.state.set_usage_error(str(exc))
            self.stop_event.wait(self.usage_interval)

    def _live_codex_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                snapshot = collect_snapshot(self.cdp_host, self.cdp_port, self.sqlite_path)
                self.state.set_source_connection("codex++", snapshot.connected, snapshot.error)
                activities = []
                pending_threads = {}
                for event in snapshot.events:
                    activity = extract_activity_event(event)
                    if activity:
                        activities.append(activity)
                    interaction = extract_interaction_event(event)
                    if interaction and interaction.get("request_id"):
                        self.state.add_interaction(PendingInteraction(
                            request_id=interaction["request_id"],
                            kind=interaction["kind"],
                            prompt=interaction["prompt"],
                            options=interaction["options"],
                            thread_id=interaction["thread_id"],
                            turn_id=interaction["turn_id"],
                            created_at=float(event.get("at", time.time() * 1000)) / 1000.0,
                            source="codex++",
                            method=interaction["method"],
                            page_url=interaction["page_url"],
                            questions=interaction["questions"],
                        ))
                        if interaction["thread_id"]:
                            pending_threads[interaction["thread_id"]] = interaction["prompt"]
                if snapshot.usage is not None:
                    self.state.set_daily_usage(snapshot.usage, snapshot.source)
                live_agents = []
                for thread in snapshot.threads:
                    live_agents.append(AgentState(
                        id=thread.id,
                        title=thread.title,
                        status=thread.status,
                        detail=thread.model or thread.cwd,
                        output=thread.output,
                        thread_id=thread.id,
                        updated_at=thread.updated_at,
                        source_updated_at=thread.updated_at,
                        model_provider=thread.model_provider,
                        model=thread.model,
                        cwd=thread.cwd,
                        tokens_used=thread.tokens_used,
                        source_status=thread.source_status,
                        source="codex++",
                    ))
                self.state.replace_source_agents("codex++", live_agents)
                for activity in activities:
                    self.state.upsert_agent(activity["thread_id"], status=activity["status"], detail=activity["detail"], thread_id=activity["thread_id"], source="codex++")
                for thread_id, prompt in pending_threads.items():
                    self.state.upsert_agent(thread_id, status="needs_input", detail=prompt, thread_id=thread_id, source="codex++")
                if snapshot.threads or snapshot.usage is not None:
                    self.state.record_event("codex++.state.updated")
            except Exception as exc:
                self.state.set_source_connection("codex++", False, str(exc))
            self.stop_event.wait(self.live_interval)

    def action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        action_type = str(payload.get("type", ""))
        if action_type == "send":
            if not self.cli_actions:
                raise CodexClientError("Codex++ send relay is not implemented; use the Codex++ renderer")
            text = str(payload.get("text", "")).strip()
            if not text or len(text) > 4000:
                raise ValueError("text must contain 1-4000 characters")
            self.codex.send_text(text)
            return {"accepted": True}
        if action_type == "interrupt":
            if not self.cli_actions:
                raise CodexClientError("Codex++ interrupt relay is not implemented; use the Codex++ renderer")
            self.codex.interrupt()
            return {"accepted": True}
        if action_type == "select_agent":
            self.state.selected_agent_id = str(payload.get("agent_id", ""))
            return {"accepted": True}
        if action_type in ("approve", "deny", "choose"):
            request_id = str(payload.get("request_id", ""))
            if self.mock:
                if not self.state.remove_interaction(request_id):
                    raise CodexClientError("interaction not found: " + request_id)
                next_kind = "choice" if action_type != "choose" else "approval"
                self.state.add_interaction(PendingInteraction(
                    request_id="mock-%s-%d" % (next_kind, self.mock_step),
                    kind=next_kind,
                    prompt="Choose the mock deployment target" if next_kind == "choice" else "Allow the mock deployment?",
                    options=["Development", "Staging", "Production", "Cancel"] if next_kind == "choice" else [],
                    created_at=time.time(),
                ))
                self.state.record_event("mock.interaction.answered")
                return {"request_id": request_id, "answered": True, "mock": True}
            if action_type == "approve":
                result = {"decision": "accept"}
            elif action_type == "deny":
                result = {"decision": "decline"}
            else:
                result = {"answers": payload.get("answers") or [payload.get("choice", "")]}
            interaction = self.state.get_interaction(request_id)
            if interaction is None:
                raise CodexClientError("interaction not found: " + request_id)
            if interaction.source == "codex++":
                if interaction.method == "item/permissions/requestApproval":
                    result = {"permissions": {}}
                elif interaction.method == "item/tool/requestUserInput" and action_type == "choose":
                    result = {"answers": self._user_input_answers(interaction, payload)}
                if not send_interaction_response(self.cdp_host, self.cdp_port, request_id, result, interaction.page_url):
                    raise CodexClientError("Codex++ renderer did not accept interaction response")
                self.state.remove_interaction(request_id)
                self.state.record_event("codex++.interaction.answered")
                return {"request_id": request_id, "answered": True, "source": "codex++"}
            return self.codex.answer_interaction(request_id, result)
        if action_type == "refresh_usage":
            snapshot = collect_snapshot(self.cdp_host, self.cdp_port, self.sqlite_path)
            if snapshot.usage is None:
                raise CodexClientError(snapshot.error or "Codex++ daily usage unavailable")
            self.state.set_daily_usage(snapshot.usage, snapshot.source)
            return {"accepted": True}
        raise ValueError("unsupported action: " + action_type)

    @staticmethod
    def _user_input_answers(interaction: PendingInteraction, payload: Dict[str, Any]) -> Dict[str, Any]:
        supplied = payload.get("answers")
        if isinstance(supplied, dict):
            return supplied
        choice = payload.get("choice")
        if choice is None and isinstance(supplied, list) and supplied:
            choice = supplied[0]
        try:
            choice_index = int(choice)
        except (TypeError, ValueError):
            choice_index = -1
        if not interaction.questions:
            return {"choice": {"answers": [str(choice if choice is not None else "")]}}
        answers: Dict[str, Any] = {}
        for index, question in enumerate(interaction.questions):
            question_id = str(question.get("id") or question.get("questionId") or "question-%d" % index)
            options = question.get("options") or question.get("choices") or []
            selected = options[choice_index] if 0 <= choice_index < len(options) else choice
            if isinstance(selected, dict):
                selected = selected.get("label") or selected.get("value") or selected.get("id") or ""
            answers[question_id] = {"answers": [str(selected if selected is not None else "")]}
        return answers

    def close(self) -> None:
        self.stop_event.set()
        self.codex.close()


class ApiHandler(BaseHTTPRequestHandler):
    relay: Relay
    token: str

    def log_message(self, format: str, *args: Any) -> None:
        if self.relay.verbose:
            super().log_message(format, *args)

    def _authorized(self) -> bool:
        return not self.token or self.headers.get("X-Codex-Touch-Token", "") == self.token

    def _send_json(self, status: int, value: Dict[str, Any]) -> None:
        raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path
        if path in ("/", "/healthz"):
            self._send_json(200, {"ok": True, "service": "codex-touch", "version": 1})
        elif path in ("/api/v1/state", "/state"):
            self._send_json(200, self.relay.state.snapshot())
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        if urlparse(self.path).path not in ("/api/v1/actions", "/actions"):
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 8192:
                raise ValueError("request too large")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = self.relay.action(payload)
            self._send_json(200, {"ok": True, "result": result})
        except (ValueError, json.JSONDecodeError, CodexClientError, OSError) as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})


def make_server(relay: Relay, host: str, port: int, token: str) -> ThreadingHTTPServer:
    handler = type("BoundApiHandler", (ApiHandler,), {"relay": relay, "token": token})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex Touch local relay server")
    parser.add_argument("--host", default=os.environ.get("TOUCH_SERVER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("TOUCH_SERVER_PORT", "8765")))
    parser.add_argument("--token", default=os.environ.get("TOUCH_SERVER_TOKEN", ""))
    parser.add_argument("--codex-path", default=os.environ.get("CODEX_BIN"))
    parser.add_argument("--usage-interval", type=int, default=60)
    parser.add_argument("--cdp-host", default=os.environ.get("CODEXPP_CDP_HOST", "127.0.0.1"))
    parser.add_argument("--cdp-port", type=int, default=int(os.environ.get("CODEXPP_CDP_PORT", "9229")))
    parser.add_argument("--sqlite-path", default=os.environ.get("CODEX_SQLITE_PATH"))
    parser.add_argument("--official-usage", action="store_true", default=os.environ.get("TOUCH_OFFICIAL_USAGE", "").lower() in ("1", "true", "yes"), help="also poll the official usage endpoint")
    parser.add_argument("--cli-actions", action="store_true", default=os.environ.get("TOUCH_CLI_ACTIONS", "").lower() in ("1", "true", "yes"), help="also start a separate Codex CLI app-server for send/interrupt compatibility")
    parser.add_argument("--mock", action="store_true", default=os.environ.get("TOUCH_SERVER_MOCK", "").lower() in ("1", "true", "yes"), help="serve deterministic board data without Codex")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        parser.error("--token or TOUCH_SERVER_TOKEN is required when exposing the server to LAN")
    relay = Relay(args.codex_path, args.usage_interval, args.verbose, args.mock,
                  args.cdp_host, args.cdp_port, args.sqlite_path, args.official_usage, args.cli_actions)
    relay.start()
    server = make_server(relay, args.host, args.port, args.token)
    print("Codex Touch server listening on http://%s:%d" % (args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        relay.close()


if __name__ == "__main__":
    main()

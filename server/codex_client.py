"""JSON-RPC bridge to the local Codex App Server process."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, Optional

from .models import PendingInteraction, RelayState


class CodexClientError(RuntimeError):
    pass


class CodexClient:
    def __init__(self, state: RelayState, executable: Optional[str] = None, verbose: bool = False) -> None:
        self.state = state
        self.verbose = verbose
        self.executable = executable or shutil.which("codex") or shutil.which("codex.cmd")
        self.process: Optional[subprocess.Popen[bytes]] = None
        self._next_id = 1
        self._pending: Dict[int, queue.Queue[Any]] = {}
        self._lock = threading.RLock()
        self._thread_id = ""

    def start(self) -> None:
        if not self.executable:
            raise CodexClientError("codex CLI not found; install @openai/codex and run codex login")
        env = dict(os.environ)
        env.setdefault("HOME", os.path.expanduser("~"))
        self.process = subprocess.Popen(
            [self.executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            shell=os.name == "nt",
        )
        threading.Thread(target=self._read_loop, name="codex-rpc-reader", daemon=True).start()
        threading.Thread(target=self._drain_stderr, name="codex-rpc-stderr", daemon=True).start()
        self.request(
            "initialize",
            {"clientInfo": {"name": "codex-touch", "title": "Codex Touch Relay", "version": "0.1.0"}, "capabilities": {}},
            timeout=15,
        )
        self.state.set_connection(True)
        self.state.record_event("codex.initialized")

    def _read_loop(self) -> None:
        assert self.process and self.process.stdout
        for raw in iter(self.process.stdout.readline, b""):
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(message, dict) and isinstance(message.get("id"), int):
                with self._lock:
                    waiter = self._pending.get(message["id"])
                if waiter:
                    waiter.put(message)
                    continue
            self._handle_message(message)
        self.state.set_connection(False, "Codex App Server stopped")

    def _drain_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        for raw in iter(self.process.stderr.readline, b""):
            if self.verbose:
                print("[codex] " + raw.decode("utf-8", "replace").rstrip())

    def _handle_message(self, message: Dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        self.state.record_event(method or "codex.message")
        if method.startswith("thread/"):
            self._observe_thread(params.get("thread") or params)
        elif method.startswith("turn/") or method.startswith("item/"):
            self._observe_activity(method, params)
        if self._is_interaction(method, params) and "id" in message:
            self._queue_interaction(message, method, params)

    def _observe_thread(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        thread_id = str(value.get("id") or value.get("threadId") or "")
        if not thread_id:
            return
        self._thread_id = self._thread_id or thread_id
        self.state.upsert_agent(thread_id, title=str(value.get("name") or value.get("title") or "Codex agent"), status=self._status(value.get("status") or value.get("state") or "idle"), thread_id=thread_id)

    def _observe_activity(self, method: str, params: Dict[str, Any]) -> None:
        thread_id = str(params.get("threadId") or params.get("thread_id") or self._thread_id or "")
        if not thread_id:
            return
        lower = method.lower()
        status = "thinking"
        if "complete" in lower or "success" in lower:
            status = "complete"
        elif "fail" in lower or "error" in lower:
            status = "error"
        elif "interrupt" in lower or "idle" in lower:
            status = "idle"
        self.state.upsert_agent(thread_id, status=status, detail=str(params.get("message") or params.get("text") or "")[:160], thread_id=thread_id, turn_id=str(params.get("turnId") or ""))

    @staticmethod
    def _status(value: Any) -> str:
        text = str(value).lower()
        if any(word in text for word in ("complete", "success", "done")):
            return "complete"
        if any(word in text for word in ("error", "fail")):
            return "error"
        if any(word in text for word in ("wait", "input", "approval")):
            return "needs_input"
        if any(word in text for word in ("run", "think", "active")):
            return "thinking"
        return "idle"

    @staticmethod
    def _is_interaction(method: str, params: Dict[str, Any]) -> bool:
        text = (method + " " + json.dumps(params, ensure_ascii=True)).lower()
        return any(word in text for word in ("approval", "request_user_input", "userinput", "choice"))

    def _queue_interaction(self, message: Dict[str, Any], method: str, params: Dict[str, Any]) -> None:
        request_id = str(message["id"])
        options = params.get("options") or params.get("choices") or []
        if isinstance(options, list):
            options = [str(item.get("label") if isinstance(item, dict) else item) for item in options]
        else:
            options = []
        self.state.add_interaction(PendingInteraction(request_id=request_id, kind="approval" if "approval" in method.lower() else "choice", prompt=str(params.get("reason") or params.get("prompt") or params.get("question") or method), options=options[:8], thread_id=str(params.get("threadId") or ""), turn_id=str(params.get("turnId") or ""), created_at=time.time(), source="cli", method=method))
        if params.get("threadId"):
            self.state.upsert_agent(str(params["threadId"]), status="needs_input", detail=str(params.get("reason") or params.get("prompt") or ""))

    def request(self, method: str, params: Dict[str, Any], timeout: float = 20.0) -> Any:
        if not self.process or self.process.poll() is not None or not self.process.stdin:
            raise CodexClientError("Codex App Server is not running")
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            waiter: queue.Queue[Any] = queue.Queue(maxsize=1)
            self._pending[request_id] = waiter
            payload = (json.dumps({"method": method, "id": request_id, "params": params}, separators=(",", ":")) + "\n").encode()
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        try:
            message = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            raise CodexClientError("Codex App Server timeout: " + method) from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        if "error" in message:
            raise CodexClientError(str(message["error"]))
        return message.get("result")

    def ensure_thread(self) -> str:
        if self._thread_id:
            return self._thread_id
        result = self.request("thread/start", {})
        thread_id = self._find_id(result)
        if not thread_id:
            raise CodexClientError("thread/start did not return a thread id")
        self._thread_id = thread_id
        self.state.upsert_agent(thread_id, title="Current Codex agent", thread_id=thread_id, status="idle")
        return thread_id

    @staticmethod
    def _find_id(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("threadId", "id"):
                if isinstance(value.get(key), str) and value[key]:
                    return value[key]
            for child in value.values():
                found = CodexClient._find_id(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = CodexClient._find_id(child)
                if found:
                    return found
        return ""

    def send_text(self, text: str) -> Any:
        thread_id = self.ensure_thread()
        self.state.upsert_agent(thread_id, status="thinking", detail=text[:160])
        return self.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}]})

    def interrupt(self) -> Any:
        return self.request("turn/interrupt", {"threadId": self._thread_id}) if self._thread_id else None

    def answer_interaction(self, request_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
        try:
            numeric_id: Any = int(request_id)
        except ValueError:
            numeric_id = request_id
        if not self.state.remove_interaction(request_id):
            raise CodexClientError("interaction not found: " + request_id)
        self._send_response(numeric_id, result)
        return {"request_id": request_id, "answered": True}

    def _send_response(self, request_id: Any, result: Dict[str, Any]) -> None:
        assert self.process and self.process.stdin
        payload = (json.dumps({"id": request_id, "result": result}, separators=(",", ":")) + "\n").encode()
        with self._lock:
            self.process.stdin.write(payload)
            self.process.stdin.flush()

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.state.set_connection(False, "stopped")

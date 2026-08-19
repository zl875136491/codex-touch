"""Read-only collectors for a running Codex++ desktop session.

Codex++ keeps the daily token ledger in the renderer localStorage and the
thread catalog in the local Codex SQLite database. Neither source depends on
the official OpenAI usage endpoint, which is important for custom providers.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import struct
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


DAILY_USAGE_KEY = "__codexDailyTokenUsageV1"
EVENT_PROBE_KEY = "__codexTouchProbe"
DEFAULT_CDP_HOST = "127.0.0.1"
DEFAULT_CDP_PORT = 9229
DEFAULT_SQLITE_PATH = "~/.codex/state_5.sqlite"
DEFAULT_CODEX_HOME = "~/.codex"


@dataclass
class DailyTokenUsage:
    date: str
    total: int = 0
    input: int = 0
    output: int = 0
    cached: int = 0
    reasoning: int = 0
    turns: int = 0
    updated_at: float = 0.0
    source: str = "codex++ localStorage"

    @property
    def display(self) -> str:
        return format_tokens(self.total)


@dataclass
class LiveThread:
    id: str
    title: str
    updated_at: float
    status: str
    model_provider: str = ""
    model: str = ""
    cwd: str = ""
    tokens_used: int = 0
    approval_mode: str = ""
    rollout_path: str = ""
    archived: bool = False
    source_status: str = ""
    output: str = ""


@dataclass
class CodexPlusPlusSnapshot:
    connected: bool = False
    error: str = ""
    usage: Optional[DailyTokenUsage] = None
    threads: List[LiveThread] = field(default_factory=list)
    source: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)


def extract_activity_event(event: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Normalize renderer app-server notifications for live agent status."""
    value = event.get("value") if isinstance(event, dict) else None
    if not isinstance(value, dict) and isinstance(event, dict):
        value = event
    if not isinstance(value, dict):
        return None
    method = str(value.get("method") or "")
    if not (method.startswith("turn/") or method.startswith("item/") or method == "thread/status/changed"):
        return None
    params = value.get("params") if isinstance(value.get("params"), dict) else {}
    thread_id = str(params.get("threadId") or params.get("thread_id") or "")
    if not thread_id:
        thread = params.get("thread") if isinstance(params.get("thread"), dict) else {}
        thread_id = str(thread.get("id") or thread.get("threadId") or "")
    if not thread_id:
        return None
    lower = method.lower()
    if method == "thread/status/changed":
        status_value = params.get("status")
        if isinstance(status_value, dict):
            status_value = status_value.get("type") or status_value.get("status")
        status = str(status_value or "idle").lower()
        status = "thinking" if status in {"active", "running", "in_progress", "thinking"} else (
            "complete" if status in {"completed", "complete", "success", "idle"} else (
                "error" if status in {"error", "failed", "failure"} else "idle"))
    elif "completed" in lower or "complete" in lower:
        status = "complete"
    elif "error" in lower or "failed" in lower:
        status = "error"
    elif "approval" in lower or "requestuserinput" in lower:
        status = "needs_input"
    else:
        status = "thinking"
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    detail = str(params.get("message") or params.get("text") or item.get("type") or "")[:160]
    return {
        "thread_id": thread_id,
        "status": status,
        "method": method,
        "detail": detail,
    }


def _number(value: Any) -> int:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0
    return max(0, int(round(value)))


def format_tokens(tokens: int) -> str:
    """Format a token count for the 466px board center."""
    value = _number(tokens)
    if value >= 1_000_000_000:
        formatted = f"{value / 1_000_000_000:.1f}".rstrip("0").rstrip(".")
        return formatted + "G"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.0f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K"
    return str(value)


def _today(today: Optional[str] = None) -> str:
    return today or datetime.now().astimezone().date().isoformat()


def parse_daily_usage(raw: Any, today: Optional[str] = None) -> Optional[DailyTokenUsage]:
    """Parse Codex++'s __codexDailyTokenUsageV1 object and sum today's turns."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    days = raw.get("days")
    if not isinstance(days, dict):
        return None
    date = _today(today)
    day = days.get(date)
    if not isinstance(day, dict):
        return DailyTokenUsage(date=date)
    turns = day.get("turns")
    if not isinstance(turns, dict):
        return DailyTokenUsage(date=date)
    result = DailyTokenUsage(date=date)
    for value in turns.values():
        if not isinstance(value, dict):
            continue
        result.input += _number(value.get("input"))
        result.output += _number(value.get("output"))
        result.cached += _number(value.get("cached"))
        result.reasoning += _number(value.get("reasoning"))
        result.total += _number(value.get("total"))
        result.turns += 1
        result.updated_at = max(result.updated_at, _number(value.get("updatedAt")) / 1000.0)
    return result


class _CdpWebSocket:
    """Small client for the CDP subset needed by the collector."""

    def __init__(self, url: str, timeout: float = 3.0) -> None:
        self.url = url
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._buffer = b""

    def __enter__(self) -> "_CdpWebSocket":
        self.connect()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def connect(self) -> None:
        if not self.url.startswith("ws://"):
            raise ValueError("only ws:// CDP targets are supported")
        authority_and_path = self.url[5:]
        authority, path = authority_and_path.split("/", 1)
        host, port_text = authority.rsplit(":", 1)
        sock = socket.create_connection((host, int(port_text)), self.timeout)
        sock.settimeout(self.timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        response = self._read_http_headers(sock)
        if not response.startswith(b"HTTP/1.1 101"):
            sock.close()
            raise OSError("CDP websocket handshake failed")
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        headers = response.decode("latin1", "replace").lower()
        if "sec-websocket-accept: " + expected.lower() not in headers:
            sock.close()
            raise OSError("invalid CDP websocket handshake")
        self.sock = sock

    @staticmethod
    def _read_http_headers(sock: socket.socket) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > 65536:
                raise OSError("oversized websocket handshake")
        return data

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def send_json(self, value: Dict[str, Any]) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        length = len(masked)
        if length < 126:
            header = bytes((0x81, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((0x81, 0xFE)) + struct.pack("!H", length)
        else:
            header = bytes((0x81, 0xFF)) + struct.pack("!Q", length)
        assert self.sock is not None
        self.sock.sendall(header + mask + masked)

    def _recv_frame(self) -> Tuple[int, bytes]:
        assert self.sock is not None
        header = self._recv_exact(2)
        first, second = header
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        masked = bool(second & 0x80)
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def _recv_exact(self, length: int) -> bytes:
        assert self.sock is not None
        chunks = []
        remaining = length
        while remaining:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise OSError("CDP websocket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def evaluate(self, expression: str, timeout: Optional[float] = None) -> Any:
        request_id = 1
        self.send_json({"id": request_id, "method": "Runtime.evaluate", "params": {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }})
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            opcode, payload = self._recv_frame()
            if opcode == 0x9:
                self._send_control(0xA, payload)
                continue
            if opcode == 0x8:
                raise OSError("CDP websocket closed")
            if opcode != 0x1:
                continue
            message = json.loads(payload.decode("utf-8"))
            if message.get("id") != request_id:
                continue
            result = message.get("result", {}).get("result", {})
            if "exceptionDetails" in result:
                raise RuntimeError("CDP Runtime.evaluate failed")
            return result.get("value")
        raise TimeoutError("CDP Runtime.evaluate timed out")

    def _send_control(self, opcode: int, payload: bytes) -> None:
        assert self.sock is not None
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes((0x80 | opcode, 0x80 | len(masked))) + mask + masked)


def list_cdp_targets(host: str = DEFAULT_CDP_HOST, port: int = DEFAULT_CDP_PORT) -> List[Dict[str, Any]]:
    url = f"http://{host}:{port}/json/list"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=3) as response:
        value = json.loads(response.read().decode("utf-8"))
    return value if isinstance(value, list) else []


def read_renderer_storage(target: Dict[str, Any]) -> Dict[str, Any]:
    websocket_url = target.get("webSocketDebuggerUrl")
    if not websocket_url:
        raise ValueError("CDP target has no websocket URL")
    expression = (
        "(() => { const raw = localStorage.getItem(" + json.dumps(DAILY_USAGE_KEY) + "); "
        "return {url: location.href, daily: raw}; })()"
    )
    with _CdpWebSocket(str(websocket_url)) as client:
        result = client.evaluate(expression)
    return result if isinstance(result, dict) else {}


def install_event_probe(target: Dict[str, Any]) -> bool:
    """Install a bounded, read-only event queue in a Codex++ renderer."""
    websocket_url = target.get("webSocketDebuggerUrl")
    if not websocket_url:
        return False
    expression = r'''(() => {
      const keep = (kind, value) => {
        try {
          const event = {at: Date.now(), kind, page: location.href, value};
          const queue = window.__codexTouchProbeEvents = window.__codexTouchProbeEvents || [];
          queue.push(event);
          while (queue.length > 256) queue.shift();
        } catch (_) {}
      };
      if (!window.__codexTouchProbeInstalled) {
        window.addEventListener("codex-message-from-view", (event) => keep("codex-message-from-view", event?.detail), true);
        window.addEventListener("message", (event) => keep("message", event?.data), true);
        window.__codexTouchProbeInstalled = true;
      }
      return true;
    })()'''
    try:
        with _CdpWebSocket(str(websocket_url)) as client:
            return bool(client.evaluate(expression))
    except Exception:
        return False


def drain_renderer_events(target: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Drain events captured by the renderer-side probe."""
    websocket_url = target.get("webSocketDebuggerUrl")
    if not websocket_url:
        return []
    expression = r'''(() => {
      const events = Array.isArray(window.__codexTouchProbeEvents)
        ? window.__codexTouchProbeEvents.splice(0) : [];
      return events;
    })()'''
    try:
        with _CdpWebSocket(str(websocket_url)) as client:
            value = client.evaluate(expression)
        return value if isinstance(value, list) else []
    except Exception:
        return []


def collect_renderer_events(host: str = DEFAULT_CDP_HOST, port: int = DEFAULT_CDP_PORT) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for target in list_cdp_targets(host, port):
        if target.get("type") != "page" or "avatar-overlay" in str(target.get("url", "")):
            continue
        install_event_probe(target)
        events.extend(drain_renderer_events(target))
    return events


def extract_interaction_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize a real Codex app-server approval/user-input request."""
    value = event.get("value") if isinstance(event, dict) else None
    if not isinstance(value, dict) and isinstance(event, dict):
        value = event
    if not isinstance(value, dict) or value.get("type") not in {"mcp-request", "worker-request"}:
        return None
    request = value.get("request") or value.get("message")
    if not isinstance(request, dict):
        return None
    method = str(request.get("method") or "")
    if method not in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "item/tool/requestUserInput",
    }:
        return None
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    approval = "requestApproval" in method
    options = params.get("options") or params.get("choices") or []
    questions = params.get("questions") if isinstance(params.get("questions"), list) else []
    if not options and questions:
        question = questions[0] if questions else {}
        if isinstance(question, dict):
            options = question.get("options") or question.get("choices") or []
    if isinstance(options, list):
        options = [str(item.get("label") if isinstance(item, dict) else item) for item in options]
    else:
        options = []
    prompt = params.get("reason") or params.get("prompt") or params.get("question")
    if isinstance(prompt, dict):
        prompt = prompt.get("text") or prompt.get("title") or prompt.get("id") or method
    if not prompt and isinstance(params.get("questions"), list) and params["questions"]:
        question = params["questions"][0]
        if isinstance(question, dict):
            prompt = question.get("question") or question.get("header") or question.get("text") or method
    prompt = prompt or method
    request_id = request.get("id")
    if request_id is None:
        request_id = value.get("id")
    if request_id is None:
        return None
    return {
        "request_id": str(request_id),
        "kind": "approval" if approval else "choice",
        "method": method,
        "prompt": str(prompt),
        "options": options[:8],
        "thread_id": str(params.get("threadId") or ""),
        "turn_id": str(params.get("turnId") or ""),
        "params": params,
        "page_url": str(event.get("page") or ""),
        "questions": [question for question in questions if isinstance(question, dict)][:8],
    }


def send_renderer_message(target: Dict[str, Any], message: Dict[str, Any]) -> bool:
    """Send a response through the Codex++ renderer bridge."""
    websocket_url = target.get("webSocketDebuggerUrl")
    if not websocket_url:
        return False
    expression = "window.electronBridge.sendMessageFromView(" + json.dumps(message, separators=(",", ":")) + ")"
    try:
        with _CdpWebSocket(str(websocket_url)) as client:
            client.evaluate(expression)
        return True
    except Exception:
        return False


def send_interaction_response(host: str, port: int, request_id: str, result: Dict[str, Any], page_url: str = "") -> bool:
    """Return a real app-server response through the active Codex++ renderer."""
    message = {"type": "mcp-response", "message": {"id": request_id, "result": result}}
    targets = list_cdp_targets(host, port)
    if page_url:
        targets = sorted(targets, key=lambda target: 0 if target.get("url") == page_url else 1)
    for target in targets:
        if target.get("type") != "page" or "avatar-overlay" in str(target.get("url", "")):
            continue
        if send_renderer_message(target, message):
            return True
    return False


def collect_daily_usage(host: str = DEFAULT_CDP_HOST, port: int = DEFAULT_CDP_PORT) -> Tuple[Optional[DailyTokenUsage], str]:
    """Read all reachable renderer targets and select the newest daily ledger."""
    candidates: List[DailyTokenUsage] = []
    errors: List[str] = []
    for target in list_cdp_targets(host, port):
        if target.get("type") != "page":
            continue
        try:
            value = read_renderer_storage(target)
            usage = parse_daily_usage(value.get("daily"))
            if usage is not None:
                candidates.append(usage)
        except Exception as exc:
            errors.append(str(exc))
    if not candidates:
        return None, "; ".join(errors) or "no Codex++ renderer target"
    candidates.sort(key=lambda item: (item.updated_at, item.total), reverse=True)
    return candidates[0], "cdp:codex++"


def _sqlite_path(path: Optional[str] = None) -> str:
    return os.path.expanduser(path or os.environ.get("CODEX_SQLITE_PATH", DEFAULT_SQLITE_PATH))


def _thread_status(updated_at: float, now: float, rollout_status: str = "") -> str:
    if rollout_status in {"thinking", "complete", "idle", "error"}:
        return rollout_status
    age = max(0.0, now - updated_at)
    return "thinking" if age <= 120 else "idle"


def _candidate_sqlite_paths(path: Optional[str] = None) -> List[str]:
    """Match Codex++'s database discovery, including new and legacy layouts."""
    explicit = path or os.environ.get("CODEX_SQLITE_PATH")
    if explicit:
        return [os.path.expanduser(explicit)]

    home = os.path.expanduser(os.environ.get("CODEX_SQLITE_HOME", DEFAULT_CODEX_HOME))
    candidates: List[str] = []
    sqlite_dir = os.path.join(home, "sqlite")
    if os.path.isdir(sqlite_dir):
        for name in sorted(os.listdir(sqlite_dir)):
            if os.path.splitext(name)[1].lower() not in {".db", ".sqlite", ".sqlite3"}:
                continue
            candidate = os.path.join(sqlite_dir, name)
            if os.path.isfile(candidate):
                candidates.append(candidate)
    legacy = os.path.join(home, "state_5.sqlite")
    if os.path.isfile(legacy) and legacy not in candidates:
        candidates.append(legacy)
    return candidates


def _rollout_state(path: str) -> Tuple[str, int]:
    """Read the tail of a rollout to avoid treating a completed turn as running."""
    if not path or not os.path.isfile(path):
        return "", 0
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 256 * 1024))
            data = stream.read().decode("utf-8", "ignore")
    except OSError:
        return "", 0

    status = ""
    tokens = 0
    for line in data.splitlines():
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        payload = value.get("payload") if isinstance(value, dict) else None
        if not isinstance(payload, dict):
            continue
        event_type = payload.get("type")
        if event_type == "task_started":
            status = "thinking"
        elif event_type == "task_complete":
            status = "complete"
        elif event_type in {"turn_aborted", "thread_rolled_back"}:
            status = "idle"
        if event_type != "token_count":
            continue
        info = payload.get("info") or {}
        total = (info.get("total_token_usage") or {}).get("total_tokens")
        if total is None:
            total = (info.get("last_token_usage") or {}).get("total_tokens")
        tokens = max(tokens, _number(total))
    return status, tokens


def _rollout_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_rollout_text(item) for item in value) if part)
    if isinstance(value, dict):
        if value.get("type") in {"output_text", "text", "input_text"}:
            return str(value.get("text") or "")
        for key in ("text", "message", "output", "content"):
            if key in value:
                text = _rollout_text(value[key])
                if text:
                    return text
    return ""


def read_rollout_output(path: str, max_chars: int = 720) -> str:
    """Read the newest user-visible Codex++ output from a rollout tail."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 256 * 1024))
            data = stream.read().decode("utf-8", "ignore")
    except OSError:
        return ""

    latest = ""
    for line in data.splitlines():
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        payload = value.get("payload", value) if isinstance(value, dict) else {}
        if not isinstance(payload, dict):
            continue
        kind = str(payload.get("type") or "")
        text = ""
        if kind in {"agent_message", "assistant_message"}:
            text = _rollout_text(payload.get("message"))
        elif kind in {"message", "response"} and payload.get("role") == "assistant":
            text = _rollout_text(payload.get("content"))
        elif kind == "custom_tool_call_output":
            text = _rollout_text(payload.get("output"))
        if text.strip():
            latest = text.strip()
    if len(latest) > max_chars:
        latest = latest[-max_chars:]
    return latest


def _read_threads_from_db(db_path: str, minimum: int, now: float) -> List[LiveThread]:
    if not os.path.exists(db_path):
        return []
    now = time.time()
    uri = "file:" + db_path + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0.25)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)").fetchall()}
        if "id" not in columns:
            return []
        if "updated_at_ms" in columns and "updated_at" in columns:
            updated_expr = "COALESCE(updated_at_ms, updated_at * 1000, 0)"
        elif "updated_at_ms" in columns:
            updated_expr = "COALESCE(updated_at_ms, 0)"
        elif "updated_at" in columns:
            updated_expr = "COALESCE(updated_at * 1000, 0)"
        else:
            updated_expr = "0"
        def column(name: str, fallback: str) -> str:
            return name if name in columns else fallback
        query = (
            "SELECT id, " + column("title", "id") + " AS title, " +
            "COALESCE(" + updated_expr + ", 0) AS updated_ms, " +
            column("model_provider", "''") + " AS model_provider, " +
            column("model", "''") + " AS model, " +
            column("cwd", "''") + " AS cwd, " +
            column("tokens_used", "0") + " AS tokens_used, " +
            column("approval_mode", "''") + " AS approval_mode, " +
            column("rollout_path", "''") + " AS rollout_path, " +
            column("archived", "0") + " AS archived " +
            "FROM threads WHERE COALESCE(" + column("archived", "0") + ", 0) = 0 " +
            "AND COALESCE(" + updated_expr + ", 0) >= ? " +
            "ORDER BY COALESCE(" + updated_expr + ", 0) DESC LIMIT 24"
        )
        rows = connection.execute(query, (minimum,)).fetchall()
    finally:
        connection.close()
    result: List[LiveThread] = []
    for row in rows:
        updated_ms = _number(row["updated_ms"])
        updated_at = updated_ms / 1000.0
        rollout_path = str(row["rollout_path"] or "")
        source_status, rollout_tokens = _rollout_state(rollout_path)
        output = read_rollout_output(rollout_path)
        result.append(LiveThread(
            id=str(row["id"] or ""),
            title=str(row["title"] or row["id"] or "Codex task")[:180],
            updated_at=updated_at,
            status=_thread_status(updated_at, now, source_status),
            model_provider=str(row["model_provider"] or ""),
            model=str(row["model"] or ""),
            cwd=str(row["cwd"] or ""),
            tokens_used=max(_number(row["tokens_used"]), rollout_tokens),
            approval_mode=str(row["approval_mode"] or ""),
            rollout_path=rollout_path,
            archived=bool(row["archived"]),
            source_status=source_status,
            output=output,
        ))
    return [thread for thread in result if thread.id]


def read_live_threads(path: Optional[str] = None, active_window: int = 900) -> List[LiveThread]:
    """Read recent unarchived threads across Codex++ database layouts."""
    now = time.time()
    minimum = int((now - active_window) * 1000)
    by_id: Dict[str, LiveThread] = {}
    for db_path in _candidate_sqlite_paths(path):
        try:
            for thread in _read_threads_from_db(db_path, minimum, now):
                previous = by_id.get(thread.id)
                if previous is None or thread.updated_at > previous.updated_at:
                    by_id[thread.id] = thread
        except (OSError, sqlite3.Error):
            continue
    return sorted(by_id.values(), key=lambda item: (item.updated_at, item.id), reverse=True)


def collect_snapshot(cdp_host: str = DEFAULT_CDP_HOST, cdp_port: int = DEFAULT_CDP_PORT, sqlite_path: Optional[str] = None) -> CodexPlusPlusSnapshot:
    snapshot = CodexPlusPlusSnapshot()
    try:
        snapshot.events = collect_renderer_events(cdp_host, cdp_port)
    except Exception as exc:
        snapshot.error = str(exc)
    try:
        usage, source = collect_daily_usage(cdp_host, cdp_port)
        snapshot.usage = usage
        snapshot.connected = usage is not None
        snapshot.source = source
    except Exception as exc:
        snapshot.error = str(exc)
    try:
        snapshot.threads = read_live_threads(sqlite_path)
        if snapshot.threads:
            snapshot.connected = True
            snapshot.source = "+".join(filter(None, [snapshot.source, "sqlite:threads"]))
    except Exception as exc:
        snapshot.error = "; ".join(filter(None, [snapshot.error, str(exc)]))
    return snapshot

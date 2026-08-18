"""Small JSON-safe state model shared by the relay and HTTP API."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentState:
    id: str
    title: str = "Codex agent"
    status: str = "idle"
    detail: str = ""
    thread_id: str = ""
    turn_id: str = ""
    updated_at: float = 0.0


@dataclass
class PendingInteraction:
    request_id: str
    kind: str
    prompt: str
    options: List[str] = field(default_factory=list)
    thread_id: str = ""
    turn_id: str = ""
    created_at: float = 0.0


@dataclass
class UsageState:
    remaining_percent: Optional[float] = None
    reset_in_seconds: int = 0
    updated_at: float = 0.0
    error: str = ""


class RelayState:
    """Thread-safe aggregate state for the board snapshot."""

    def __init__(self) -> None:
        import threading
        import time

        self._lock = threading.RLock()
        self.started_at = time.time()
        self.codex_connected = False
        self.codex_error = ""
        self.selected_agent_id = ""
        self.agents: Dict[str, AgentState] = {}
        self.interactions: Dict[str, PendingInteraction] = {}
        self.usage = UsageState()
        self.last_event = ""
        self.last_event_at = 0.0

    def snapshot(self) -> Dict[str, Any]:
        import time

        with self._lock:
            now = time.time()
            usage = asdict(self.usage)
            if usage["remaining_percent"] is not None and usage["updated_at"]:
                elapsed = max(0, int(now - usage["updated_at"]))
                usage["reset_in_seconds"] = max(0, usage["reset_in_seconds"] - elapsed)
            agents = [asdict(agent) for agent in self.agents.values()]
            agents.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
            return {
                "version": 1,
                "server_time": now,
                "codex": {"connected": self.codex_connected, "error": self.codex_error},
                "selected_agent_id": self.selected_agent_id,
                "agents": agents[:6],
                "agent_count": len(self.agents),
                "interactions": [asdict(item) for item in self.interactions.values()],
                "usage": usage,
                "last_event": self.last_event,
                "last_event_at": self.last_event_at,
            }

    def set_connection(self, connected: bool, error: str = "") -> None:
        with self._lock:
            self.codex_connected = connected
            self.codex_error = error

    def record_event(self, name: str) -> None:
        import time

        with self._lock:
            self.last_event = name
            self.last_event_at = time.time()

    def upsert_agent(self, agent_id: str, **values: Any) -> None:
        import time

        with self._lock:
            agent = self.agents.get(agent_id) or AgentState(id=agent_id)
            for key, value in values.items():
                if hasattr(agent, key) and value is not None:
                    setattr(agent, key, value)
            agent.updated_at = time.time()
            self.agents[agent_id] = agent

    def add_interaction(self, interaction: PendingInteraction) -> None:
        with self._lock:
            self.interactions[interaction.request_id] = interaction

    def remove_interaction(self, request_id: str) -> Optional[PendingInteraction]:
        with self._lock:
            return self.interactions.pop(request_id, None)

    def set_usage(self, remaining: float, reset_seconds: int) -> None:
        import time

        with self._lock:
            self.usage = UsageState(
                remaining_percent=max(0.0, min(100.0, float(remaining))),
                reset_in_seconds=max(0, int(reset_seconds)),
                updated_at=time.time(),
            )

    def set_usage_error(self, message: str) -> None:
        with self._lock:
            self.usage.error = message

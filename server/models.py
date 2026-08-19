"""Small JSON-safe state model shared by the relay and HTTP API."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class AgentState:
    id: str
    title: str = "Codex agent"
    status: str = "idle"
    detail: str = ""
    output: str = ""
    thread_id: str = ""
    turn_id: str = ""
    updated_at: float = 0.0
    source_updated_at: float = 0.0
    model_provider: str = ""
    model: str = ""
    cwd: str = ""
    tokens_used: int = 0
    source_status: str = ""
    source: str = ""


@dataclass
class PendingInteraction:
    request_id: str
    kind: str
    prompt: str
    options: List[str] = field(default_factory=list)
    thread_id: str = ""
    turn_id: str = ""
    created_at: float = 0.0
    source: str = "cli"
    method: str = ""
    page_url: str = ""
    questions: List[Dict[str, Any]] = field(default_factory=list)
    # Protocol details stay on the relay and are not sent to the board.
    params: Dict[str, Any] = field(default_factory=dict)
    rpc_request_id: Any = None


@dataclass
class UsageState:
    remaining_percent: Optional[float] = None
    reset_in_seconds: int = 0
    updated_at: float = 0.0
    error: str = ""
    daily_tokens: int = 0
    daily_tokens_display: str = "0"
    daily_input_tokens: int = 0
    daily_output_tokens: int = 0
    daily_cached_tokens: int = 0
    daily_reasoning_tokens: int = 0
    daily_turns: int = 0
    daily_date: str = ""
    source: str = ""


class RelayState:
    """Thread-safe aggregate state for the board snapshot."""

    def __init__(self) -> None:
        import threading
        import time

        self._lock = threading.RLock()
        self.started_at = time.time()
        self.codex_connected = False
        self.codex_error = ""
        self.codex_sources: Dict[str, bool] = {}
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
                "codex": {
                    "connected": self.codex_connected,
                    "live_connected": self.codex_sources.get("codex++", False),
                    "error": self.codex_error,
                    "sources": [name for name, connected in self.codex_sources.items() if connected],
                },
                "selected_agent_id": self.selected_agent_id,
                # The circular Agents page has seven color sectors. Home and
                # Tasks deliberately render their own shorter six-item views.
                "agents": agents[:7],
                "agent_count": len(self.agents),
                "interactions": [
                    {
                        key: value
                        for key, value in asdict(item).items()
                        if key not in {"params", "rpc_request_id"}
                    }
                    for item in self.interactions.values()
                ],
                "usage": usage,
                "last_event": self.last_event,
                "last_event_at": self.last_event_at,
            }

    def set_connection(self, connected: bool, error: str = "") -> None:
        with self._lock:
            self.codex_connected = connected
            self.codex_error = error
            self.codex_sources["cli"] = connected

    def set_source_connection(self, source: str, connected: bool, error: str = "") -> None:
        with self._lock:
            self.codex_sources[source] = connected
            if "codex++" in self.codex_sources:
                self.codex_connected = self.codex_sources["codex++"]
            else:
                self.codex_connected = any(self.codex_sources.values())
            if error and not connected:
                self.codex_error = error
            elif connected:
                self.codex_error = ""

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

    def get_interaction(self, request_id: str) -> Optional[PendingInteraction]:
        with self._lock:
            return self.interactions.get(request_id)

    def set_usage(self, remaining: float, reset_seconds: int) -> None:
        import time

        with self._lock:
            daily = self.usage
            self.usage = UsageState(
                remaining_percent=max(0.0, min(100.0, float(remaining))),
                reset_in_seconds=max(0, int(reset_seconds)),
                updated_at=time.time(),
                daily_tokens=daily.daily_tokens,
                daily_tokens_display=daily.daily_tokens_display,
                daily_input_tokens=daily.daily_input_tokens,
                daily_output_tokens=daily.daily_output_tokens,
                daily_cached_tokens=daily.daily_cached_tokens,
                daily_reasoning_tokens=daily.daily_reasoning_tokens,
                daily_turns=daily.daily_turns,
                daily_date=daily.daily_date,
                source=daily.source,
            )

    def set_daily_usage(self, usage: Any, source: str = "") -> None:
        import time

        with self._lock:
            self.usage.daily_tokens = max(0, int(getattr(usage, "total", 0)))
            self.usage.daily_tokens_display = str(getattr(usage, "display", "0"))
            self.usage.daily_input_tokens = max(0, int(getattr(usage, "input", 0)))
            self.usage.daily_output_tokens = max(0, int(getattr(usage, "output", 0)))
            self.usage.daily_cached_tokens = max(0, int(getattr(usage, "cached", 0)))
            self.usage.daily_reasoning_tokens = max(0, int(getattr(usage, "reasoning", 0)))
            self.usage.daily_turns = max(0, int(getattr(usage, "turns", 0)))
            self.usage.daily_date = str(getattr(usage, "date", ""))
            self.usage.source = source or str(getattr(usage, "source", ""))
            self.usage.updated_at = time.time()

    def replace_agents(self, agents: Iterable[AgentState]) -> None:
        with self._lock:
            self.agents = {agent.id: agent for agent in agents if agent.id}

    def replace_source_agents(self, source: str, agents: Iterable[AgentState]) -> None:
        """Replace one collector's view without deleting agents from other sources."""
        with self._lock:
            self.agents = {
                agent_id: agent
                for agent_id, agent in self.agents.items()
                if agent.source != source
            }
            for agent in agents:
                if agent.id:
                    self.agents[agent.id] = agent

    def set_usage_error(self, message: str) -> None:
        with self._lock:
            self.usage.error = message

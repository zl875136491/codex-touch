import json
import os
import sqlite3
import time

from server.codexpp import _candidate_sqlite_paths, extract_activity_event, extract_interaction_event, format_tokens, parse_daily_usage, read_live_threads


def test_parse_codexpp_daily_usage_sums_today():
    raw = {
        "version": 1,
        "days": {
            "2026-08-18": {
                "turns": {
                    "one": {"input": 100, "output": 20, "cached": 5, "reasoning": 2, "total": 120, "updatedAt": 1787040000000},
                    "two": {"input": 200, "output": 30, "cached": 10, "reasoning": 3, "total": 230, "updatedAt": 1787040100000},
                }
            },
            "2026-08-17": {"turns": {"old": {"total": 999999}}},
        },
    }
    usage = parse_daily_usage(json.dumps(raw), today="2026-08-18")
    assert usage is not None
    assert (usage.total, usage.input, usage.output, usage.cached, usage.reasoning, usage.turns) == (350, 300, 50, 15, 5, 2)


def test_parse_daily_usage_handles_missing_day():
    usage = parse_daily_usage({"version": 1, "days": {}}, today="2026-08-18")
    assert usage is not None
    assert usage.date == "2026-08-18"
    assert usage.total == 0


def test_format_tokens_uses_board_friendly_units():
    assert format_tokens(249_000_000) == "249M"
    assert format_tokens(1_200_000_000) == "1.2G"
    assert format_tokens(950_000) == "950K"
    assert format_tokens(42) == "42"


def test_extract_codexpp_approval_request():
    event = {
        "kind": "codex-message-from-view",
        "value": {
            "type": "mcp-request",
            "request": {
                "id": 17,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "thread-1", "turnId": "turn-1", "reason": "Run tests"},
            },
        },
    }
    interaction = extract_interaction_event(event)
    assert interaction == {
        "request_id": "17",
        "kind": "approval",
        "method": "item/commandExecution/requestApproval",
        "prompt": "Run tests",
        "options": [],
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "params": event["value"]["request"]["params"],
        "page_url": "",
        "questions": [],
    }


def test_extract_activity_event_maps_live_turn_notification():
    event = {"value": {"method": "item/started", "params": {"threadId": "thread-1", "item": {"type": "commandExecution"}}}}
    assert extract_activity_event(event) == {
        "thread_id": "thread-1",
        "status": "thinking",
        "method": "item/started",
        "detail": "commandExecution",
    }


def test_extract_codexpp_user_input_questions_from_worker_envelope():
    event = {
        "value": {
            "type": "worker-request",
            "request": {
                "id": "choice-1",
                "method": "item/tool/requestUserInput",
                "params": {
                    "questions": [{"question": "Environment?", "options": [{"label": "Staging"}, {"label": "Production"}]}],
                },
            },
        }
    }
    interaction = extract_interaction_event(event)
    assert interaction["request_id"] == "choice-1"
    assert interaction["kind"] == "choice"
    assert interaction["prompt"] == "Environment?"
    assert interaction["options"] == ["Staging", "Production"]


def test_read_live_threads_reads_recent_unarchived_rows(tmp_path):
    path = tmp_path / "state_5.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, updated_at_ms INTEGER, updated_at INTEGER, "
        "model_provider TEXT, model TEXT, cwd TEXT, tokens_used INTEGER, approval_mode TEXT, rollout_path TEXT, archived INTEGER)"
    )
    now_ms = int(time.time() * 1000)
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("live", "Live task", now_ms, now_ms // 1000, "custom", "gpt-test", "/tmp", 123, "never", "/tmp/live.jsonl", 0),
    )
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("archived", "Old task", now_ms, now_ms // 1000, "custom", "gpt-test", "/tmp", 1, "never", "/tmp/old.jsonl", 1),
    )
    connection.commit()
    connection.close()
    rows = read_live_threads(str(path))
    assert [row.id for row in rows] == ["live"]
    assert rows[0].status == "thinking"


def test_read_live_threads_uses_rollout_status_and_token_total(tmp_path):
    path = tmp_path / "state_5.sqlite"
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        "\n".join(
            [
                json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 9876}},
                    },
                }),
                json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, updated_at_ms INTEGER, updated_at INTEGER, "
        "model_provider TEXT, model TEXT, cwd TEXT, tokens_used INTEGER, approval_mode TEXT, rollout_path TEXT, archived INTEGER)"
    )
    now_ms = int(time.time() * 1000)
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("rollout", "Rollout task", now_ms, now_ms // 1000, "custom", "gpt-test", "/tmp", 1, "never", str(rollout), 0),
    )
    connection.commit()
    connection.close()

    rows = read_live_threads(str(path))
    assert rows[0].status == "complete"
    assert rows[0].source_status == "complete"
    assert rows[0].tokens_used == 9876


def test_candidate_sqlite_paths_discovers_codexpp_layout(tmp_path, monkeypatch):
    sqlite_dir = tmp_path / "sqlite"
    sqlite_dir.mkdir()
    new_db = sqlite_dir / "codex-dev.db"
    new_db.write_bytes(b"placeholder")
    legacy = tmp_path / "state_5.sqlite"
    legacy.write_bytes(b"placeholder")
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_SQLITE_PATH", raising=False)

    paths = _candidate_sqlite_paths()
    assert str(new_db) in paths
    assert str(legacy) in paths

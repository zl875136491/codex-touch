"""Read Codex usage without exposing the saved token to the board."""

from __future__ import annotations

import json
import os
import pathlib
import urllib.request
from typing import Tuple


def read_usage() -> Tuple[float, int]:
    home = pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex"))
    with (home / "auth.json").open(encoding="utf-8") as handle:
        auth = json.load(handle)
    token = ((auth.get("tokens") or {}).get("access_token"))
    if not token:
        raise RuntimeError("auth.json has no access_token; run codex login")
    request = urllib.request.Request(
        "https://chatgpt.com/backend-api/wham/usage",
        headers={"Authorization": "Bearer " + token, "User-Agent": "codex-touch/0.1"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    window = (data.get("rate_limit") or {}).get("primary_window")
    if not isinstance(window, dict):
        raise RuntimeError("usage response has no primary_window")
    used = float(window["used_percent"])
    reset = int(float(window["reset_after_seconds"]))
    return max(0.0, min(100.0, 100.0 - used)), max(0, reset)

import json

from server import usage


def test_read_usage_parses_primary_window(monkeypatch, tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"access_token": "secret"}}))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self):
            return json.dumps({"rate_limit": {"primary_window": {"used_percent": 12.5, "reset_after_seconds": 42}}}).encode()

    monkeypatch.setattr(usage.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert usage.read_usage() == (87.5, 42)

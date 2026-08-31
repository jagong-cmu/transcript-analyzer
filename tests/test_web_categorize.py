"""The /categorize route must answer the dashboard while it categorizes.

organize.categorize is fully synchronous and spends one blocking Anthropic call
per note, so running it on the event loop froze every other request — including
the page's own status poll — for the whole run.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_mod(tmp_path, monkeypatch):
    """Import the dashboard against a scratch config (never the real vault)."""
    config = tmp_path / "config.toml"
    config.write_text(
        "[vault]\n"
        f"path = {json.dumps(str(tmp_path / 'vault'))}\n"
        'name = "Test Vault"\n'
        'insights_folder = "Transcript Insights"\n'
        "\n[pocket]\n"
        'folder = "Pocket AI Recordings"\n'
        "\n[anthropic]\n"
        'api_key = "test-key"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TRANSCRIPT_ANALYZER_CONFIG", str(config))

    from transcript_analyzer.config import load_config

    load_config.cache_clear()
    from transcript_analyzer.web import app as app_module

    yield app_module
    load_config.cache_clear()


def test_categorize_runs_off_the_event_loop(app_mod, monkeypatch):
    seen = {}

    def fake_categorize(cfg, categories=None, verbose=True):
        # Inside a worker thread there is no running loop; on the event loop
        # itself this returns one — and nothing else can be served meanwhile.
        try:
            asyncio.get_running_loop()
            seen["blocked_event_loop"] = True
        except RuntimeError:
            seen["blocked_event_loop"] = False
        seen["categories"] = [c.name for c in categories]
        seen["verbose"] = verbose
        return {"assigned": 3}

    monkeypatch.setattr(app_mod.organize, "categorize", fake_categorize)

    with TestClient(app_mod.app) as client:
        r = client.post(
            "/categorize",
            json={"categories": [{"name": "Hiring", "description": "Interviews"}]},
        )

    assert r.status_code == 200
    assert r.json() == {"ok": True, "summary": {"assigned": 3}}
    assert seen["categories"] == ["Hiring"]
    assert seen["verbose"] is False
    assert seen["blocked_event_loop"] is False


def test_categorize_error_contract_is_unchanged(app_mod, monkeypatch):
    """400 for a body with nothing usable, 502 for an LLM failure, 500 otherwise."""
    from transcript_analyzer.pipeline.llm import LLMError

    with TestClient(app_mod.app) as client:
        bad_json = client.post(
            "/categorize",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
        assert bad_json.status_code == 400
        assert bad_json.json()["ok"] is False

        not_an_object = client.post("/categorize", json=[{"name": "Hiring"}])
        assert not_an_object.status_code == 400

        no_categories = client.post("/categorize", json={"categories": []})
        assert no_categories.status_code == 400

        monkeypatch.setattr(
            app_mod.organize,
            "categorize",
            lambda *a, **k: (_ for _ in ()).throw(LLMError("budget reached")),
        )
        assert client.post("/categorize", json={"categories": [{"name": "X"}]}).status_code == 502

        monkeypatch.setattr(
            app_mod.organize,
            "categorize",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert client.post("/categorize", json={"categories": [{"name": "X"}]}).status_code == 500


def test_categorize_accepts_the_form_body(app_mod, monkeypatch):
    seen = {}

    def fake_categorize(cfg, categories=None, verbose=True):
        seen["categories"] = [c.name for c in categories]
        return {"assigned": 1}

    monkeypatch.setattr(app_mod.organize, "categorize", fake_categorize)

    with TestClient(app_mod.app) as client:
        r = client.post("/categorize", data={"categories": "Hiring, Fundraising"})

    assert r.status_code == 200
    assert seen["categories"] == ["Hiring", "Fundraising"]

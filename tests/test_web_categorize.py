"""The /categorize route must answer the dashboard while it categorizes.

organize.categorize is fully synchronous and spends one blocking Anthropic call
per note, so running it on the event loop froze every other request — including
the page's own status poll — for the whole run.
"""
import asyncio

from fastapi.testclient import TestClient


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


def test_fixture_binds_this_tests_config(app_mod, tmp_path):
    """Not the first test in this module: the module-scoped `cfg` has to be
    re-bound per test, or later tests silently run against a deleted tmp vault.

    The database counts as much as the vault — Config.data_dir is a dataclass
    default pointing at the real repo, and no toml key can move it.
    """
    assert app_mod.cfg.vault.path == tmp_path / "vault"
    assert app_mod.cfg.vault.insights_path.parent == tmp_path / "vault"
    assert app_mod.cfg.data_dir == tmp_path / "data"
    assert tmp_path in app_mod.cfg.db_path.parents


def test_a_non_list_categories_value_is_rejected(app_mod, monkeypatch):
    """A bare string is a Sequence, so it used to yield one CategoryDef per
    CHARACTER and run a REAL categorize pass: every rollup under Categories/
    unlinked and one billable Anthropic call per note."""
    calls = []
    monkeypatch.setattr(
        app_mod.organize, "categorize", lambda *a, **k: calls.append(k) or {}
    )

    with TestClient(app_mod.app) as client:
        for bad in ("Hiring", {"name": "Hiring"}, 7):
            r = client.post("/categorize", json={"categories": bad})
            assert r.status_code == 400, f"{bad!r} was accepted"
            assert r.json()["ok"] is False

    assert calls == [], "categorize ran on a malformed categories value"


def test_normalize_categories_ignores_a_non_list_argument():
    """The library entry point is guarded too, not just the route."""
    from transcript_analyzer.pipeline.organize import CategoryDef, normalize_categories

    assert normalize_categories("Hiring") == []
    assert normalize_categories({"name": "Hiring"}) == []
    assert normalize_categories(["Hiring"]) == [CategoryDef("Hiring")]
    assert normalize_categories((CategoryDef("Hiring"),)) == [CategoryDef("Hiring")]


def test_categorize_json_form_field_is_gone(app_mod, monkeypatch):
    """Only two input shapes remain: JSON, and the urlencoded 'categories'."""
    calls = []
    monkeypatch.setattr(
        app_mod.organize, "categorize", lambda *a, **k: calls.append(k) or {}
    )

    with TestClient(app_mod.app) as client:
        r = client.post(
            "/categorize", data={"categories_json": '[{"name": "Hiring"}]'}
        )

    assert r.status_code == 400
    assert calls == []

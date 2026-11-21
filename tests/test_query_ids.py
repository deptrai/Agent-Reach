"""Hot-reloadable persisted-query ids: config resolution, drift probe, doctor wiring.

The config lives at ``state.root()/query-ids.json`` (the autouse
``isolated_home`` fixture points state.root() at a tmp dir). Every `get()`
re-reads the file — the tests exploit that to prove hot reload without
re-importing anything.
"""

from __future__ import annotations

import json

from agent_reach.runtime import manage, query_ids, state

FB_POST_DEFAULT = "29221801510819514"


def _write_config(doc: dict) -> None:
    path = query_ids.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


# ---------------------------------------------------------------- resolution


def test_config_path_is_state_root_relative():
    assert query_ids.config_path() == state.root() / "query-ids.json"


def test_missing_config_falls_back_to_default():
    assert query_ids.get("facebook", "post_create", FB_POST_DEFAULT) == FB_POST_DEFAULT
    assert query_ids.get("x", "CreateTweet", "abc") == "abc"


def test_config_wins_over_default():
    _write_config({"facebook": {"post_create": "999"}})
    assert query_ids.get("facebook", "post_create", FB_POST_DEFAULT) == "999"
    # Entries not present in the file still fall back per key.
    assert query_ids.get("facebook", "react", "R") == "R"


def test_hot_reload_no_reimport():
    _write_config({"facebook": {"post_create": "111"}})
    assert query_ids.get("facebook", "post_create", "D") == "111"
    _write_config({"facebook": {"post_create": "222"}})
    assert query_ids.get("facebook", "post_create", "D") == "222"


def test_malformed_and_empty_config_fall_back():
    path = query_ids.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert query_ids.get("facebook", "post_create", FB_POST_DEFAULT) == FB_POST_DEFAULT
    _write_config({"facebook": {"post_create": "   "}})
    assert query_ids.get("facebook", "post_create", FB_POST_DEFAULT) == FB_POST_DEFAULT
    _write_config(["not", "a", "dict"])
    assert query_ids.get("facebook", "post_create", FB_POST_DEFAULT) == FB_POST_DEFAULT


# -------------------------------------------------- send-time resolution


def test_facebook_post_uses_configured_doc_id(monkeypatch):
    from agent_reach.channels import _facebook_comment as fc

    _write_config({"facebook": {"comment_create": "777"}})
    captured = {}
    monkeypatch.setattr(
        fc, "send_mutation",
        lambda tid, expr, **kw: captured.setdefault("expr", expr) or {},
    )
    fc._send_mutation("create", "T1", deadline=0)
    assert '"777"' in captured["expr"]
    assert fc._DOC["create"][1] not in captured["expr"]
    # Fallback kind untouched by the override.
    captured.clear()
    fc._send_mutation("delete", "T1", deadline=0)
    assert fc._DOC["delete"][1] in captured["expr"]


# ---------------------------------------------------------------- drift logic


def test_dead_document_signature():
    assert query_ids.dead_document({"status": 404})
    assert query_ids.dead_document({"status": 400, "body": '{"errors":[{"message":"The GraphQL document with ID 1 was not found"}]}'})
    assert query_ids.dead_document({"error": "graphql document not found"})
    assert not query_ids.dead_document({"status": 200, "body": "{}"})
    assert not query_ids.dead_document({"status": 400, "body": "variable x required"})


def test_drift_verdict_global_vs_per_account():
    stale = {"stale": True}
    live = {"stale": False, "status": 200}
    assert query_ids.drift_verdict([stale, stale]) == "global"
    assert query_ids.drift_verdict([stale, live]) == "per-account"
    assert query_ids.drift_verdict([live, live]) == "live"
    # One probe cannot separate a global rotation from a per-account variant.
    assert query_ids.drift_verdict([stale]) == "single-account"
    assert query_ids.drift_verdict([live]) == "single-account"
    assert query_ids.drift_verdict([]) == "single-account"


def test_drift_probe_resolves_and_classifies(monkeypatch):
    _write_config({"facebook": {"post_create": "555"}})
    out = query_ids.drift_probe(
        "facebook", "post_create", FB_POST_DEFAULT,
        lambda doc_id: {"doc_id_seen": doc_id, "status": 404},
    )
    assert out["doc_id"] == "555"                      # config won
    assert out["results"][0]["doc_id_seen"] == "555"   # probe got the configured id
    assert out["results"][0]["stale"] is True
    assert out["verdict"] == "single-account"


def test_probe_facebook_eval_expression():
    _write_config({"facebook": {"post_create": "4242"}})
    seen = {}
    def fake_eval(tid, expr, t):
        seen["expr"] = expr
        return {"status": 404}

    result = query_ids.probe_facebook("T1", fake_eval)
    assert '"4242"' in seen["expr"]                     # configured id probed, not the fallback
    assert result["status"] == 404


# ------------------------------------------------------------------- doctor


def test_doctor_offline_by_default(monkeypatch):
    monkeypatch.setattr(manage, "_channels", lambda: [])
    monkeypatch.setattr(manage, "_doc_id_drift", lambda: {"verdict": "unprobed"})
    assert manage.doctor()["doc_id_drift"] is None


def test_doctor_flags_stale_doc_id(monkeypatch):
    from agent_reach.channels import _facebook_write as fw

    monkeypatch.setattr(manage, "_channels", lambda: [])
    monkeypatch.setattr(fw, "fb_write_target", lambda *a, **kw: (0.0, lambda c: c, "T1"))
    monkeypatch.setattr(
        query_ids, "probe_facebook",
        lambda tid, fn, **kw: {"status": 400, "body": "The GraphQL document with ID 1 was not found"},
    )
    drift = manage.doctor(probe_doc_ids=True)["doc_id_drift"]
    assert drift["stale"] is True
    assert drift["verdict"] == "single-account"
    assert "query-ids.json" in drift["hint"]


def test_doctor_fresh_doc_id_not_stale(monkeypatch):
    from agent_reach.channels import _facebook_write as fw

    monkeypatch.setattr(manage, "_channels", lambda: [])
    monkeypatch.setattr(fw, "fb_write_target", lambda *a, **kw: (0.0, lambda c: c, "T1"))
    monkeypatch.setattr(query_ids, "probe_facebook", lambda tid, fn, **kw: {"status": 400, "body": "variables required"})
    drift = manage.doctor(probe_doc_ids=True)["doc_id_drift"]
    assert drift["stale"] is False
    assert drift["hint"] == ""


def test_doctor_probe_failure_does_not_break_report(monkeypatch):
    from agent_reach.channels import _facebook_write as fw

    monkeypatch.setattr(manage, "_channels", lambda: [])

    def boom(*a, **kw):
        raise OSError("browser offline")

    monkeypatch.setattr(fw, "fb_write_target", boom)
    drift = manage.doctor(probe_doc_ids=True)["doc_id_drift"]
    assert drift["verdict"] == "unprobed"
    assert "browser offline" in drift["error"]

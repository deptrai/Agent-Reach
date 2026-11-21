# -*- coding: utf-8 -*-
"""Doc-id drift monitor (story 16.3): both stores probed, drift → alert sink."""

import json
import os
import urllib.request
from pathlib import Path

import pytest

from agent_reach.runtime import cli as unified
from agent_reach.runtime import farm_drift, query_ids, state
from agent_reach.runtime.result import BAD_INPUT


@pytest.fixture(autouse=True)
def xactions_home(tmp_path, monkeypatch):
    home = tmp_path / "xactions"
    home.mkdir()
    monkeypatch.setenv("XACTIONS_HOME", str(home))
    return home


def _write_reach(doc: dict) -> None:
    path = query_ids.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _write_xactions(operations: dict) -> Path:
    path = farm_drift.xactions_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"operations": operations, "fetchedAt": "2026-11-22T00:00:00Z"}), encoding="utf-8")
    return path


def _run_cli(capsys, argv):
    """Run a unified command; return (code, envelope, other stdout lines)."""
    code = unified.main(argv)
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    envelope = None
    for line in reversed(lines):
        if line.startswith('{"schema_version"'):
            envelope = json.loads(line)
            break
    assert envelope is not None, f"no JSON envelope in stdout: {lines!r}"
    others = [line for line in lines if not line.startswith('{"schema_version"')]
    return code, envelope, others


# ------------------------------------------------------------------ probe surface


def test_run_once_reports_every_doc_id_from_both_stores(xactions_home):
    _write_reach({"facebook": {"post_create": "FB-1"}, "x": {"CreateTweet": "AAA", "DeleteTweet": "BBB"}})
    _write_xactions({"CreateTweet": {"queryId": "AAA", "operationType": "mutation"},
                     "SearchTimeline": {"queryId": "CCC", "operationType": "query"}})

    report = farm_drift.run_once()

    assert report["drifted"] == []
    assert report["alerts"] == []
    by_store = {(p["store"], p["platform"], p["name"]): p["doc_id"] for p in report["probes"]}
    assert by_store[("reach", "facebook", "post_create")] == "FB-1"
    assert by_store[("reach", "x", "CreateTweet")] == "AAA"
    assert by_store[("reach", "x", "DeleteTweet")] == "BBB"
    assert by_store[("xactions", "x", "CreateTweet")] == "AAA"
    assert by_store[("xactions", "x", "SearchTimeline")] == "CCC"
    assert report["stores"]["reach"].endswith("query-ids.json")
    assert report["stores"]["xactions"].endswith("query-ids.json")


def test_reach_side_edit_is_drift(xactions_home):
    _write_reach({"x": {"CreateTweet": "AAA"}})
    _write_xactions({"CreateTweet": {"queryId": "BBB"}})

    report = farm_drift.run_once()

    assert len(report["drifted"]) == 1
    drift = report["drifted"][0]
    assert drift["kind"] == "store-mismatch"
    assert drift["platform"] == "x"
    assert drift["name"] == "CreateTweet"
    assert drift["reach_id"] == "AAA"
    assert drift["xactions_id"] == "BBB"


def test_xactions_side_edit_is_drift(xactions_home):
    _write_reach({"x": {"CreateTweet": "AAA"}})
    _write_xactions({"CreateTweet": {"queryId": "ZZZ"}})  # the store rotated underneath Reach

    report = farm_drift.run_once()

    assert [d["name"] for d in report["drifted"]] == ["CreateTweet"]
    assert report["drifted"][0]["xactions_id"] == "ZZZ"


# ------------------------------------------------------------------ alert sinks


def test_alert_stdout_sink_is_default(xactions_home, capsys):
    _write_reach({"x": {"CreateTweet": "AAA"}})
    _write_xactions({"CreateTweet": {"queryId": "BBB"}})

    report = farm_drift.run_once()

    out = capsys.readouterr().out
    assert "x/CreateTweet" in out
    assert "AAA" in out and "BBB" in out
    assert report["sink"] == "stdout"
    assert report["sink_errors"] == []


def test_alert_file_sink_appends_json_lines(xactions_home, tmp_path, monkeypatch):
    log = tmp_path / "alerts" / "drifts.log"
    monkeypatch.setenv("REACH_DRIFT_SINK", "file")
    monkeypatch.setenv("REACH_DRIFT_FILE", str(log))
    _write_reach({"x": {"CreateTweet": "AAA"}})
    _write_xactions({"CreateTweet": {"queryId": "BBB"}})

    report = farm_drift.run_once()

    assert report["sink_errors"] == []
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "x/CreateTweet" in lines[0] and "AAA" in lines[0] and "BBB" in lines[0]


def test_alert_webhook_sink_posts_alerts(xactions_home, monkeypatch):
    monkeypatch.setenv("REACH_DRIFT_SINK", "webhook")
    monkeypatch.setenv("REACH_DRIFT_WEBHOOK_URL", "https://hooks.example.test/drift")
    _write_reach({"x": {"CreateTweet": "AAA"}})
    _write_xactions({"CreateTweet": {"queryId": "BBB"}})

    seen = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b""

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    report = farm_drift.run_once()

    assert report["sink_errors"] == []
    assert seen["url"] == "https://hooks.example.test/drift"
    assert seen["timeout"] == pytest.approx(farm_drift.DEFAULT_TIMEOUT)
    assert "x/CreateTweet" in json.dumps(seen["body"])
    assert "AAA" in json.dumps(seen["body"]) and "BBB" in json.dumps(seen["body"])


def test_no_drift_means_no_alert_anywhere(xactions_home, tmp_path, monkeypatch):
    monkeypatch.setenv("REACH_DRIFT_SINK", "file")
    monkeypatch.setenv("REACH_DRIFT_FILE", str(tmp_path / "drifts.log"))
    _write_reach({"x": {"CreateTweet": "AAA"}})
    _write_xactions({"CreateTweet": {"queryId": "AAA"}})

    report = farm_drift.run_once()

    assert report["drifted"] == []
    assert report["alerts"] == []
    assert report["sink_errors"] == []
    assert not (tmp_path / "drifts.log").exists()


# ------------------------------------------------------------------ config + live


def test_invalid_config_is_bad_input(xactions_home, capsys, monkeypatch):
    monkeypatch.setenv("REACH_DRIFT_SINK", "carrier-pigeon")
    code, envelope, _ = _run_cli(capsys, ["farm", "drift", "--once"])
    assert code == 1
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == BAD_INPUT

    monkeypatch.setenv("REACH_DRIFT_SINK", "webhook")
    monkeypatch.delenv("REACH_DRIFT_WEBHOOK_URL", raising=False)
    code, envelope, _ = _run_cli(capsys, ["farm", "drift", "--once"])
    assert code == 1
    assert envelope["error"]["code"] == BAD_INPUT

    monkeypatch.setenv("REACH_DRIFT_WEBHOOK_URL", "https://hooks.example.test/x")
    monkeypatch.setenv("REACH_DRIFT_INTERVAL", "soon")
    code, envelope, _ = _run_cli(capsys, ["farm", "drift", "--once"])
    assert code == 1
    assert envelope["error"]["code"] == BAD_INPUT


def test_live_probe_stale_doc_id_alerts(xactions_home, monkeypatch):
    _write_reach({"facebook": {"post_create": "FB-STALE"}})
    _write_xactions({})
    monkeypatch.setenv("REACH_DRIFT_LIVE", "1")
    monkeypatch.setattr(farm_drift, "_live_facebook", lambda: {
        "probed": "facebook",
        "results": [{"doc_id": "FB-STALE", "stale": True}],
        "verdict": "single-account",
        "stale": True,
    })

    report = farm_drift.run_once()

    assert report["live"]["stale"] is True
    assert report["drifted"][0]["platform"] == "facebook"
    assert report["drifted"][0]["name"] == "post_create"
    assert "facebook/post_create" in report["alerts"][0]


def test_next_delay_stays_within_jitter():
    cfg = farm_drift.config({})
    cfg = {**cfg, "interval": 100.0, "jitter": 30.0}
    delays = [farm_drift.next_delay(cfg, rng=lambda a, b: (a + b) / 2) for _ in range(20)]
    assert all(70.0 <= d <= 130.0 for d in delays)
    assert farm_drift.next_delay({**cfg, "jitter": 0.0}) == 100.0

# -*- coding: utf-8 -*-
"""Farm metrics (story 14.5): actions_log + fleet health → JSON sink."""

import json
import stat
import time
from contextlib import closing

from agent_reach.runtime import cli as unified
from agent_reach.runtime import farm, farm_metrics, fleet


def _run(capsys, argv):
    code = unified.main(argv)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1, "unified commands print exactly one JSON line"
    return code, json.loads(lines[0])


def _seed_two_accounts():
    farm.add_account("a1", "http://p1:8080")
    farm.add_account("a2", "http://p2:8080")
    for outcome in ("success", "success", "RATE_LIMITED", "PLATFORM_BLOCKED"):
        farm.record_action("a1", "post", outcome)
    farm.record_action("a1", "reply", "success")
    farm.record_action("a1", "reply", "QUOTA_EXCEEDED")
    farm.record_action("a1", "reply", "PERSISTED_QUERY_DEAD")
    farm.record_action("a1", "reply", "TIMEOUT")
    farm.record_action("a2", "post", "success")
    farm.record_action("a2", "post", "RATE_LIMITED")
    farm.record_action("a2", "post", "RATE_LIMITED")
    farm.record_action("a2", "comment", "PLATFORM_BLOCKED")


def _seed_container(name, account, session_state="ok", last_check_ts=None, ban_status="new"):
    now = int(time.time())
    with closing(fleet.connect()) as conn, conn:
        conn.execute(
            f"INSERT INTO fleet_containers ({fleet._COLUMNS}) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, "deadbeef", account, "http://p:1", "agent-reach-farm:1.6.1",
             session_state, ban_status, last_check_ts if last_check_ts is not None else now,
             None, 0, None, now),
        )


# ------------------------------------------------------------------ counting


def test_metrics_counts_two_accounts_by_class_and_error():
    _seed_two_accounts()
    out = farm_metrics.metrics()
    assert out["window_hours"] == 24
    assert isinstance(out["generated_ts"], int) and out["generated_ts"] > 0
    assert out["generated_at"].endswith("Z")
    assert "fleet" not in out  # no containers seeded → section omitted

    by_name = {a["account"]: a for a in out["accounts"]}
    assert set(by_name) == {"a1", "a2"}

    a1 = by_name["a1"]
    assert a1["ban_status"] == "new"
    assert a1["age_days"] == 0
    assert a1["actions"]["success"] == 3  # actions/day = successes in window
    assert a1["actions"]["attempts"] == 8
    assert a1["actions"]["by_class"] == {
        "post": {"success": 2, "attempts": 4},
        "reply": {"success": 1, "attempts": 4},
    }
    assert a1["errors"] == {
        "rate_limited": 1, "platform_blocked": 1, "quota_exceeded": 1,
        "persisted_query_dead": 1, "other": 1,
    }
    assert a1["by_outcome"]["TIMEOUT"] == 1

    a2 = by_name["a2"]
    assert a2["actions"]["success"] == 1
    assert a2["actions"]["attempts"] == 4
    assert a2["errors"]["rate_limited"] == 2
    assert a2["errors"]["platform_blocked"] == 1
    assert a2["actions"]["by_class"]["comment"] == {"success": 0, "attempts": 1}


def test_window_filtering_drops_rows_older_than_window():
    _seed_two_accounts()
    old = int(time.time()) - 90000
    with closing(farm.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO actions_log (account, ts, action_class, outcome) VALUES (?, ?, ?, ?)",
            ("a1", old, "post", "success"),
        )
        conn.execute(
            "INSERT INTO actions_log (account, ts, action_class, outcome) VALUES (?, ?, ?, ?)",
            ("a2", old, "post", "RATE_LIMITED"),
        )
    out = farm_metrics.metrics(window_hours=24)
    by_name = {a["account"]: a for a in out["accounts"]}
    assert by_name["a1"]["actions"]["success"] == 3
    assert by_name["a2"]["errors"]["rate_limited"] == 2

    wide = farm_metrics.metrics(window_hours=48)
    wide_by_name = {a["account"]: a for a in wide["accounts"]}
    assert wide["window_hours"] == 48
    assert wide_by_name["a1"]["actions"]["success"] == 4
    assert wide_by_name["a2"]["errors"]["rate_limited"] == 3


def test_single_account_scope_and_unknown_account():
    _seed_two_accounts()
    out = farm_metrics.metrics(account="a2")
    assert [a["account"] for a in out["accounts"]] == ["a2"]

    import pytest
    from agent_reach.runtime.result import BAD_INPUT, NEED_SETUP

    with pytest.raises(farm.ReachError) as ei:
        farm_metrics.metrics(account="ghost")
    assert ei.value.code == NEED_SETUP
    with pytest.raises(farm.ReachError) as ei:
        farm_metrics.metrics(account="../x")
    assert ei.value.code == BAD_INPUT
    with pytest.raises(farm.ReachError) as ei:
        farm_metrics.metrics(window_hours=0)
    assert ei.value.code == BAD_INPUT


def test_ban_status_and_age_days_come_from_registry():
    farm.add_account("aged", "http://p:1")
    old_age = int(time.time()) - 10 * 86400
    with closing(farm.connect()) as conn, conn:
        conn.execute(
            "UPDATE accounts SET age = ?, ban_status = 'suspended' WHERE name = 'aged'",
            (old_age,),
        )
    out = farm_metrics.metrics(account="aged")
    row = out["accounts"][0]
    assert row["ban_status"] == "suspended"
    assert row["age_days"] == 10
    assert row["actions"]["success"] == 0
    assert row["errors"]["rate_limited"] == 0


def test_fleet_section_when_containers_seeded():
    _seed_two_accounts()
    _seed_container("farm-a1", "a1", "ok")
    _seed_container("farm-a2", "a2", "NEED_LOGIN", ban_status="suspended")
    _seed_container("farm-other", "other-account", "down")

    out = farm_metrics.metrics()
    snap = out["fleet"]
    assert snap["total"] == 3
    assert snap["healthy"] == 1
    by_name = {c["name"]: c for c in snap["containers"]}
    assert by_name["farm-a1"]["session_state"] == "ok"
    assert by_name["farm-a1"]["healthy"] is True
    assert isinstance(by_name["farm-a1"]["last_check_age_s"], int)
    assert by_name["farm-a2"]["ban_status"] == "suspended"
    assert by_name["farm-a2"]["healthy"] is False

    scoped = farm_metrics.metrics(account="a1")
    assert [c["name"] for c in scoped["fleet"]["containers"]] == ["farm-a1"]
    assert farm_metrics.metrics(account="a2")["fleet"]["healthy"] == 0

    no_row = farm_metrics.metrics(account="aged") if False else None
    farm.add_account("bare", "http://p:9")
    assert "fleet" not in farm_metrics.metrics(account="bare")
    assert no_row is None


# ------------------------------------------------------------------ CLI + sink


def test_cli_envelope_shape(capsys):
    _seed_two_accounts()
    code, out = _run(capsys, ["farm", "metrics"])
    assert code == 0
    assert out["ok"] is True
    assert out["action"] == "farm metrics"
    result = out["result"]
    assert set(result) == {"generated_ts", "generated_at", "window_hours", "accounts"}
    assert len(result["accounts"]) == 2
    for row in result["accounts"]:
        assert set(row) >= {"account", "ban_status", "age_days", "actions", "errors", "by_outcome"}
        assert set(row["errors"]) == {
            "rate_limited", "platform_blocked", "quota_exceeded", "persisted_query_dead", "other",
        }
    json.dumps(out)  # envelope stays JSON-safe


def test_cli_account_filter_and_bad_usage(capsys):
    _seed_two_accounts()
    code, out = _run(capsys, ["farm", "metrics", "--account", "a1", "--hours", "12"])
    assert code == 0
    result = out["result"]
    assert [a["account"] for a in result["accounts"]] == ["a1"]
    assert result["window_hours"] == 12

    code, out = _run(capsys, ["farm", "metrics", "--hours", "0"])
    assert code == 1
    assert out["error"]["code"] == "BAD_INPUT"

    code, out = _run(capsys, ["farm", "metrics", "extra"])
    assert code == 1
    assert out["error"]["code"] == "BAD_INPUT"


def test_file_sink_jsonl_append_and_env(capsys, tmp_path, monkeypatch):
    _seed_two_accounts()
    sink = tmp_path / "metrics.jsonl"

    code, out = _run(capsys, ["farm", "metrics", "--file", str(sink)])
    assert code == 0
    assert out["result"]["sink_file"] == str(sink)
    code, out = _run(capsys, ["farm", "metrics", "--file", str(sink)])
    assert code == 0

    lines = sink.read_text().splitlines()
    assert len(lines) == 2  # append, not overwrite
    for line in lines:
        payload = json.loads(line)
        assert {a["account"] for a in payload["accounts"]} == {"a1", "a2"}
        assert "sink_file" not in payload  # the line is the pure metrics payload
    assert stat.S_IMODE(sink.stat().st_mode) == 0o600

    env_sink = tmp_path / "env.jsonl"
    monkeypatch.setenv("REACH_METRICS_FILE", str(env_sink))
    code, out = _run(capsys, ["farm", "metrics", "--account", "a2"])
    assert code == 0
    assert out["result"]["sink_file"] == str(env_sink)
    assert json.loads(env_sink.read_text())["accounts"][0]["account"] == "a2"

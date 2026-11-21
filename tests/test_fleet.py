# -*- coding: utf-8 -*-
"""Fleet manager (story 14.4): docker discovery, check poll, NEED_LOGIN reopen."""

import json
import subprocess
import time

import pytest

from agent_reach.runtime import cli as unified
from agent_reach.runtime import farm, fleet
from agent_reach.runtime.result import NEED_SETUP, ReachError


class Proc:
    """Minimal subprocess.CompletedProcess stand-in."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ps_json(*entries):
    return "\n".join(json.dumps(e) for e in entries) + "\n"


def _ps_entry(name, state="running", labels="com.agent-reach.farm=1,com.agent-reach.account=" + ""):
    return {"ID": "deadbeef" + name[-2:].ljust(2, "0"), "Names": name, "Image": "agent-reach-farm:1.6.1",
            "State": state, "Labels": labels}


CHECK_OK = json.dumps({
    "schema_version": 1, "ok": True, "scope": "all",
    "results": [{"channel": "twitter", "action": "search", "backend": "opencli",
                 "passed": True, "verified": True, "elapsed_ms": 5, "item_count": 3,
                 "error": None, "attempts": []}],
})

CHECK_NEED_LOGIN = json.dumps({
    "schema_version": 1, "ok": False, "scope": "all",
    "results": [{"channel": "twitter", "action": "search", "backend": "opencli",
                 "passed": False, "verified": False, "elapsed_ms": 5, "item_count": 0,
                 "error": {"code": "NEED_LOGIN", "message": "session dead"}, "attempts": []}],
})

CHECK_BROKEN = json.dumps({
    "schema_version": 1, "ok": False, "scope": "all",
    "results": [{"channel": "twitter", "action": "search", "backend": "opencli",
                 "passed": False, "verified": False, "elapsed_ms": 5, "item_count": 0,
                 "error": {"code": "UPSTREAM_BROKEN", "message": "bridge down"}, "attempts": []}],
})


def _docker_stub(monkeypatch, ps_entries, check_stdout=CHECK_OK, check_rc=0):
    """Patch fleet._run; returns the recorded argv list."""
    calls = []

    def fake(argv, timeout=None):
        calls.append(argv)
        if argv[:3] == ["docker", "ps", "-a"]:
            return Proc(0, _ps_json(*ps_entries))
        if argv[:2] == ["docker", "exec"] and "-d" in argv:
            return Proc(0, "")
        if argv[:2] == ["docker", "exec"]:
            return Proc(check_rc, check_stdout)
        return Proc(1, "", f"unexpected argv {argv}")

    monkeypatch.setattr(fleet, "_run", fake)
    return calls


def _exec_calls(calls):
    return [c for c in calls if c[:2] == ["docker", "exec"]]


def _relogin_calls(calls):
    return [c for c in _exec_calls(calls) if "-d" in c]


def _run_cli(capsys, argv):
    code = unified.main(argv)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1, "unified commands print exactly one JSON line"
    return code, json.loads(lines[0])


# ------------------------------------------------------------------ discovery


def test_discover_filters_to_farm_containers(monkeypatch):
    calls = _docker_stub(monkeypatch, [
        _ps_entry("farm-acct1", "running"),
        _ps_entry("some-other", "running", labels=""),
        _ps_entry("farm-acct2", "exited"),
    ])
    found = fleet.discover()
    assert calls[0] == ["docker", "ps", "-a", "--format", "json"]
    assert [c["name"] for c in found] == ["farm-acct1", "farm-acct2"]
    assert found[0]["state"] == "running"
    assert found[1]["state"] == "exited"


# ------------------------------------------------------------------ poll


def test_poll_populates_registry_from_check_json(monkeypatch):
    farm.add_account("acct1", "http://proxy-i:7000")
    _docker_stub(monkeypatch, [_ps_entry("farm-acct1")])

    report = fleet.poll_once()

    assert report["polled"] == 1
    row = fleet.get_container("farm-acct1")
    assert row["account"] == "acct1"
    assert row["proxy"] == "http://proxy-i:7000"
    assert row["ban_status"] == "new"
    assert row["session_state"] == "ok"
    assert row["container_id"].startswith("deadbeef")
    assert row["image"] == "agent-reach-farm:1.6.1"
    assert isinstance(row["last_check_ts"], int) and row["last_check_ts"] > 0
    assert row["last_error"] is None
    assert row["pending_login"] == 0


def test_need_login_fires_relogin_once_per_transition(monkeypatch):
    calls = _docker_stub(monkeypatch, [_ps_entry("farm-acct1")],
                         check_stdout=CHECK_NEED_LOGIN, check_rc=1)

    fleet.poll_once()

    row = fleet.get_container("farm-acct1")
    assert row["session_state"] == "NEED_LOGIN"
    assert row["login_channel"] == "twitter"
    assert row["pending_login"] == 1
    assert "pending operator" in row["last_error"]
    relogins = _relogin_calls(calls)
    assert len(relogins) == 1
    assert relogins[0] == ["docker", "exec", "-u", "farm", "-d", "farm-acct1",
                           "agent-reach", "login", "twitter"]

    fleet.poll_once()  # still NEED_LOGIN → no second relogin
    assert len(_relogin_calls(calls)) == 1
    assert fleet.get_container("farm-acct1")["pending_login"] == 1


def test_pending_login_cleared_on_recovery(monkeypatch):
    calls = _docker_stub(monkeypatch, [_ps_entry("farm-acct1")],
                         check_stdout=CHECK_NEED_LOGIN, check_rc=1)
    fleet.poll_once()
    assert fleet.get_container("farm-acct1")["pending_login"] == 1

    _docker_stub(monkeypatch, [_ps_entry("farm-acct1")], check_stdout=CHECK_OK)
    fleet.poll_once()

    row = fleet.get_container("farm-acct1")
    assert row["session_state"] == "ok"
    assert row["pending_login"] == 0
    assert row["login_channel"] is None


def test_unreachable_when_exec_returns_no_json(monkeypatch):
    calls = _docker_stub(monkeypatch, [_ps_entry("farm-acct1")],
                         check_stdout="not json at all", check_rc=1)
    fleet.poll_once()
    row = fleet.get_container("farm-acct1")
    assert row["session_state"] == "unreachable"
    assert "no check JSON" in row["last_error"]
    assert _relogin_calls(calls) == []


def test_exited_container_marked_down_without_exec(monkeypatch):
    calls = _docker_stub(monkeypatch, [_ps_entry("farm-acct1", "exited")])
    fleet.poll_once()
    row = fleet.get_container("farm-acct1")
    assert row["session_state"] == "down"
    assert "exited" in row["last_error"]
    assert _exec_calls(calls) == []  # no docker exec for a stopped container


def test_degraded_on_non_login_failures(monkeypatch):
    _docker_stub(monkeypatch, [_ps_entry("farm-acct1")],
                 check_stdout=CHECK_BROKEN, check_rc=1)
    fleet.poll_once()
    row = fleet.get_container("farm-acct1")
    assert row["session_state"] == "degraded"
    assert "UPSTREAM_BROKEN" in row["last_error"]


# ------------------------------------------------------------------ CLI surface


def test_fleet_list_cli_envelope(capsys, monkeypatch):
    _docker_stub(monkeypatch, [_ps_entry("farm-acct1")])
    fleet.poll_once()

    code, out = _run_cli(capsys, ["fleet", "list"])
    assert code == 0
    assert out["ok"] is True
    assert out["action"] == "fleet list"
    containers = out["result"]["containers"]
    assert len(containers) == 1
    c = containers[0]
    assert c["name"] == "farm-acct1"
    assert c["session_state"] == "ok"
    assert c["healthy"] is True
    assert isinstance(c["last_check_age_s"], int)
    json.dumps(out)  # envelope stays JSON-safe


def test_fleet_poll_once_via_cli(capsys, monkeypatch):
    _docker_stub(monkeypatch, [_ps_entry("farm-acct1")])
    code, out = _run_cli(capsys, ["fleet", "poll", "--once"])
    assert code == 0
    assert out["action"] == "fleet poll"
    assert out["result"]["polled"] == 1
    assert out["result"]["containers"][0]["session_state"] == "ok"


def test_fleet_relogin_manual_uses_recorded_channel(capsys, monkeypatch):
    calls = _docker_stub(monkeypatch, [_ps_entry("farm-acct1")],
                         check_stdout=CHECK_NEED_LOGIN, check_rc=1)
    fleet.poll_once()
    assert len(_relogin_calls(calls)) == 1

    code, out = _run_cli(capsys, ["fleet", "relogin", "farm-acct1"])
    assert code == 0
    assert out["ok"] is True
    result = out["result"]
    assert result["container"] == "farm-acct1"
    assert result["channel"] == "twitter"
    assert result["pending_login"] is True
    assert "operator" in result["note"]
    assert len(_relogin_calls(calls)) == 2  # manual re-fire allowed


def test_relogin_unknown_container_is_error(capsys):
    code, out = _run_cli(capsys, ["fleet", "relogin", "farm-ghost"])
    assert code == 1
    assert out["ok"] is False
    assert "NEED_SETUP" in str(out["error"])

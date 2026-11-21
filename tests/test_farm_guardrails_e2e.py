# -*- coding: utf-8 -*-
"""Guardrails e2e (story 13.4): the X write lane chained through all dispatch
guards, from the CLI JSON envelope down to the farm ledger.

One scenario exercises the gates in their real order — proxy kill-switch,
then warm-up ramp inside the write quota — while the platform backend never
runs (its stub fails the whole run if a guard lets a refusal through). What
is new here vs test_farm_quota/killswitch/ramp is the chain and the X lane,
not the gate internals.
"""

import json
import socket
import time
from contextlib import closing

import pytest

from agent_reach.channels.twitter import TwitterChannel
from agent_reach.runtime import cli as unified
from agent_reach.runtime import dispatch, farm, state
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import PLATFORM_BLOCKED, QUOTA_EXCEEDED, make_item

DEAD_PROXY = "http://127.0.0.1:1"  # loopback port 1 refuses instantly


@pytest.fixture(autouse=True)
def _clear_verdicts():
    dispatch._PROXY_VERDICTS.clear()
    yield
    dispatch._PROXY_VERDICTS.clear()


def _set_age(account: str, days_ago: float) -> None:
    """Rewrite the registry onboard epoch — the operator-facing ramp input."""
    with closing(farm.connect()) as conn:
        with conn:
            conn.execute(
                "UPDATE accounts SET age = ? WHERE name = ?",
                (int(time.time()) - int(days_ago * 86400), account),
            )


def _set_proxy(account: str, proxy: str) -> None:
    with closing(farm.connect()) as conn:
        with conn:
            conn.execute("UPDATE accounts SET proxy = ? WHERE name = ?", (proxy, account))


def _live_proxy(account: str):
    """Point the account at an accept-ably open loopback endpoint (proxy up)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(30)
    _set_proxy(account, f"http://127.0.0.1:{server.getsockname()[1]}")
    return server


def _rows(account: str) -> int:
    with closing(farm.connect()) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM actions_log WHERE account = ?", (account,)
        ).fetchone()[0]


def _ok_post(**kw):
    return [make_item(title="ok", url="https://x.test/1")]


def _boom(**kw):
    raise AssertionError("backend ran — a guard should have refused first")


def _cli(capsys, argv):
    code = unified.main(argv)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1, "unified commands print exactly one JSON line"
    return code, json.loads(lines[0])


def _setup_success_account(monkeypatch, account="test", days=30, cap="2"):
    farm.add_account(account, DEAD_PROXY)
    _set_age(account, days)
    server = _live_proxy(account)
    monkeypatch.setenv("REACH_ACCOUNT", account)
    monkeypatch.setenv("REACH_CAP_POST", cap)
    channel = TwitterChannel()
    monkeypatch.setattr(channel, "_post", _ok_post)
    return server, channel


def test_x_post_over_cap_refuses_before_backend(monkeypatch):
    """REACH_ACCOUNT=test + REACH_CAP_POST=2: post 3 is refused, ledger stays 2."""
    server, channel = _setup_success_account(monkeypatch)
    try:
        outs = [run_action(channel, "post", {"text": t}) for t in ("one", "two", "three")]
        assert outs[0]["ok"] and outs[1]["ok"]
        assert outs[2]["error"]["code"] == QUOTA_EXCEEDED
        msg = outs[2]["error"]["message"]
        assert "cap" in msg and "test" in msg and "post" in msg and "2" in msg
        assert _rows("test") == 2  # the refusal never reached the platform
    finally:
        server.close()


def test_x_post_dead_proxy_platform_blocked_zero_egress(monkeypatch):
    """proxy http://127.0.0.1:1 → PLATFORM_BLOCKED; the backend never ran."""
    farm.add_account("test", DEAD_PROXY)
    _set_age("test", 30)
    monkeypatch.setenv("REACH_ACCOUNT", "test")
    channel = TwitterChannel()
    monkeypatch.setattr(channel, "_post", _boom)
    out = run_action(channel, "post", {"text": "hi"})
    assert out["error"]["code"] == PLATFORM_BLOCKED
    assert "test" in out["error"]["message"] and DEAD_PROXY in out["error"]["message"]
    assert _rows("test") == 0  # a gate refusal is no attempt


def test_x_day1_ramp_shrinks_cap_and_refusal_names_ramp(monkeypatch):
    """1-day-old account: REACH_CAP_POST=8 × ramp 0.25 → effective cap 2."""
    server, channel = _setup_success_account(monkeypatch, days=1, cap="8")
    try:
        outs = [run_action(channel, "post", {"text": t}) for t in ("one", "two", "three")]
        assert outs[0]["ok"] and outs[1]["ok"]
        assert outs[2]["error"]["code"] == QUOTA_EXCEEDED
        msg = outs[2]["error"]["message"]
        assert "ramp" in msg and "0.25" in msg and "8" in msg and "test" in msg
        assert _rows("test") == 2
    finally:
        server.close()


def test_x_one_scenario_chains_ramp_then_kill_switch(monkeypatch):
    """Same account, gates in order: ramped cap refuses at 2, then a dead proxy
    refuses PLATFORM_BLOCKED before quota counting — backend untouched."""
    server, channel = _setup_success_account(monkeypatch, days=1, cap="8")
    try:
        outs = [run_action(channel, "post", {"text": t}) for t in ("one", "two")]
        assert all(o["ok"] for o in outs)
    finally:
        server.close()
    _set_proxy("test", DEAD_PROXY)
    dispatch._PROXY_VERDICTS.clear()
    monkeypatch.setattr(channel, "_post", _boom)
    out = run_action(channel, "post", {"text": "three"})
    assert out["error"]["code"] == PLATFORM_BLOCKED
    assert _rows("test") == 2  # the kill-switch refusal recorded nothing


def test_x_cli_envelope_surfaces_quota_and_proxy_blocks(monkeypatch, capsys):
    """The operator-facing path: `agent-reach twitter post …` prints one JSON
    envelope with the refusal code/message — the ticket's "X write lane thật"."""
    server, channel = _setup_success_account(monkeypatch)
    monkeypatch.setattr(unified, "_channel", lambda name: channel if name == "twitter" else None)
    try:
        for text in ("one", "two"):
            code, out = _cli(capsys, ["twitter", "post", text])
            assert code == 0 and out["ok"]
        code, out = _cli(capsys, ["twitter", "post", "three"])
        assert code == 1 and out["ok"] is False
        assert out["error"]["code"] == QUOTA_EXCEEDED
        assert "cap" in out["error"]["message"]
    finally:
        server.close()
    # Dead proxy → the same envelope carries PLATFORM_BLOCKED instead.
    _set_proxy("test", DEAD_PROXY)
    dispatch._PROXY_VERDICTS.clear()
    code, out = _cli(capsys, ["twitter", "post", "four"])
    assert code == 1 and out["ok"] is False
    assert out["error"]["code"] == PLATFORM_BLOCKED
    assert _rows("test") == 2

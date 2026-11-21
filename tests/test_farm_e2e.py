# -*- coding: utf-8 -*-
"""Farm e2e (story 11.8): two accounts on two fake HOMEs, run sequentially —
env resolve stays distinct, dispatch writes state under the right HOME, the
ledger counts per account. One pass exercises the whole substrate:
add_account → account_env → run_action env injection → record_action/ledger_24h.
"""

import os
from contextlib import closing
from pathlib import Path

import pytest

from agent_reach.channels.base import Action, Channel, Param
from agent_reach.runtime import farm, state
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import NEED_SETUP, ReachError, make_item

TARGET = "https://example.test/x"


class _Probe(Channel):
    """Records the env and state root its backend actually ran under."""

    name = "probe"
    level = "optional"
    actions = (
        Action("read", "probe read", (Param("target", "t"),), (("probe", "_read"),)),
    )

    def __init__(self):
        self.runs = []

    def can_handle(self, url):
        return False

    def _read(self, **kwargs):
        self.runs.append(
            {
                "home": os.environ.get("HOME"),
                "proxy": os.environ.get("REACH_BROWSER_PROXY"),
                "account": os.environ.get("REACH_ACCOUNT"),
                "root": state.root(),
            }
        )
        return [make_item(title="ok", url=TARGET)]


def _add_two_accounts() -> None:
    farm.add_account("a1", proxy="http://p1:8080")
    farm.add_account("a2", proxy="socks5://p2:9050")


def _account_root(name: str) -> Path:
    """The .agent-reach tree a run under account `name` writes into."""
    return Path(farm.account_env(name)["HOME"]) / ".agent-reach"


def test_env_resolve_two_accounts_distinct():
    _add_two_accounts()
    env1, env2 = farm.account_env("a1"), farm.account_env("a2")
    accounts = state.root() / "farm" / "accounts"
    assert env1 == {
        "HOME": str(accounts / "a1"),
        "REACH_BROWSER_PROXY": "http://p1:8080",
        "REACH_ACCOUNT": "a1",
    }
    assert env2 == {
        "HOME": str(accounts / "a2"),
        "REACH_BROWSER_PROXY": "socks5://p2:9050",
        "REACH_ACCOUNT": "a2",
    }
    assert env1["HOME"] != env2["HOME"]
    with pytest.raises(ReachError) as excinfo:
        farm.account_env("ghost")
    assert excinfo.value.code == NEED_SETUP


def test_dispatch_two_accounts_state_lands_in_own_home(monkeypatch):
    _add_two_accounts()
    outer_root = state.root()
    home_before = os.environ["HOME"]
    probe = _Probe()
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    assert run_action(probe, "read", {"target": TARGET})["ok"]
    monkeypatch.setenv("REACH_ACCOUNT", "a2")
    assert run_action(probe, "read", {"target": TARGET})["ok"]

    run1, run2 = probe.runs
    # each backend observed its own identity end to end
    assert run1["home"] == str(_account_root("a1").parent)
    assert run1["proxy"] == "http://p1:8080"
    assert run1["account"] == "a1"
    assert run1["root"] == _account_root("a1")
    assert run2["home"] == str(_account_root("a2").parent)
    assert run2["proxy"] == "socks5://p2:9050"
    assert run2["account"] == "a2"
    assert run2["root"] == _account_root("a2")

    # state written during each run landed under that account's own tree …
    assert (_account_root("a1") / "state.json").is_file()
    assert (_account_root("a2") / "state.json").is_file()
    # … and nowhere else: the outer HOME holds the farm registry but no run
    # state, and neither account tree carries a nested registry
    assert not (outer_root / "state.json").exists()
    assert not (_account_root("a1") / "farm").exists()
    assert not (_account_root("a2") / "farm").exists()
    # env restored after the sequential runs
    assert os.environ["HOME"] == home_before


def test_ledger_counts_per_account_only():
    _add_two_accounts()
    for outcome in ("success", "success", "RATE_LIMITED"):
        farm.record_action("a1", "post", outcome)
    farm.record_action("a1", "reply", "success")
    farm.record_action("a2", "post", "success")
    farm.record_action("a2", "post", "TIMEOUT")

    led1, led2 = farm.ledger_24h("a1"), farm.ledger_24h("a2")
    assert led1["success"] == 3 and led1["attempts"] == 4
    assert led1["by_class"] == {
        "post": {"success": 2, "attempts": 3},
        "reply": {"success": 1, "attempts": 1},
    }
    assert led2["success"] == 1 and led2["attempts"] == 2
    assert led2["by_class"] == {"post": {"success": 1, "attempts": 2}}

    # raw rows stay tagged to their own account — nothing leaks across
    with closing(farm.connect()) as conn:
        rows = conn.execute(
            "SELECT account, COUNT(*) AS n FROM actions_log GROUP BY account"
        ).fetchall()
    assert dict(rows) == {"a1": 4, "a2": 2}

    with pytest.raises(ReachError) as excinfo:
        farm.ledger_24h("ghost")
    assert excinfo.value.code == NEED_SETUP


def test_dispatch_unknown_account_need_setup_never_runs(monkeypatch):
    _add_two_accounts()
    home_before = os.environ["HOME"]
    probe = _Probe()
    monkeypatch.setenv("REACH_ACCOUNT", "ghost")
    out = run_action(probe, "read", {"target": TARGET})
    assert not out["ok"]
    assert out["error"]["code"] == "NEED_SETUP"
    assert probe.runs == []  # backend never ran — no silent default-HOME fallthrough
    assert os.environ["HOME"] == home_before

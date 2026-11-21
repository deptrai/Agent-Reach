# -*- coding: utf-8 -*-
"""Dispatch farm-account env resolve (story 11.4): REACH_ACCOUNT → registry env per run.

A probe channel records the env and state root its backend actually ran under,
inside `_call_with_deadline`'s worker thread — proving HOME_i reached the whole
action region, not just the dispatch caller.
"""

import os
from contextlib import closing
from pathlib import Path

from agent_reach.channels.base import Action, Channel, Param
from agent_reach.runtime import farm, state
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import ReachError, make_item

TARGET = "https://example.test/x"


class _Probe(Channel):
    name = "probe"
    level = "optional"
    actions = (
        Action("read", "probe read", (Param("target", "t"),), (("probe", "_read"),)),
    )

    def __init__(self):
        self.seen = {}

    def can_handle(self, url):
        return False

    def _read(self, **kwargs):
        self.seen["home"] = os.environ.get("HOME")
        self.seen["proxy"] = os.environ.get("REACH_BROWSER_PROXY")
        self.seen["account"] = os.environ.get("REACH_ACCOUNT")
        self.seen["root"] = state.root()
        return [make_item(title="ok", url=TARGET)]


def _account_root(name: str) -> Path:
    return Path(os.path.abspath(farm.account_env(name)["HOME"])) / ".agent-reach"


def test_registered_account_runs_under_account_home(monkeypatch):
    row = farm.add_account("test1", "http://p1:8080")
    probe = _Probe()
    monkeypatch.setenv("REACH_ACCOUNT", "test1")
    out = run_action(probe, "read", {"target": TARGET})
    assert out["ok"], out
    assert out["backend"] == "probe"
    assert probe.seen["home"] == row["home"]
    assert probe.seen["root"] == _account_root("test1")
    assert probe.seen["proxy"] == "http://p1:8080"
    assert probe.seen["account"] == "test1"
    # state written during the run lands in the account's own tree
    assert _account_root("test1").is_dir()


def test_unknown_account_is_need_setup_never_silent_home(monkeypatch):
    home_before = os.environ["HOME"]
    probe = _Probe()
    monkeypatch.setenv("REACH_ACCOUNT", "kotontai")
    out = run_action(probe, "read", {"target": TARGET})
    assert not out["ok"]
    assert out["error"]["code"] == "NEED_SETUP"
    assert "farm account add" in out["error"]["hint"]
    assert not probe.seen  # backend never ran
    assert os.environ["HOME"] == home_before  # no silent default-HOME run


def test_unset_account_keeps_legacy_home(monkeypatch):
    monkeypatch.delenv("REACH_ACCOUNT", raising=False)
    probe = _Probe()
    out = run_action(probe, "read", {"target": TARGET})
    assert out["ok"], out
    assert probe.seen["home"] == os.environ["HOME"]
    assert probe.seen["root"] == state.root()


def test_env_restored_after_run(monkeypatch):
    farm.add_account("test1", "http://p1:8080")
    home_before = os.environ["HOME"]
    monkeypatch.setenv("REACH_ACCOUNT", "test1")
    monkeypatch.setenv("REACH_BROWSER_PROXY", "http://inherited:1")
    out = run_action(_Probe(), "read", {"target": TARGET})
    assert out["ok"], out
    assert os.environ["HOME"] == home_before
    assert os.environ["REACH_BROWSER_PROXY"] == "http://inherited:1"
    assert os.environ["REACH_ACCOUNT"] == "test1"


def test_env_restored_after_backend_failure(monkeypatch):
    farm.add_account("test1", "http://p1:8080")
    home_before = os.environ["HOME"]

    def broken(self, **kwargs):
        raise ReachError("UPSTREAM_BROKEN", "backend died")

    monkeypatch.setattr(_Probe, "_read", broken)
    monkeypatch.setenv("REACH_ACCOUNT", "test1")
    monkeypatch.delenv("REACH_BROWSER_PROXY", raising=False)
    out = run_action(_Probe(), "read", {"target": TARGET})
    assert not out["ok"] and out["error"]["code"] == "UPSTREAM_BROKEN"
    assert os.environ["HOME"] == home_before
    assert "REACH_BROWSER_PROXY" not in os.environ


def test_account_without_proxy_drops_inherited_proxy(monkeypatch):
    farm.add_account("noproxy", "http://p:1")
    with closing(farm.connect()) as conn, conn:
        conn.execute("UPDATE accounts SET proxy = '' WHERE name = 'noproxy'")
    probe = _Probe()
    monkeypatch.setenv("REACH_ACCOUNT", "noproxy")
    monkeypatch.setenv("REACH_BROWSER_PROXY", "http://inherited:1")
    out = run_action(probe, "read", {"target": TARGET})
    assert out["ok"], out
    assert probe.seen["proxy"] is None  # inherited proxy never enters the run
    assert os.environ["REACH_BROWSER_PROXY"] == "http://inherited:1"  # restored


def test_two_accounts_sequential_runs_isolated(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    farm.add_account("a2", "socks5://p2:1080")
    probe = _Probe()
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    assert run_action(probe, "read", {"target": TARGET})["ok"]
    seen1 = dict(probe.seen)
    monkeypatch.setenv("REACH_ACCOUNT", "a2")
    assert run_action(probe, "read", {"target": TARGET})["ok"]
    assert seen1["root"] == _account_root("a1")
    assert probe.seen["root"] == _account_root("a2")
    assert seen1["proxy"] == "http://p1:8080"
    assert probe.seen["proxy"] == "socks5://p2:1080"


def test_bad_account_name_is_bad_input(monkeypatch):
    monkeypatch.setenv("REACH_ACCOUNT", "../bad")
    out = run_action(_Probe(), "read", {"target": TARGET})
    assert not out["ok"] and out["error"]["code"] == "BAD_INPUT"


def test_nested_env_enter_noop_and_registry_pinned(monkeypatch):
    """A nested run_action inside an applied env window must be a no-op and
    farm.* calls inside the window must reach the OPERATOR registry
    (REACH_FARM_ROOT pinned at entry), not a second registry under the
    account's swapped HOME."""
    from agent_reach.runtime import dispatch

    farm.add_account("test1", "http://p1:8080")
    outer_registry = farm.registry_path()
    monkeypatch.setenv("REACH_ACCOUNT", "test1")
    restore_outer = dispatch._farm_env_enter()
    try:
        assert os.environ.get("REACH_FARM_ROOT")
        restore_inner = dispatch._farm_env_enter()
        assert restore_inner() is None  # nested: inherits outer window
        assert farm.registry_path() == outer_registry
        assert [a["name"] for a in farm.list_accounts()] == ["test1"]
    finally:
        restore_outer()

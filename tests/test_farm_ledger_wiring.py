# -*- coding: utf-8 -*-
"""Write → ledger recording at dispatch (story 12.7).

A write verb under REACH_ACCOUNT appends one `actions_log` row carrying the
run's FINAL outcome ('success' or the result.py error code). Reads, write
verbs on other channels, and the single-account path (no REACH_ACCOUNT)
record nothing; ledger errors never break the action.
"""

import os
from contextlib import closing

from agent_reach.channels.base import Action, Channel, Param
from agent_reach.runtime import farm
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import ReachError, make_item

TARGET = "https://example.test/x"


class _WriteProbe(Channel):
    """Probe borrowing the `facebook` name so dispatch's WRITE_ACTIONS map applies."""

    name = "facebook"
    level = "optional"
    actions = (
        Action("post", "probe write", (Param("text", "t"),), (("probe", "_post"),)),
        Action("search", "probe read", (Param("query", "q"),), (("probe", "_search"),)),
    )

    def __init__(self):
        self.fail = None

    def can_handle(self, url):
        return False

    def _post(self, **kwargs):
        if self.fail:
            raise self.fail
        return [make_item(title="ok", url=TARGET)]

    def _search(self, **kwargs):
        return [make_item(title="ok", url=TARGET)]


class _TwitterProbe(Channel):
    name = "twitter"
    level = "optional"
    actions = (
        Action("post", "probe tweet", (Param("text", "t"),), (("probe", "_post"),)),
    )

    def can_handle(self, url):
        return False

    def _post(self, **kwargs):
        return [make_item(title="ok", url=TARGET)]


class _XueqiuProbe(Channel):
    """xueqiu's `quote` is a stock-price read — same verb, different lane."""

    name = "xueqiu"
    level = "optional"
    actions = (
        Action("quote", "stock quote", (Param("symbol", "s"),), (("probe", "_quote"),)),
    )

    def can_handle(self, url):
        return False

    def _quote(self, **kwargs):
        return [make_item(title="ok", url=TARGET)]


def test_write_success_records_ledger(monkeypatch):
    farm.add_account("test1", "http://p1:8080")
    monkeypatch.setenv("REACH_ACCOUNT", "test1")
    out = run_action(_WriteProbe(), "post", {"text": "hi"})
    assert out["ok"], out
    ledger = farm.ledger_24h("test1")
    assert ledger["success"] == 1
    assert ledger["attempts"] == 1
    assert ledger["by_class"]["post"] == {"success": 1, "attempts": 1}


def test_twitter_write_records_same_verb_class(monkeypatch):
    farm.add_account("test1", "http://p1:8080")
    monkeypatch.setenv("REACH_ACCOUNT", "test1")
    out = run_action(_TwitterProbe(), "post", {"text": "hi"})
    assert out["ok"], out
    ledger = farm.ledger_24h("test1")
    assert ledger["by_class"]["post"] == {"success": 1, "attempts": 1}


def test_write_failure_records_attempt_not_success(monkeypatch):
    farm.add_account("test1", "http://p1:8080")
    probe = _WriteProbe()
    probe.fail = ReachError("UPSTREAM_BROKEN", "backend died")
    monkeypatch.setenv("REACH_ACCOUNT", "test1")
    out = run_action(probe, "post", {"text": "hi"})
    assert not out["ok"]
    ledger = farm.ledger_24h("test1")
    assert ledger["success"] == 0
    assert ledger["attempts"] == 1
    assert ledger["by_class"]["post"] == {"success": 0, "attempts": 1}
    # The row stores the result.py error code, not 'success'.
    with closing(farm.connect()) as conn:
        outcomes = [r[0] for r in conn.execute("SELECT outcome FROM actions_log")]
    assert outcomes == ["UPSTREAM_BROKEN"]


def test_read_action_records_nothing(monkeypatch):
    farm.add_account("test1", "http://p1:8080")
    monkeypatch.setenv("REACH_ACCOUNT", "test1")
    out = run_action(_WriteProbe(), "search", {"query": "q"})
    assert out["ok"], out
    assert farm.ledger_24h("test1")["attempts"] == 0


def test_single_account_path_records_nothing(monkeypatch):
    farm.add_account("test1", "http://p1:8080")
    monkeypatch.delenv("REACH_ACCOUNT", raising=False)
    out = run_action(_WriteProbe(), "post", {"text": "hi"})
    assert out["ok"], out
    assert farm.ledger_24h("test1")["attempts"] == 0


def test_same_verb_on_unlisted_channel_records_nothing(monkeypatch):
    farm.add_account("test1", "http://p1:8080")
    monkeypatch.setenv("REACH_ACCOUNT", "test1")
    out = run_action(_XueqiuProbe(), "quote", {"symbol": "920008"})
    assert out["ok"], out
    assert farm.ledger_24h("test1")["attempts"] == 0


def test_validation_failure_records_attempt(monkeypatch):
    farm.add_account("test1", "http://p1:8080")
    monkeypatch.setenv("REACH_ACCOUNT", "test1")
    out = run_action(_WriteProbe(), "post", {})  # missing required `text`
    assert not out["ok"] and out["error"]["code"] == "BAD_INPUT"
    ledger = farm.ledger_24h("test1")
    assert ledger["success"] == 0 and ledger["attempts"] == 1


def test_unknown_account_still_need_setup_no_row(monkeypatch):
    monkeypatch.setenv("REACH_ACCOUNT", "ghost")
    out = run_action(_WriteProbe(), "post", {"text": "hi"})
    assert not out["ok"] and out["error"]["code"] == "NEED_SETUP"
    # record_action can't write a row for a missing account; swallowed either way.


def test_ledger_failure_never_breaks_action(monkeypatch):
    farm.add_account("test1", "http://p1:8080")
    monkeypatch.setenv("REACH_ACCOUNT", "test1")

    def boom(*args, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(farm, "record_action", boom)
    out = run_action(_WriteProbe(), "post", {"text": "hi"})
    assert out["ok"], out

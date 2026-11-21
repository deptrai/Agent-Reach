# -*- coding: utf-8 -*-
"""Per-day write quota (story 13.1).

A write under REACH_ACCOUNT is refused before any backend once the account's
rolling-24h success count for that action_class reaches the cap. Reads, the
legacy path, and unlimited classes are untouched; a refusal writes no ledger row.
"""

import os
import time
from contextlib import closing

from agent_reach.channels.base import Action, Channel, Param
from agent_reach.runtime import farm
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import QUOTA_EXCEEDED, ReachError, make_item

TARGET = "https://example.test/x"


class _WriteProbe(Channel):
    """Borrows the `facebook` name so WRITE_ACTIONS and DEFAULT_CAPS apply."""

    name = "facebook"
    level = "optional"
    actions = (
        Action("post", "probe write", (Param("text", "t"),), (("probe", "_post"),)),
        Action("search", "probe read", (Param("query", "q"),), (("probe", "_search"),)),
        Action("message", "uncapped write", (Param("text", "t"),), (("probe", "_post"),)),
    )

    def __init__(self):
        self.calls = 0

    def can_handle(self, url):
        return False

    def _post(self, **kwargs):
        self.calls += 1
        return [make_item(title="ok", url=TARGET)]

    def _search(self, **kwargs):
        self.calls += 1
        return [make_item(title="ok", url=TARGET)]


def _rows(account: str) -> int:
    """Raw actions_log rows — works even when ledger_24h itself is monkeypatched."""
    with closing(farm.connect()) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM actions_log WHERE account = ?", (account,)
        ).fetchone()[0]


def _mature(account: str) -> None:
    """Age the account past the 13.3 warm-up ramp so cap tests see the raw cap."""
    with closing(farm.connect()) as conn:
        with conn:
            conn.execute(
                "UPDATE accounts SET age = ? WHERE name = ?",
                (int(time.time()) - 30 * 86400, account),
            )


def test_third_post_is_refused_and_ledger_stays_at_two(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    _mature("a1")  # a fresh row is day-0 → ramp 25% would cut cap 2 to 1
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_POST", "2")
    probe = _WriteProbe()
    assert run_action(probe, "post", {"text": "one"})["ok"]
    assert run_action(probe, "post", {"text": "two"})["ok"]
    out = run_action(probe, "post", {"text": "three"})
    assert not out["ok"]
    assert out["error"]["code"] == QUOTA_EXCEEDED
    assert out["meta"]["quota_refused"] is True
    message = out["error"]["message"]
    assert "cap" in message and "a1" in message and "post" in message and "2" in message
    assert probe.calls == 2  # refusal never reached the backend
    snap = farm.ledger_24h("a1")
    assert snap["success"] == 2 and snap["attempts"] == 2
    assert snap["by_class"]["post"] == {"success": 2, "attempts": 2}


def test_under_cap_passes(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_POST", "2")
    probe = _WriteProbe()
    out = run_action(probe, "post", {"text": "one"})
    assert out["ok"], out
    assert probe.calls == 1
    assert _rows("a1") == 1


def test_default_cap_allows_a_post_without_env(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.delenv("REACH_CAP_POST", raising=False)
    out = run_action(_WriteProbe(), "post", {"text": "one"})
    assert out["ok"], out


def test_unset_account_is_uncapped(monkeypatch):
    monkeypatch.delenv("REACH_ACCOUNT", raising=False)
    monkeypatch.setenv("REACH_CAP_POST", "0")
    probe = _WriteProbe()
    out = run_action(probe, "post", {"text": "one"})
    assert out["ok"], out
    assert probe.calls == 1
    assert not farm.registry_path().exists()


def test_read_verb_ignores_cap(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_SEARCH", "0")
    probe = _WriteProbe()
    out = run_action(probe, "search", {"query": "q"})
    assert out["ok"], out
    assert probe.calls == 1
    assert _rows("a1") == 0


def test_uncapped_class_passes_until_env_sets_a_cap(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.delenv("REACH_CAP_MESSAGE", raising=False)
    probe = _WriteProbe()
    assert run_action(probe, "message", {"text": "hi"})["ok"]
    monkeypatch.setenv("REACH_CAP_MESSAGE", "0")
    out = run_action(probe, "message", {"text": "nope"})
    assert out["error"]["code"] == QUOTA_EXCEEDED
    assert probe.calls == 1
    assert _rows("a1") == 1  # the refusal added nothing



def test_invalid_cap_is_bad_input(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_POST", "lots")
    probe = _WriteProbe()
    out = run_action(probe, "post", {"text": "x"})
    assert out["error"]["code"] == "BAD_INPUT"
    assert probe.calls == 0
    assert _rows("a1") == 0  # gate refusal — never reached the platform


def test_unreadable_ledger_refuses_the_write(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_POST", "2")

    def broken(name):
        raise RuntimeError("registry down")

    monkeypatch.setattr(farm, "ledger_24h", broken)
    probe = _WriteProbe()
    out = run_action(probe, "post", {"text": "x"})
    assert not out["ok"]
    assert out["error"]["code"] == "UPSTREAM_BROKEN"
    assert probe.calls == 0
    assert _rows("a1") == 0  # gate refusal writes no row, even when record_action works


def test_cap_is_per_account(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    farm.add_account("a2", "http://p2:8080")
    monkeypatch.setenv("REACH_CAP_POST", "1")
    probe = _WriteProbe()
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    assert run_action(probe, "post", {"text": "a1"})["ok"]
    assert run_action(probe, "post", {"text": "a1-again"})["error"]["code"] == QUOTA_EXCEEDED
    monkeypatch.setenv("REACH_ACCOUNT", "a2")
    assert run_action(probe, "post", {"text": "a2"})["ok"]
    assert farm.ledger_24h("a1")["success"] == 1
    assert farm.ledger_24h("a2")["success"] == 1


def test_ledger_reach_error_surfaces_unchanged(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_POST", "2")

    def gone(name):
        raise ReachError("NEED_SETUP", "account vanished")

    monkeypatch.setattr(farm, "ledger_24h", gone)
    out = run_action(_WriteProbe(), "post", {"text": "x"})
    assert out["error"]["code"] == "NEED_SETUP"
    assert os.environ["REACH_ACCOUNT"] == "a1"
    assert _rows("a1") == 0

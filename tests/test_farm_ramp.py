# -*- coding: utf-8 -*-
"""Age-based warm-up ramp on quota caps (story 13.3).

The registry `age` column stores the onboard unix epoch. Dispatch scales the
resolved cap by a ramp factor — d0-3: 25%, d4-7: 50%, d8-14: 75%, d15+: 100%,
overridable via REACH_RAMP="days:factor,..." — so the effective cap is
max(1, floor(cap x factor)) compared against the ledger count. A missing
onboard field means a mature account; an explicit cap of 0 still refuses all.
"""

import time
from contextlib import closing

import pytest

from agent_reach.channels.base import Action, Channel, Param
from agent_reach.runtime import farm
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import QUOTA_EXCEEDED, make_item

TARGET = "https://example.test/x"


class _WriteProbe(Channel):
    """Borrows the `facebook` name so WRITE_ACTIONS applies (see test_farm_quota)."""

    name = "facebook"
    level = "optional"
    actions = (
        Action("post", "probe write", (Param("text", "t"),), (("probe", "_post"),)),
    )

    def __init__(self):
        self.calls = 0

    def can_handle(self, url):
        return False

    def _post(self, **kwargs):
        self.calls += 1
        return [make_item(title="ok", url=TARGET)]


def _set_age(account: str, value) -> None:
    """Rewrite the registry `age` (onboard epoch) — the ticket's verify path."""
    with closing(farm.connect()) as conn:
        with conn:
            conn.execute("UPDATE accounts SET age = ? WHERE name = ?", (value, account))


def _days_ago(days: float) -> int:
    return int(time.time()) - int(days * 86400)


def _posts(probe: _WriteProbe, n: int) -> None:
    for i in range(n):
        out = run_action(probe, "post", {"text": f"post-{i}"})
        assert out["ok"], out


def test_day1_account_gets_quarter_cap(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    _set_age("a1", _days_ago(1))
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_POST", "10")
    probe = _WriteProbe()
    _posts(probe, 2)
    out = run_action(probe, "post", {"text": "three"})
    assert out["error"]["code"] == QUOTA_EXCEEDED
    assert out["meta"]["quota_refused"] is True
    msg = out["error"]["message"]
    # the refusal explains the ramp so the operator sees why 10 became 2
    assert "ramp" in msg and "0.25" in msg and "10" in msg and "a1" in msg
    assert probe.calls == 2  # refused before the backend, count 2 < cap 10


def test_day30_account_gets_full_cap(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    _set_age("a1", _days_ago(30))
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_POST", "10")
    probe = _WriteProbe()
    _posts(probe, 10)
    out = run_action(probe, "post", {"text": "eleven"})
    assert out["error"]["code"] == QUOTA_EXCEEDED
    assert "ramp" not in out["error"]["message"]  # full cap — nothing scaled
    assert probe.calls == 10


@pytest.mark.parametrize(
    "days,allowed",
    [
        (0, 2),    # day 0 → 25% of 8
        (3, 2),    # last 25% day
        (4, 4),    # first 50% day
        (7, 4),    # last 50% day
        (8, 6),    # first 75% day
        (14, 6),   # last 75% day
        (15, 8),   # mature → full cap
    ],
)
def test_ramp_boundaries(monkeypatch, days, allowed):
    farm.add_account("a1", "http://p1:8080")
    _set_age("a1", _days_ago(days))
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_POST", "8")
    probe = _WriteProbe()
    _posts(probe, allowed)
    out = run_action(probe, "post", {"text": "over"})
    assert out["error"]["code"] == QUOTA_EXCEEDED
    assert probe.calls == allowed


def test_missing_onboard_field_is_treated_mature(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    _set_age("a1", "legacy")  # unmigrated/hand-repaired row → no epoch to read
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_POST", "4")
    probe = _WriteProbe()
    _posts(probe, 4)  # 25% would allow only 1 — full 4 proves factor 1.0
    out = run_action(probe, "post", {"text": "over"})
    assert out["error"]["code"] == QUOTA_EXCEEDED
    assert probe.calls == 4


def test_ramp_never_floors_below_one(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    _set_age("a1", _days_ago(1))
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_POST", "1")  # 1 x 0.25 floors to 0 → clamped to 1
    probe = _WriteProbe()
    _posts(probe, 1)
    out = run_action(probe, "post", {"text": "two"})
    assert out["error"]["code"] == QUOTA_EXCEEDED
    assert probe.calls == 1


def test_cap_zero_still_refuses_all_for_a_young_account(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    _set_age("a1", _days_ago(1))
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_POST", "0")
    probe = _WriteProbe()
    out = run_action(probe, "post", {"text": "x"})
    assert out["error"]["code"] == QUOTA_EXCEEDED
    assert probe.calls == 0


def test_reach_ramp_env_overrides_the_table(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    _set_age("a1", _days_ago(1))
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_POST", "10")
    monkeypatch.setenv("REACH_RAMP", "2:0.5")  # custom: days ≤2 → 50%
    probe = _WriteProbe()
    _posts(probe, 5)
    out = run_action(probe, "post", {"text": "six"})
    assert out["error"]["code"] == QUOTA_EXCEEDED
    assert probe.calls == 5


def test_invalid_reach_ramp_is_bad_input(monkeypatch):
    farm.add_account("a1", "http://p1:8080")
    _set_age("a1", _days_ago(1))
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_POST", "10")
    monkeypatch.setenv("REACH_RAMP", "three:quarter")
    probe = _WriteProbe()
    out = run_action(probe, "post", {"text": "x"})
    assert out["error"]["code"] == "BAD_INPUT"
    assert out["meta"]["quota_refused"] is True
    assert probe.calls == 0

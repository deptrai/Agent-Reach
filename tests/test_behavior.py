# -*- coding: utf-8 -*-
"""Behavior-sim layer tests — pacing persistence, env toggles, no-throw."""

from __future__ import annotations

import pytest

from agent_reach.runtime import behavior


@pytest.fixture(autouse=True)
def _sim_env(monkeypatch, isolated_home):
    # isolated_home gives state.load/mutate a private root; re-enable sim
    # (conftest sets REACH_SIM=0 globally for the write-lane tests).
    monkeypatch.setenv("REACH_SIM", "1")
    monkeypatch.setenv("REACH_SIM_DELAY", "0,0")
    monkeypatch.setenv("REACH_WRITE_INTERVAL", "60")
    yield


def test_disabled_returns_noop(monkeypatch):
    monkeypatch.setenv("REACH_SIM", "0")
    assert behavior.enabled() is False
    assert behavior.pace_write("x") == 0.0
    assert behavior.humanize("x", None) == {"sim": False}


def test_first_write_not_paced():
    assert behavior.pace_write("twitter") == 0.0


def test_second_write_waits_for_interval(monkeypatch):
    behavior.pace_write("twitter")
    slept = []
    monkeypatch.setattr(behavior.time, "sleep", slept.append)
    again = behavior.pace_write("twitter")
    assert again == pytest.approx(60, abs=1.0)
    assert slept and slept[0] == pytest.approx(60, abs=1.0)


def test_pacing_keyed_per_channel(monkeypatch):
    behavior.pace_write("twitter")
    monkeypatch.setattr(behavior.time, "sleep", lambda s: pytest.fail("must not sleep"))
    assert behavior.pace_write("instagram") == 0.0


def test_pacing_persists_across_instances(monkeypatch):
    # state.json survives between CLI runs — a second process still waits.
    behavior.pace_write("facebook")
    slept = []
    monkeypatch.setattr(behavior.time, "sleep", slept.append)
    behavior.pace_write("facebook")
    assert slept  # persisted timestamp forced the wait


def test_invalid_interval_env_falls_back(monkeypatch):
    monkeypatch.setenv("REACH_WRITE_INTERVAL", "not-a-number")
    assert behavior._write_interval() == 20.0


def test_delay_range_parses_and_validates(monkeypatch):
    monkeypatch.setenv("REACH_SIM_DELAY", "3,9")
    assert behavior._delay_range() == (3.0, 9.0)
    monkeypatch.setenv("REACH_SIM_DELAY", "junk")
    assert behavior._delay_range() == (2.0, 8.0)
    monkeypatch.setenv("REACH_SIM_DELAY", "9,3")  # hi < lo → clamp
    assert behavior._delay_range() == (9.0, 9.0)


def test_presence_is_best_effort_without_browser():
    # No CDP target, no raise — writes must never block on the sim.
    behavior.simulate_presence(None)
    behavior.simulate_presence("deadbeef-target-id")


def test_humanize_composes_pace_presence_delay(monkeypatch):
    called = []
    monkeypatch.setattr(behavior, "simulate_presence", lambda tid, **kw: called.append(tid))
    out = behavior.humanize("reddit", "tab-1")
    assert out["sim"] is True and out["delay_s"] == 0.0
    assert called == ["tab-1"]

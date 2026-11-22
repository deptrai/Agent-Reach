# -*- coding: utf-8 -*-
"""Behavior simulation for write lanes — makes mutations look human-paced.

A platform write currently replays a captured mutation with zero preceding
UI activity: a comment POST arrives with no page view, no mouse movement and
no delay — the exact pattern anti-abuse systems flag (an Instagram account
was checkpointed after a burst of raw GraphQL test comments).

This module adds a cheap, portable layer:

* ``pace_write`` — a minimum interval between writes per channel, persisted
  in ``state.json`` so it survives across CLI invocations (one clock per
  channel for ALL write verbs — a real human doesn't post+like instantly).
* ``simulate_presence`` — trusted mouse-move + wheel events via CDP
  ``Input.dispatchMouseEvent`` on the target tab, so the session shows
  activity before the mutation fires.
* ``humanize`` — pace + presence + a random pre-write delay.

Disable entirely with ``REACH_SIM=0`` (used by the test suite). Tunables:

* ``REACH_SIM_DELAY`` — ``"min,max"`` seconds jitter before send (default ``2,8``).
* ``REACH_WRITE_INTERVAL`` — min seconds between writes per channel (default ``20``).
"""

from __future__ import annotations

import os
import random
import time


def enabled() -> bool:
    return os.environ.get("REACH_SIM", "1").strip().lower() not in {"0", "off", "false", "no"}


def _delay_range() -> tuple[float, float]:
    raw = os.environ.get("REACH_SIM_DELAY", "2,8")
    try:
        lo_s, hi_s = raw.split(",", 1)
        lo = max(0.0, float(lo_s))
        return lo, max(lo, float(hi_s))
    except (ValueError, IndexError):
        return 2.0, 8.0


def _write_interval() -> float:
    try:
        return max(0.0, float(os.environ.get("REACH_WRITE_INTERVAL", "20")))
    except ValueError:
        return 20.0


def pace_write(channel: str) -> float:
    """Sleep until >=REACH_WRITE_INTERVAL since the channel's last write,
    then stamp this write. One clock per channel across all write verbs.
    Returns seconds slept (0 when disabled)."""
    if not enabled():
        return 0.0
    from agent_reach.runtime import state

    key = f"{channel}:write"
    last = state.load().get("write_starts", {}).get(key)
    waited = 0.0
    if isinstance(last, (int, float)):
        waited = max(0.0, _write_interval() - (time.time() - last))
    if waited > 0:
        time.sleep(waited)
    state.mutate(lambda s: s.setdefault("write_starts", {}).__setitem__(key, time.time()))
    return waited


def simulate_presence(target_id: str | None, *, moves: int = 3, scrolls: int = 2) -> None:
    """Trusted mouse-move + wheel events on the target page via CDP Input.
    Best-effort — never raises, never blocks the write."""
    if not enabled() or not target_id:
        return
    from agent_reach.runtime import cdp

    try:
        for _ in range(moves):
            cdp.call(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": random.randint(80, 1400), "y": random.randint(80, 800)},
                target_id=target_id,
            )
            time.sleep(random.uniform(0.15, 0.5))
        for _ in range(scrolls):
            cdp.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": random.randint(200, 1200),
                    "y": random.randint(200, 600),
                    "deltaX": 0,
                    "deltaY": random.randint(120, 500),
                },
                target_id=target_id,
            )
            time.sleep(random.uniform(0.2, 0.6))
    except Exception:
        return


def headroom() -> float:
    """Extra outer-budget seconds a write action needs when the sim layer is
    on: full pacing interval + max jitter delay + presence events + slack."""
    if not enabled():
        return 0.0
    return _write_interval() + _delay_range()[1] + 8.0


def humanize(channel: str, target_id: str | None) -> dict:
    """Full pre-write humanization: pacing + presence events + jitter delay.
    Called once per write verb after the target tab is ready, before send."""
    if not enabled():
        return {"sim": False}
    paced = pace_write(channel)
    simulate_presence(target_id)
    lo, hi = _delay_range()
    delay = random.uniform(lo, hi)
    time.sleep(delay)
    return {"sim": True, "paced_s": round(paced, 2), "delay_s": round(delay, 2)}

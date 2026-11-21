# -*- coding: utf-8 -*-
"""Proxy kill-switch soft-check at dispatch (story 13.2).

Under REACH_ACCOUNT with a registry proxy that refuses a TCP handshake, every
action — reads included — is refused PLATFORM_BLOCKED before any backend
(zero egress, contract §2 layer-1). Proxy-less accounts, the legacy path, and
the REACH_PROXY_CHECK=0 escape hatch run normally; a refusal writes no ledger
row; verdicts cache per proxy URL for ~60s.
"""

import socket
import threading
import time
from contextlib import closing

import pytest

from agent_reach.channels.base import Action, Channel, Param
from agent_reach.runtime import dispatch, farm
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import PLATFORM_BLOCKED, ReachError, make_item

TARGET = "https://example.test/x"
DEAD_PROXY = "http://127.0.0.1:1"  # loopback port 1 refuses instantly


class _Probe(Channel):
    name = "probe"
    level = "optional"
    actions = (
        Action("read", "probe read", (Param("target", "t"),), (("probe", "_read"),)),
    )

    def __init__(self):
        self.calls = 0

    def can_handle(self, url):
        return False

    def _read(self, **kwargs):
        self.calls += 1
        return [make_item(title="ok", url=TARGET)]


class _WriteProbe(Channel):
    """Borrows the `facebook` name so WRITE_ACTIONS and the ledger seam apply."""

    name = "facebook"
    level = "optional"
    actions = (
        Action("post", "probe write", (Param("text", "t"),), (("probe", "_post"),)),
        Action("search", "probe read", (Param("query", "q"),), (("probe", "_post"),)),
    )

    def __init__(self):
        self.calls = 0

    def can_handle(self, url):
        return False

    def _post(self, **kwargs):
        self.calls += 1
        return [make_item(title="ok", url=TARGET)]


@pytest.fixture(autouse=True)
def _clear_verdicts():
    dispatch._PROXY_VERDICTS.clear()
    yield
    dispatch._PROXY_VERDICTS.clear()


def _rows(account: str) -> int:
    with closing(farm.connect()) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM actions_log WHERE account = ?", (account,)
        ).fetchone()[0]


def _serve_once(ready: threading.Event) -> int:
    """A live TCP endpoint on an ephemeral loopback port; stays accept-ably open."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    ready.set()
    server.settimeout(30)
    return server


def test_dead_proxy_refuses_platform_blocked(monkeypatch):
    farm.add_account("a1", DEAD_PROXY)
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    probe = _Probe()
    out = run_action(probe, "read", {"target": TARGET})
    assert not out["ok"]
    assert out["error"]["code"] == PLATFORM_BLOCKED
    assert out["meta"]["quota_refused"] is True
    assert "a1" in out["error"]["message"] and DEAD_PROXY in out["error"]["message"]
    assert probe.calls == 0  # backend never ran — zero egress


def test_dead_proxy_refuses_a_read_write_verb_too(monkeypatch):
    farm.add_account("a1", DEAD_PROXY)
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    probe = _WriteProbe()
    out = run_action(probe, "post", {"text": "hi"})
    assert not out["ok"]
    assert out["error"]["code"] == PLATFORM_BLOCKED
    assert probe.calls == 0
    assert _rows("a1") == 0  # refusal is no attempt — nothing reached a platform


def test_proxy_less_account_runs(monkeypatch):
    farm.add_account("noproxy", "http://placeholder:1")
    with closing(farm.connect()) as conn, conn:
        conn.execute("UPDATE accounts SET proxy = '' WHERE name = 'noproxy'")
    monkeypatch.setenv("REACH_ACCOUNT", "noproxy")
    probe = _Probe()
    out = run_action(probe, "read", {"target": TARGET})
    assert out["ok"], out
    assert probe.calls == 1


def test_unset_account_skips_the_check(monkeypatch):
    monkeypatch.delenv("REACH_ACCOUNT", raising=False)
    # A hostile stale proxy env must not gate the legacy path.
    monkeypatch.setenv("REACH_BROWSER_PROXY", DEAD_PROXY)
    probe = _Probe()
    out = run_action(probe, "read", {"target": TARGET})
    assert out["ok"], out
    assert probe.calls == 1
    assert not farm.registry_path().exists()


def test_live_proxy_passes_and_caches(monkeypatch):
    ready = threading.Event()
    server = _serve_once(ready)
    port = server.getsockname()[1]
    try:
        farm.add_account("a1", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("REACH_ACCOUNT", "a1")
        probe = _Probe()
        assert run_action(probe, "read", {"target": TARGET})["ok"]
        # Second action within the ttl must not probe again: close the server
        # so a fresh handshake would fail — the cached OK still passes.
        server.close()
        assert run_action(probe, "read", {"target": TARGET})["ok"]
        assert probe.calls == 2
    finally:
        try:
            server.close()
        except OSError:
            pass


def test_dead_verdict_caches_within_ttl(monkeypatch):
    farm.add_account("a1", DEAD_PROXY)
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    calls = []
    real = dispatch._proxy_alive

    def counting(url):
        calls.append(url)
        return real(url)

    monkeypatch.setattr(dispatch, "_proxy_alive", counting)
    probe = _Probe()
    assert run_action(probe, "read", {"target": TARGET})["error"]["code"] == PLATFORM_BLOCKED
    assert run_action(probe, "read", {"target": TARGET})["error"]["code"] == PLATFORM_BLOCKED
    assert calls == [DEAD_PROXY]  # second refusal answered from the cache
    assert probe.calls == 0


def test_ttl_zero_rechecks_every_run(monkeypatch):
    farm.add_account("a1", DEAD_PROXY)
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_PROXY_CHECK_TTL", "0")
    probe = _Probe()
    assert run_action(probe, "read", {"target": TARGET})["error"]["code"] == PLATFORM_BLOCKED
    # The dead verdict expired instantly — a now-healthy proxy passes.
    monkeypatch.setattr(dispatch, "_proxy_alive", lambda url: None)
    assert run_action(probe, "read", {"target": TARGET})["ok"]
    assert probe.calls == 1


def test_check_off_bypasses_dead_proxy(monkeypatch):
    farm.add_account("a1", DEAD_PROXY)
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_PROXY_CHECK", "0")
    probe = _Probe()
    out = run_action(probe, "read", {"target": TARGET})
    assert out["ok"], out
    assert probe.calls == 1


def test_unresolvable_proxy_host_does_not_block(monkeypatch):
    """A proxy whose host has no DNS answer cannot be probed — the run proceeds
    rather than fail-closing every account that stores a placeholder URL."""
    farm.add_account("a1", "http://p1.invalid:8080")
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    probe = _Probe()
    out = run_action(probe, "read", {"target": TARGET})
    assert out["ok"], out
    assert probe.calls == 1
    assert "http://p1.invalid:8080" not in dispatch._PROXY_VERDICTS


def test_invalid_ttl_is_bad_input(monkeypatch):
    farm.add_account("a1", DEAD_PROXY)
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_PROXY_CHECK_TTL", "forever")
    out = run_action(_Probe(), "read", {"target": TARGET})
    assert out["error"]["code"] == "BAD_INPUT"


def test_registry_lookup_failure_fails_closed(monkeypatch):
    farm.add_account("a1", DEAD_PROXY)
    monkeypatch.setenv("REACH_ACCOUNT", "a1")

    def broken(name):
        raise RuntimeError("registry down")

    monkeypatch.setattr(farm, "account_proxy", broken)
    probe = _Probe()
    out = run_action(probe, "read", {"target": TARGET})
    assert not out["ok"]
    assert out["error"]["code"] == PLATFORM_BLOCKED
    assert probe.calls == 0
    assert _rows("a1") == 0


def test_kill_switch_runs_before_quota(monkeypatch):
    """Dead proxy + quota refusal → PLATFORM_BLOCKED wins; order is pinned so a
    dead egress never gets masked as a quota problem."""
    farm.add_account("a1", DEAD_PROXY)
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    monkeypatch.setenv("REACH_CAP_POST", "0")  # would refuse if it ever ran
    probe = _WriteProbe()
    out = run_action(probe, "post", {"text": "hi"})
    assert out["error"]["code"] == PLATFORM_BLOCKED
    assert probe.calls == 0
    assert _rows("a1") == 0

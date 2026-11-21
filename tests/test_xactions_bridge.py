# -*- coding: utf-8 -*-
"""Identity bridge Reach <-> XActions accountPool (story 16.1).

Each test gets an isolated HOME (conftest) plus an isolated XACTIONS_HOME, so
the farm registry DB and the XActions `accounts.db` pool store are both real
SQLite files under tmp — the same files the two products open for real.
"""

import sqlite3

import pytest

from agent_reach.runtime import farm
from agent_reach.runtime import xactions_bridge as bridge
from agent_reach.runtime.result import BAD_INPUT, NEED_SETUP, ReachError

COOKIES = "auth_token=tok123; ct0=csrf456"


@pytest.fixture(autouse=True)
def isolated_xactions_home(tmp_path, monkeypatch):
    home = tmp_path / "xactions"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("XACTIONS_HOME", str(home))
    return home


def _account(name="a1"):
    return farm.add_account(name, "http://p1:8080")


def _registry_ban_status(name):
    with farm.connect() as conn:
        return conn.execute(
            "SELECT ban_status FROM accounts WHERE name = ?", (name,)
        ).fetchone()[0]


def _pool_row(xid):
    with sqlite3.connect(bridge.pool_path()) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM accounts WHERE name = ?", (xid,)).fetchone()


def _link_row(name):
    with farm.connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM xactions_link WHERE reach_name = ?", (name,)
        ).fetchone()
    return dict(row) if row else None


def test_link_creates_locked_placeholder_and_resolves():
    _account("a1")
    out = bridge.link("a1")
    assert out["xactions_id"] == "reach:a1"
    assert bridge.resolve("a1") == "reach:a1"
    row = _pool_row("reach:a1")
    assert row is not None
    # Placeholder is parked: real pool can never hand it out before provision.
    assert row["locked"] == 1
    assert row["cookies"] == ""


def test_link_is_idempotent_and_relink_repoints():
    _account("a1")
    bridge.link("a1")
    bridge.link("a1")  # same target — no error, mapping unchanged
    assert bridge.resolve("a1") == "reach:a1"
    # Relinking to an existing pool entry attaches without clobbering it.
    bridge.pool.add("custom-x", cookies=COOKIES)
    bridge.link("a1", "custom-x")
    assert bridge.resolve("a1") == "custom-x"
    assert _pool_row("custom-x")["cookies"] == COOKIES
    assert _link_row("a1")["xactions_id"] == "custom-x"


def test_link_unknown_account_is_need_setup():
    with pytest.raises(ReachError) as exc:
        bridge.link("nope")
    assert exc.value.code == NEED_SETUP


def test_unlink_drops_mapping_but_keeps_pool_entry():
    _account("a1")
    bridge.link("a1")
    out = bridge.unlink("a1")
    assert out["unlinked"] is True
    assert bridge.resolve("a1") is None
    assert _pool_row("reach:a1") is not None
    assert bridge.unlink("a1")["unlinked"] is False


def test_provision_session_writes_cookies_and_unlocks():
    _account("a1")
    bridge.link("a1")
    out = bridge.provision_session(
        "a1", {"auth_token": "tok123", "ct0": "csrf456"}
    )
    assert out["provisioned"] is True
    row = _pool_row("reach:a1")
    assert "auth_token=tok123" in row["cookies"]
    assert "ct0=csrf456" in row["cookies"]
    assert row["locked"] == 0
    assert _link_row("a1")["provisioned_at"] is not None
    # The envelope never carries secrets back.
    assert "tok123" not in repr(out)


def test_provision_requires_auth_token_and_link():
    _account("a1")
    bridge.link("a1")
    with pytest.raises(ReachError) as exc:
        bridge.provision_session("a1", "ct0=only")
    assert exc.value.code == BAD_INPUT
    bridge.unlink("a1")
    with pytest.raises(ReachError) as exc2:
        bridge.provision_session("a1", COOKIES)  # no link -> NEED_SETUP
    assert exc2.value.code == NEED_SETUP


def test_record_ban_writes_registry_and_bridge_row():
    _account("a1")
    bridge.link("a1")
    out = bridge.record_ban("a1", "banned", "HTTP 403: suspended (code 64)")
    assert out["recorded"] is True
    assert _registry_ban_status("a1") == "banned"
    link = _link_row("a1")
    assert link["ban_status"] == "banned"
    assert "suspended" in link["ban_reason"]
    assert link["ban_checked_at"] is not None


def test_record_ban_unlinked_updates_registry_only():
    _account("a1")
    bridge.record_ban("a1", "banned", "manual")
    assert _registry_ban_status("a1") == "banned"
    assert bridge.resolve("a1") is None  # no phantom link fabricated


def test_sync_bans_folds_pool_locks_into_registry():
    _account("a1")
    _account("b2")
    bridge.link("a1")
    bridge.link("b2")
    # Simulate XActions-side detection: the pool entry gets locked.
    bridge.pool.update("reach:a1", locked=True,
                       lock_reason="HTTP 403 on Followers: suspended (code 64)")
    recorded = bridge.sync_bans()
    assert [r["reach_name"] for r in recorded] == ["a1"]
    assert _registry_ban_status("a1") == "banned"
    assert _registry_ban_status("b2") == "new"  # unlocked entry untouched
    # Second sync is a no-op fold (already recorded once — status still banned).
    assert [r["reach_name"] for r in bridge.sync_bans()] == ["a1"]


def test_pool_store_is_real_xactions_schema(tmp_path):
    # The bridge opens/creates the same file the JS pool uses.
    assert bridge.pool_path().name == "accounts.db"
    assert str(bridge.pool_path()).startswith(str(tmp_path / "xactions"))
    bridge.pool.add("probe", cookies=COOKIES, proxy="socks5://h:1")
    entry = bridge.pool.get("probe")
    assert entry is not None and entry.proxy == "socks5://h:1" and not entry.locked
    assert bridge.pool.remove("probe") is True
    assert bridge.pool.get("probe") is None

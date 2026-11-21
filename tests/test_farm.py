# -*- coding: utf-8 -*-
"""Farm registry: SQLite-WAL accounts table and the `farm` CLI surface."""

import json
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path

import pytest

from agent_reach.runtime import cli as unified
from agent_reach.runtime import farm, state
from agent_reach.runtime.result import BAD_INPUT, NEED_SETUP, UPSTREAM_BROKEN, ReachError

ACCOUNT_FIELDS = {"name", "proxy", "home", "age", "ban_status", "created_at", "updated_at"}


def _run(capsys, argv):
    code = unified.main(argv)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1, "unified commands print exactly one JSON line"
    return code, json.loads(lines[0])


def test_add_then_list(capsys):
    code, out = _run(capsys, ["farm", "account", "add", "test1", "--proxy", "http://p1:8080"])
    assert code == 0
    assert out["ok"] is True
    assert out["action"] == "farm account add"
    row = out["result"]
    assert row["name"] == "test1"
    assert row["proxy"] == "http://p1:8080"
    assert row["ban_status"] == "new"
    assert isinstance(row["age"], int) and row["age"] > 0
    home = state.root() / "farm" / "accounts" / "test1"
    assert row["home"] == str(home)
    assert home.is_dir()

    code, out = _run(capsys, ["farm", "account", "list"])
    assert code == 0
    assert out["action"] == "farm account list"
    accounts = out["result"]["accounts"]
    assert len(accounts) == 1
    assert set(accounts[0]) == ACCOUNT_FIELDS
    assert accounts[0]["name"] == "test1"
    json.dumps(out)  # the whole envelope stays JSON-safe


def test_list_empty_registry(capsys):
    code, out = _run(capsys, ["farm", "account", "list"])
    assert code == 0
    assert out["result"]["accounts"] == []


def test_duplicate_name_is_bad_input(capsys):
    code, _ = _run(capsys, ["farm", "account", "add", "dup", "--proxy", "http://p:1"])
    assert code == 0
    code, out = _run(capsys, ["farm", "account", "add", "dup", "--proxy", "http://p:2"])
    assert code == 1
    assert out["ok"] is False
    assert out["error"]["code"] == "BAD_INPUT"


@pytest.mark.parametrize("name", ["../x", "has space", "x" * 33, "a" * 33, "", "a/b"])
def test_bad_name_rejected_before_any_fs(capsys, name):
    code, out = _run(capsys, ["farm", "account", "add", name, "--proxy", "http://p:1"])
    assert code == 1
    assert out["error"]["code"] == "BAD_INPUT"
    assert not farm.farm_root().exists()


@pytest.mark.parametrize("proxy", [
    "notaurl", "ftp://h:1", "http://", "://missing", "",
    "http://[", "http://h:99999", "http://h:abc",
])
def test_bad_proxy_rejected_before_any_fs(capsys, proxy):
    code, out = _run(capsys, ["farm", "account", "add", "valid", "--proxy", proxy])
    assert code == 1
    assert out["error"]["code"] == "BAD_INPUT"
    assert not farm.farm_root().exists()


@pytest.mark.parametrize("argv", [
    ["farm", "account", "add"],
    ["farm", "account", "add", "noname"],
    ["farm", "account", "add", "a", "b", "--proxy", "http://p:1"],
    ["farm", "account", "list", "extra"],
    ["farm", "account", "env"],
    ["farm", "account", "env", "a", "b"],
    ["farm", "account", "env", "a", "--proxy", "http://p:1"],
    ["farm", "account"],
    ["farm", "bogus"],
    ["farm"],
])
def test_missing_or_unknown_args_are_bad_input(capsys, argv):
    code, out = _run(capsys, argv)
    assert code == 1
    assert out["error"]["code"] == "BAD_INPUT"


@pytest.mark.parametrize("op", ["env", "ledger"])
def test_top_level_env_and_ledger_still_stubbed(capsys, op):
    code, out = _run(capsys, ["farm", op])
    assert code == 1
    assert out["error"]["code"] == "BAD_INPUT"


def test_env_registered_account(capsys):
    farm.add_account("a1", "http://p1:8080")
    code, out = _run(capsys, ["farm", "account", "env", "a1"])
    assert code == 0
    assert out["action"] == "farm account env"
    env = out["result"]
    home = state.root() / "farm" / "accounts" / "a1"
    assert env == {
        "HOME": str(home),
        "REACH_BROWSER_PROXY": "http://p1:8080",
        "REACH_ACCOUNT": "a1",
    }
    assert Path(env["HOME"]).is_absolute()


def test_env_two_accounts_resolve_distinct_values(capsys):
    farm.add_account("a1", "http://p1:8080")
    farm.add_account("a2", "socks5://p2:1080")
    _, out1 = _run(capsys, ["farm", "account", "env", "a1"])
    _, out2 = _run(capsys, ["farm", "account", "env", "a2"])
    env1, env2 = out1["result"], out2["result"]
    assert env1["HOME"] != env2["HOME"]
    assert env1["HOME"] == str(state.root() / "farm" / "accounts" / "a1")
    assert env2["HOME"] == str(state.root() / "farm" / "accounts" / "a2")
    assert env1["REACH_BROWSER_PROXY"] == "http://p1:8080"
    assert env2["REACH_BROWSER_PROXY"] == "socks5://p2:1080"
    assert env1["REACH_ACCOUNT"] == "a1"
    assert env2["REACH_ACCOUNT"] == "a2"


def test_env_unknown_account_is_need_setup(capsys):
    code, out = _run(capsys, ["farm", "account", "env", "ghost"])
    assert code == 1
    assert out["ok"] is False
    assert out["error"]["code"] == "NEED_SETUP"


@pytest.mark.parametrize("name", ["../x", "has space", "a/b"])
def test_env_bad_name_is_bad_input(capsys, name):
    code, out = _run(capsys, ["farm", "account", "env", name])
    assert code == 1
    assert out["error"]["code"] == "BAD_INPUT"


def test_env_row_without_proxy_omits_proxy_var(capsys):
    farm.add_account("noproxy", "http://p:1")
    with closing(farm.connect()) as conn, conn:
        conn.execute("UPDATE accounts SET proxy = '' WHERE name = 'noproxy'")
    code, out = _run(capsys, ["farm", "account", "env", "noproxy"])
    assert code == 0
    env = out["result"]
    assert "REACH_BROWSER_PROXY" not in env
    assert env["REACH_ACCOUNT"] == "noproxy"
    assert Path(env["HOME"]).is_absolute()


def test_dirs_are_private_0700():
    farm.add_account("perm", "http://p:1")
    root = farm.farm_root()
    for path in (state.root(), root, root / "accounts", root / "accounts" / "perm"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_registry_db_is_owner_only_0600():
    farm.add_account("fileperm", "http://p:1")
    assert stat.S_IMODE(farm.registry_path().stat().st_mode) == 0o600


def test_registry_path_and_wal_mode():
    farm.add_account("wal", "socks5://h:1080")
    assert farm.registry_path() == state.root() / "farm" / "registry.db"
    assert farm.registry_path().is_file()
    conn = sqlite3.connect(farm.registry_path())
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_module_add_list_direct():
    row = farm.add_account("direct", "socks5://h:1080")
    assert set(row) == ACCOUNT_FIELDS | {"seed"}
    assert row["ban_status"] == "new"
    rows = farm.list_accounts()
    assert [r["name"] for r in rows] == ["direct"]


def test_ledger_happy_path_via_cli(capsys):
    farm.add_account("test1", "http://p1:8080")
    for outcome in ("success", "success", "success", "RATE_LIMITED", "TIMEOUT"):
        farm.record_action("test1", "post", outcome)
    code, out = _run(capsys, ["farm", "account", "ledger", "test1"])
    assert code == 0
    assert out["ok"] is True
    assert out["action"] == "farm account ledger"
    result = out["result"]
    assert result["account"] == "test1"
    assert result["window_hours"] == 24
    assert result["success"] == 3
    assert result["attempts"] == 5
    assert result["by_class"] == {"post": {"success": 3, "attempts": 5}}
    json.dumps(out)


def test_ledger_by_class_groups_classes(capsys):
    farm.add_account("multi", "http://p:1")
    farm.record_action("multi", "post", "success")
    farm.record_action("multi", "post", "success")
    farm.record_action("multi", "reply", "success")
    farm.record_action("multi", "reply", "PLATFORM_BLOCKED")
    farm.record_action("multi", "like", "NEED_LOGIN")
    code, out = _run(capsys, ["farm", "account", "ledger", "multi"])
    assert code == 0
    result = out["result"]
    assert result["by_class"] == {
        "post": {"success": 2, "attempts": 2},
        "reply": {"success": 1, "attempts": 2},
        "like": {"success": 0, "attempts": 1},
    }
    assert result["success"] == 3
    assert result["attempts"] == 5


def test_ledger_empty_account_zeros(capsys):
    farm.add_account("empty", "http://p:1")
    code, out = _run(capsys, ["farm", "account", "ledger", "empty"])
    assert code == 0
    result = out["result"]
    assert result["success"] == 0
    assert result["attempts"] == 0
    assert result["by_class"] == {}


def test_ledger_excludes_rows_older_than_24h(capsys):
    farm.add_account("stale", "http://p:1")
    farm.record_action("stale", "post", "success")
    old_ts = int(time.time()) - 90000
    with closing(farm.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO actions_log (account, ts, action_class, outcome) "
            "VALUES (?, ?, ?, ?)",
            ("stale", old_ts, "post", "success"),
        )
        conn.execute(
            "INSERT INTO actions_log (account, ts, action_class, outcome) "
            "VALUES (?, ?, ?, ?)",
            ("stale", old_ts, "like", "RATE_LIMITED"),
        )
    code, out = _run(capsys, ["farm", "account", "ledger", "stale"])
    assert code == 0
    result = out["result"]
    assert result["success"] == 1
    assert result["attempts"] == 1
    assert result["by_class"] == {"post": {"success": 1, "attempts": 1}}


def test_ledger_does_not_count_other_accounts(capsys):
    farm.add_account("mine", "http://p:1")
    farm.add_account("other", "http://p:2")
    farm.record_action("other", "post", "success")
    farm.record_action("other", "post", "success")
    code, out = _run(capsys, ["farm", "account", "ledger", "mine"])
    assert code == 0
    assert out["result"]["success"] == 0
    assert out["result"]["attempts"] == 0


def test_ledger_unknown_account_is_need_setup(capsys):
    code, out = _run(capsys, ["farm", "account", "ledger", "ghost"])
    assert code == 1
    assert out["ok"] is False
    assert out["error"]["code"] == NEED_SETUP


def test_record_action_unknown_account_is_need_setup():
    farm.add_account("ok", "http://p:1")
    with pytest.raises(ReachError) as excinfo:
        farm.record_action("ghost", "post", "success")
    assert excinfo.value.code == NEED_SETUP


@pytest.mark.parametrize("name", ["../x", "has space", "a/b", ""])
def test_ledger_bad_name_is_bad_input(capsys, name):
    code, out = _run(capsys, ["farm", "account", "ledger", name])
    assert code == 1
    assert out["error"]["code"] == BAD_INPUT


@pytest.mark.parametrize("action_class,outcome", [
    ("", "success"), ("post", ""), (None, "success"), ("post", None),
    (123, "success"), ("post", 0),
])
def test_record_action_bad_class_or_outcome(action_class, outcome):
    farm.add_account("ok", "http://p:1")
    with pytest.raises(ReachError) as excinfo:
        farm.record_action("ok", action_class, outcome)
    assert excinfo.value.code == BAD_INPUT


@pytest.mark.parametrize("argv", [
    ["farm", "account", "ledger"],
    ["farm", "account", "ledger", "a", "b"],
    ["farm", "account", "ledger", "a", "--proxy", "http://p:1"],
])
def test_ledger_usage_errors_are_bad_input(capsys, argv):
    code, out = _run(capsys, argv)
    assert code == 1
    assert out["error"]["code"] == BAD_INPUT


def test_record_action_returns_inserted_row():
    farm.add_account("ret", "http://p:1")
    row = farm.record_action("ret", "reply", "success")
    assert row["account"] == "ret"
    assert row["action_class"] == "reply"
    assert row["outcome"] == "success"
    assert isinstance(row["ts"], int) and row["ts"] > 0


def test_locked_db_surfaces_upstream_broken(monkeypatch):
    def _locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(farm, "connect", _locked)
    calls = (
        lambda: farm.add_account("a1", "http://p:1"),
        farm.list_accounts,
        lambda: farm.account_env("a1"),
        lambda: farm.record_action("a1", "post", "success"),
        lambda: farm.ledger_24h("a1"),
    )
    for call in calls:
        with pytest.raises(ReachError) as excinfo:
            call()
        assert excinfo.value.code == UPSTREAM_BROKEN


def test_connect_sets_busy_timeout_and_actions_log_schema():
    farm.add_account("bt", "http://p:1")
    with closing(farm.connect()) as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            ).fetchall()
        }
    assert "accounts" in tables
    assert "actions_log" in tables
    assert "actions_log_account_class_ts" in tables


def test_add_rejects_portless_proxy(capsys):
    code, out = _run(capsys, ["farm", "account", "add", "x1", "--proxy", "http://host"])
    assert code == 1
    assert out["ok"] is False
    assert "BAD_INPUT" in str(out["error"])


def test_record_action_rejects_bad_outcome_vocabulary():
    farm.add_account("oc", "http://p:1")
    farm.record_action("oc", "post", "success")
    farm.record_action("oc", "post", "RATE_LIMITED")
    with pytest.raises(ReachError) as ei:
        farm.record_action("oc", "post", "custom-error")
    assert ei.value.code == BAD_INPUT


def test_farm_root_honors_reach_farm_root(monkeypatch):
    monkeypatch.setenv("REACH_FARM_ROOT", "/tmp/farm-root-override")
    assert farm.farm_root() == Path("/tmp/farm-root-override")


def test_connect_surfaces_upstream_broken_when_farm_root_is_file():
    fr = farm.farm_root()
    fr.parent.mkdir(parents=True, exist_ok=True)
    fr.write_text("not a dir")
    with pytest.raises(ReachError) as ei:
        farm.connect()
    assert ei.value.code == UPSTREAM_BROKEN


def test_account_env_rejects_symlinked_home(tmp_path):
    row = farm.add_account("sym1", "http://p:1")
    home = Path(row["home"])
    home.rmdir()
    target = tmp_path / "realhome"
    target.mkdir()
    home.symlink_to(target)
    with pytest.raises(ReachError) as ei:
        farm.account_env("sym1")
    assert ei.value.code == UPSTREAM_BROKEN

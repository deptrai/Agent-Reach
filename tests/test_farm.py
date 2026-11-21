# -*- coding: utf-8 -*-
"""Farm registry: SQLite-WAL accounts table and the `farm` CLI surface."""

import json
import sqlite3
import stat

import pytest

from agent_reach.runtime import cli as unified
from agent_reach.runtime import farm, state

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
    ["farm", "account"],
    ["farm", "bogus"],
    ["farm"],
])
def test_missing_or_unknown_args_are_bad_input(capsys, argv):
    code, out = _run(capsys, argv)
    assert code == 1
    assert out["error"]["code"] == "BAD_INPUT"


@pytest.mark.parametrize("op", ["env", "ledger"])
def test_env_and_ledger_not_implemented(capsys, op):
    code, out = _run(capsys, ["farm", op])
    assert code == 1
    assert out["error"]["code"] == "BAD_INPUT"


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
    assert set(row) == ACCOUNT_FIELDS
    assert row["ban_status"] == "new"
    rows = farm.list_accounts()
    assert [r["name"] for r in rows] == ["direct"]

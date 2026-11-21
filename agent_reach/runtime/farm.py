"""Multi-account farm registry: a SQLite-WAL accounts table under state.root()/"farm".

Scope: add/list, per-account env resolution, and the actions_log ledger
(record_action/ledger_24h). No container/netns work, and no credential or
session copying — account rows plus an empty HOME skeleton.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

from agent_reach.utils.paths import ensure_no_symlink_path, make_private_dir

from . import state
from .result import BAD_INPUT, NEED_SETUP, UPSTREAM_BROKEN, ReachError

_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}")
_PROXY_SCHEMES = frozenset({"http", "https", "socks5"})

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS accounts ("
    "name TEXT PRIMARY KEY, "
    "proxy TEXT NOT NULL, "
    "home TEXT NOT NULL, "
    "age INTEGER NOT NULL, "
    "ban_status TEXT NOT NULL DEFAULT 'new', "
    "created_at INTEGER NOT NULL, "
    "updated_at INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS actions_log ("
    "account TEXT NOT NULL REFERENCES accounts(name), "
    "ts INTEGER NOT NULL, "
    "action_class TEXT NOT NULL, "
    "outcome TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS actions_log_account_ts ON actions_log (account, ts)",
)

_COLUMNS = "name, proxy, home, age, ban_status, created_at, updated_at"


def farm_root() -> Path:
    return state.root() / "farm"


def registry_path() -> Path:
    return farm_root() / "registry.db"


def connect() -> sqlite3.Connection:
    """Open the registry (creating dirs lazily) in WAL mode with the schema applied."""
    make_private_dir(state.root())
    make_private_dir(farm_root())
    db = registry_path()
    ensure_no_symlink_path(db, "registry")
    conn = sqlite3.connect(db)
    try:
        mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        if mode != "wal":
            raise ReachError(UPSTREAM_BROKEN, "registry cannot use WAL on this filesystem")
        conn.execute("PRAGMA busy_timeout=5000")
        for stmt in _SCHEMA:
            conn.execute(stmt)
        if sys.platform != "win32":
            os.chmod(db, 0o600)
    except Exception:
        conn.close()
        raise
    return conn


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ReachError(
            BAD_INPUT,
            f"invalid account name: {name!r}",
            hint="Use 1-32 chars: letters, digits, '_' or '-', starting with a letter or digit.",
        )


def _validate_proxy(proxy: str) -> None:
    try:
        parsed = urlparse(proxy or "")
        ok = parsed.scheme in _PROXY_SCHEMES and bool(parsed.hostname)
        parsed.port  # .port raises ValueError on a non-numeric or out-of-range port
    except ValueError:
        ok = False
    if not ok:
        raise ReachError(
            BAD_INPUT,
            f"invalid proxy URL: {proxy!r}",
            hint="Use a URL like http://host:port or socks5://host:port.",
        )


def account_home(name: str) -> Path:
    """Create farm/accounts/<name>/ (0700 at every level) and return the leaf."""
    accounts = make_private_dir(farm_root() / "accounts")
    return make_private_dir(accounts / name)


def add_account(name: str, proxy: str) -> dict:
    """Validate input, create the account HOME skeleton, and insert the registry row."""
    _validate_name(name)
    _validate_proxy(proxy)
    home = farm_root() / "accounts" / name
    created = not home.exists()
    home = account_home(name)
    now = int(time.time())

    def _insert() -> None:
        with closing(connect()) as conn:
            with conn:
                conn.execute(
                    f"INSERT INTO accounts ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (name, proxy, str(home), now, "new", now, now),
                )

    try:
        _with_busy_retry(_insert)
    except sqlite3.IntegrityError:
        raise ReachError(BAD_INPUT, f"farm account already exists: {name}")
    except Exception:
        if created:
            shutil.rmtree(home, ignore_errors=True)
        raise
    return {
        "name": name,
        "proxy": proxy,
        "home": str(home),
        "age": now,
        "ban_status": "new",
        "created_at": now,
        "updated_at": now,
    }


def list_accounts() -> list:
    """Every registry row as a JSON-safe dict (empty list when the table is empty)."""

    def _query():
        with closing(connect()) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                f"SELECT {_COLUMNS} FROM accounts ORDER BY name"
            ).fetchall()

    return [dict(row) for row in _with_busy_retry(_query)]


def account_env(name: str) -> dict:
    """Resolve the env dict dispatch applies for one registered account.

    HOME is the registry `home` column made absolute (no symlink resolution, so
    it stays identical to what `add`/`list` report); REACH_BROWSER_PROXY is only
    present when the row stores a non-empty proxy. Unknown account -> NEED_SETUP.
    """
    _validate_name(name)

    def _query():
        with closing(connect()) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT proxy, home FROM accounts WHERE name = ?", (name,)
            ).fetchone()

    row = _with_busy_retry(_query)
    if row is None:
        raise ReachError(
            NEED_SETUP,
            f"unknown farm account: {name}",
            hint=f"Run `agent-reach farm account add {name} --proxy <url>` first.",
        )
    env = {"HOME": str(Path(row["home"] or farm_root() / "accounts" / name).absolute())}
    if row["proxy"]:
        env["REACH_BROWSER_PROXY"] = row["proxy"]
    env["REACH_ACCOUNT"] = name
    return env


_BUSY_RETRIES = 3


def _with_busy_retry(fn, what: str = "registry"):
    """Run fn(); on SQLITE_BUSY after busy_timeout, retry a bounded number of times."""
    last = None
    for _ in range(_BUSY_RETRIES + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            last = exc
            time.sleep(0.05)
    raise ReachError(UPSTREAM_BROKEN, f"{what} stayed busy: {last}")


def _require_account(conn: sqlite3.Connection, name: str) -> None:
    row = conn.execute("SELECT 1 FROM accounts WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ReachError(
            NEED_SETUP,
            f"unknown farm account: {name}",
            hint=f"Run `agent-reach farm account add {name} --proxy <url>` first.",
        )


def record_action(account: str, action_class: str, outcome: str) -> dict:
    """Append one actions_log row for a registered account (one short transaction)."""
    _validate_name(account)
    if not isinstance(action_class, str) or not action_class:
        raise ReachError(BAD_INPUT, "action_class must be a non-empty string")
    if not isinstance(outcome, str) or not outcome:
        raise ReachError(BAD_INPUT, "outcome must be a non-empty string")
    now = int(time.time())

    def _insert() -> None:
        with closing(connect()) as conn:
            with conn:
                _require_account(conn, account)
                conn.execute(
                    "INSERT INTO actions_log (account, ts, action_class, outcome) "
                    "VALUES (?, ?, ?, ?)",
                    (account, now, action_class, outcome),
                )

    _with_busy_retry(_insert)
    return {"account": account, "ts": now, "action_class": action_class, "outcome": outcome}


def ledger_24h(account: str) -> dict:
    """Rolling-24h action counts for one registered account.

    Only outcome='success' rows count toward `success`; every row in the window
    counts toward `attempts`. Shape: {"account", "window_hours", "success",
    "attempts", "by_class": {<class>: {"success", "attempts"}}}.
    """
    _validate_name(account)
    since = int(time.time()) - 86400

    def _query():
        with closing(connect()) as conn:
            conn.row_factory = sqlite3.Row
            _require_account(conn, account)
            return conn.execute(
                "SELECT action_class, outcome, COUNT(*) AS n FROM actions_log "
                "WHERE account = ? AND ts >= ? GROUP BY action_class, outcome",
                (account, since),
            ).fetchall()

    rows = _with_busy_retry(_query, "farm account ledger")
    success = attempts = 0
    by_class = {}
    for row in rows:
        n = row["n"]
        bucket = by_class.setdefault(row["action_class"], {"success": 0, "attempts": 0})
        bucket["attempts"] += n
        attempts += n
        if row["outcome"] == "success":
            bucket["success"] += n
            success += n
    return {
        "account": account,
        "window_hours": 24,
        "success": success,
        "attempts": attempts,
        "by_class": by_class,
    }

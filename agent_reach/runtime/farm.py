"""Multi-account farm registry: a SQLite-WAL accounts table under state.root()/"farm".

Story 1 scope: add/list only. No ledger table, no container/netns work, and no
credential or session copying — account rows plus an empty HOME skeleton.
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
from .result import BAD_INPUT, UPSTREAM_BROKEN, ReachError

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
    "updated_at INTEGER NOT NULL)"
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
        conn.execute(_SCHEMA)
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
    with closing(connect()) as conn:
        try:
            with conn:
                conn.execute(
                    f"INSERT INTO accounts ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (name, proxy, str(home), now, "new", now, now),
                )
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
    with closing(connect()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT {_COLUMNS} FROM accounts ORDER BY name").fetchall()
    return [dict(row) for row in rows]

"""Identity bridge: Reach farm registry <-> XActions accountPool (story 16.1).

Each row of the registry's `accounts` table may be linked to exactly one entry
in the XActions pool store (`$XACTIONS_HOME/accounts.db`, SQLite WAL — the same
file `accountPool.js` opens). The mapping lives in the bridge-owned
`xactions_link` table inside the farm registry DB, so the registry stays the
single source of truth and no shadow account system is created.

Contract: docs/farm-xactions-bridge.md.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from agent_reach.utils.paths import (
    PrivatePathError,
    ensure_no_symlink_path,
    home_dir,
    make_private_dir,
)

from . import farm
from .result import BAD_INPUT, NEED_SETUP, UPSTREAM_BROKEN, ReachError

_DEFAULT_ID_PREFIX = "reach:"
_POOL_DB_FILENAME = "accounts.db"

# Same DDL as XActions accountPool.js openDatabase() — kept column-identical so
# a bridge-created store is opened cleanly by the real pool afterwards.
_POOL_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS accounts ("
    "name        TEXT PRIMARY KEY, "
    "cookies     TEXT NOT NULL, "
    "proxy       TEXT, "
    "locked      INTEGER NOT NULL DEFAULT 0, "
    "lock_reason TEXT, "
    "locked_at   INTEGER, "
    "last_used   INTEGER NOT NULL DEFAULT 0, "
    "added_at    INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS rate_limits ("
    "account     TEXT NOT NULL, "
    "operation   TEXT NOT NULL, "
    "remaining   INTEGER, "
    "reset_at    INTEGER, "
    "updated_at  INTEGER NOT NULL, "
    "PRIMARY KEY (account, operation), "
    "FOREIGN KEY (account) REFERENCES accounts(name) ON DELETE CASCADE)",
)

_LINK_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS xactions_link ("
    "reach_name      TEXT PRIMARY KEY REFERENCES accounts(name), "
    "xactions_id     TEXT NOT NULL, "
    "provisioned_at  REAL, "
    "ban_status      TEXT, "
    "ban_reason      TEXT, "
    "ban_checked_at  REAL, "
    "updated_at      REAL NOT NULL)",
)

_AUTH_TOKEN_RE = re.compile(r"(^|;\s*)auth_token=")

#: lock_reason stamped on link()-created placeholders; sync_bans must not read
#: "not provisioned yet" as an XActions-detected ban.
PLACEHOLDER_LOCK_REASON = "awaiting_session"


def xactions_home() -> Path:
    """`$XACTIONS_HOME`, else `~/.xactions` — mirroring XActions resolveCacheDir()."""
    override = os.environ.get("XACTIONS_HOME")
    if override:
        return Path(override)
    return home_dir() / ".xactions"


def pool_path() -> Path:
    return xactions_home() / _POOL_DB_FILENAME


def default_xactions_id(reach_name: str) -> str:
    return f"{_DEFAULT_ID_PREFIX}{reach_name}"


# ---------------------------------------------------------------------------
# Pool adapter — speaks the real accountPool.js `accounts` table.
# ---------------------------------------------------------------------------


def _chmod_owner_only(path: Path) -> None:
    if sys.platform == "win32":
        return
    try:
        os.chmod(path, 0o600)
    except FileNotFoundError:
        pass


def _connect_pool() -> sqlite3.Connection:
    """Open the XActions pool store in WAL mode with its schema applied."""
    try:
        make_private_dir(xactions_home())
        db = pool_path()
        ensure_no_symlink_path(db, "xactions pool")
        conn = sqlite3.connect(db)
        try:
            mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise ReachError(UPSTREAM_BROKEN, "xactions pool cannot use WAL on this filesystem")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            for stmt in _POOL_SCHEMA:
                conn.execute(stmt)
            # The real pool adds `lock_until` via ALTER; apply the same so old
            # stores and new ones share one column set.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
            if "lock_until" not in columns:
                conn.execute("ALTER TABLE accounts ADD COLUMN lock_until INTEGER")
            _chmod_owner_only(db)
            for suffix in ("-wal", "-shm"):
                _chmod_owner_only(Path(str(db) + suffix))
        except Exception:
            conn.close()
            raise
    except ReachError:
        raise
    except (OSError, PrivatePathError, sqlite3.Error) as exc:
        raise ReachError(UPSTREAM_BROKEN, f"xactions pool unavailable: {exc}") from exc
    return conn


class PoolEntry:
    """Row shape the bridge reads/writes; secret `cookies` stays out of reprs."""

    __slots__ = ("name", "proxy", "locked", "lock_reason", "locked_at", "lock_until")

    def __init__(self, row: sqlite3.Row):
        self.name = row["name"]
        self.proxy = row["proxy"]
        self.locked = bool(row["locked"])
        self.lock_reason = row["lock_reason"]
        self.locked_at = row["locked_at"]
        self.lock_until = row["lock_until"]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "proxy": self.proxy,
            "locked": self.locked,
            "lock_reason": self.lock_reason,
            "locked_at": self.locked_at,
            "lock_until": self.lock_until,
        }


def _ms() -> int:
    # Pool timestamps follow Date.now() (ms) like the JS store.
    return int(time.time() * 1000)


class XActionsPool:
    """Minimal adapter over the real pool `accounts` table (get/add/update/remove)."""

    def get(self, xactions_id: str) -> PoolEntry | None:
        with closing(_connect_pool()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM accounts WHERE name = ?", (xactions_id,)).fetchone()
        return PoolEntry(row) if row else None

    def add(self, xactions_id: str, *, cookies: str, proxy: str | None = None, locked: bool = False,
            lock_reason: str | None = None) -> PoolEntry:
        """Insert a pool entry. `locked=True` parks it out of rotation (provision placeholder)."""
        now = _ms()

        def _insert() -> None:
            with closing(_connect_pool()) as conn:
                with conn:
                    conn.execute(
                        "INSERT INTO accounts (name, cookies, proxy, locked, lock_reason, "
                        "locked_at, last_used, added_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                        (xactions_id, cookies, proxy, int(locked),
                         (lock_reason or "locked") if locked else None,
                         now if locked else None, now),
                    )

        _with_busy_retry(_insert, "xactions pool")
        entry = self.get(xactions_id)
        assert entry is not None
        return entry

    def update(self, xactions_id: str, *, cookies: str | None = None, proxy: str | None = None,
               locked: bool | None = None, lock_reason: str | None = None,
               lock_until_ms: int | None = None) -> None:
        def _update() -> None:
            with closing(_connect_pool()) as conn:
                with conn:
                    if cookies is not None:
                        conn.execute("UPDATE accounts SET cookies = ? WHERE name = ?",
                                     (cookies, xactions_id))
                    if proxy is not None:
                        conn.execute("UPDATE accounts SET proxy = ? WHERE name = ?",
                                     (proxy, xactions_id))
                    if locked is not None:
                        if locked:
                            conn.execute(
                                "UPDATE accounts SET locked = 1, lock_reason = ?, "
                                "locked_at = ?, lock_until = ? WHERE name = ?",
                                (lock_reason or "locked", _ms(), lock_until_ms, xactions_id))
                        else:
                            conn.execute(
                                "UPDATE accounts SET locked = 0, lock_reason = NULL, "
                                "locked_at = NULL, lock_until = NULL WHERE name = ?",
                                (xactions_id,))

        _with_busy_retry(_update, "xactions pool")

    def remove(self, xactions_id: str) -> bool:
        def _remove() -> int:
            with closing(_connect_pool()) as conn:
                with conn:
                    return conn.execute("DELETE FROM accounts WHERE name = ?",
                                        (xactions_id,)).rowcount

        return _with_busy_retry(_remove, "xactions pool") > 0


#: Default adapter instance; swap with a stub in tests that avoid SQLite.
pool = XActionsPool()


# ---------------------------------------------------------------------------
# Link table (inside the farm registry DB; bridge-owned)
# ---------------------------------------------------------------------------


def _connect_registry() -> sqlite3.Connection:
    conn = farm.connect()
    try:
        for stmt in _LINK_SCHEMA:
            conn.execute(stmt)
    except Exception:
        conn.close()
        raise
    return conn


def _with_busy_retry(fn, what: str = "registry"):
    # Same bounded retry as farm._with_busy_retry; kept local so the bridge does
    # not depend on farm internals that another story may be editing.
    last = None
    for _ in range(4):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise ReachError(UPSTREAM_BROKEN, f"{what} failed: {exc}") from exc
            last = exc
            time.sleep(0.05)
    raise ReachError(UPSTREAM_BROKEN, f"{what} stayed busy: {last}")


_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}")


def _require_reach_account(conn: sqlite3.Connection, reach_name: str) -> None:
    if not isinstance(reach_name, str) or not _NAME_RE.fullmatch(reach_name):
        raise ReachError(BAD_INPUT, f"invalid account name: {reach_name!r}")
    row = conn.execute("SELECT 1 FROM accounts WHERE name = ?", (reach_name,)).fetchone()
    if row is None:
        raise ReachError(
            NEED_SETUP,
            f"unknown farm account: {reach_name}",
            hint=f"Run `agent-reach farm account add {reach_name} --proxy <url>` first.",
        )


def _link_row(reach_name: str) -> dict | None:
    def _query():
        with closing(_connect_registry()) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM xactions_link WHERE reach_name = ?", (reach_name,)
            ).fetchone()

    row = _with_busy_retry(_query)
    return dict(row) if row else None


def link(reach_name: str, xactions_id: str | None = None, *, proxy: str | None = None) -> dict:
    """Associate a farm account with an XActions pool entry.

    `xactions_id` defaults to `reach:<name>`. When the entry does not exist in
    the pool it is provisioned as a *locked placeholder* (`cookies=''`,
    `lock_reason='awaiting_session'`) so the mapping is real but the entry can
    never serve traffic until `provision_session` unlocks it. Re-linking to the
    same id is idempotent; re-linking to a different id re-points the mapping
    without touching either pool entry.
    """
    target = xactions_id or default_xactions_id(reach_name)
    now = time.time()

    def _upsert() -> None:
        with closing(_connect_registry()) as conn:
            with conn:
                _require_reach_account(conn, reach_name)
                conn.execute(
                    "INSERT INTO xactions_link (reach_name, xactions_id, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(reach_name) DO UPDATE SET "
                    "xactions_id = excluded.xactions_id, updated_at = excluded.updated_at",
                    (reach_name, target, now),
                )

    _with_busy_retry(_upsert)
    if pool.get(target) is None:
        pool.add(target, cookies="", proxy=proxy, locked=True,
                 lock_reason=PLACEHOLDER_LOCK_REASON)
    return {"reach_name": reach_name, "xactions_id": target, "linked": True}


def unlink(reach_name: str) -> dict:
    """Drop the mapping. The pool entry itself is left for the operator."""
    def _delete() -> int:
        with closing(_connect_registry()) as conn:
            with conn:
                _require_reach_account(conn, reach_name)
                return conn.execute(
                    "DELETE FROM xactions_link WHERE reach_name = ?", (reach_name,)
                ).rowcount

    removed = _with_busy_retry(_delete) > 0
    return {"reach_name": reach_name, "unlinked": removed}


def resolve(reach_name: str) -> str | None:
    """The linked XActions pool entry id, or None when unlinked."""
    row = _link_row(reach_name)
    return row["xactions_id"] if row else None


def _normalize_cookies(session_blob) -> str:
    """Mirror XActions normalizeCookies(): header string, [{name,value}], or map."""
    if isinstance(session_blob, str):
        text = session_blob.strip()
        if not text:
            raise ReachError(BAD_INPUT, "session_blob cookie string is empty")
        return text
    if isinstance(session_blob, (list, tuple)):
        return "; ".join(
            f"{c['name']}={c['value']}" for c in session_blob
            if isinstance(c, dict) and c.get("name") and c.get("value") is not None
        )
    if isinstance(session_blob, dict):
        return "; ".join(
            f"{k}={v}" for k, v in session_blob.items() if v is not None and v != ""
        )
    raise ReachError(BAD_INPUT, "unsupported session_blob: pass a cookie string, "
                                "a list of {name, value}, or a map")


def provision_session(reach_name: str, session_blob) -> dict:
    """Write session cookies into the linked pool entry and unlock it.

    Never logs or returns the secret material. The blob must carry an
    `auth_token` cookie — the same requirement the real pool's add() enforces.
    """
    cookies = _normalize_cookies(session_blob)
    if not _AUTH_TOKEN_RE.search(cookies):
        raise ReachError(
            BAD_INPUT,
            "session_blob has no auth_token cookie; the pool entry cannot make "
            "authenticated calls",
        )
    xid = resolve(reach_name)
    if xid is None:
        raise ReachError(
            NEED_SETUP,
            f"farm account not linked to XActions: {reach_name}",
            hint="Run bridge.link() first.",
        )
    pool.update(xid, cookies=cookies, locked=False)
    now = time.time()

    def _stamp() -> None:
        with closing(_connect_registry()) as conn:
            with conn:
                conn.execute(
                    "UPDATE xactions_link SET provisioned_at = ?, updated_at = ? "
                    "WHERE reach_name = ?",
                    (now, now, reach_name),
                )

    _with_busy_retry(_stamp)
    return {"reach_name": reach_name, "xactions_id": xid, "provisioned": True}


def record_ban(reach_name: str, status: str, reason: str = "") -> dict:
    """Write-back of an XActions-detected ban into the bridge row and the
    registry `accounts.ban_status` lifecycle field."""
    if not isinstance(status, str) or not status:
        raise ReachError(BAD_INPUT, "ban status must be a non-empty string")
    now = time.time()

    def _write() -> None:
        with closing(_connect_registry()) as conn:
            with conn:
                _require_reach_account(conn, reach_name)
                # Registry ban_status is always updated. The bridge row is only
                # stamped when a link exists — a write-back must not fabricate a
                # mapping to a pool entry that may not exist.
                conn.execute(
                    "UPDATE xactions_link SET ban_status = ?, ban_reason = ?, "
                    "ban_checked_at = ?, updated_at = ? WHERE reach_name = ?",
                    (status, reason or None, now, now, reach_name),
                )
                conn.execute(
                    "UPDATE accounts SET ban_status = ?, updated_at = ? WHERE name = ?",
                    (status, int(now), reach_name),
                )

    _with_busy_retry(_write)
    return {"reach_name": reach_name, "ban_status": status, "recorded": True}


def sync_bans() -> list[dict]:
    """Copy pool lock state into the registry for every linked account.

    XActions marks a pool entry `locked` when it sees a dead-account error
    (suspended / locked / dead session). This fold reads each linked entry and
    calls `record_ban` for the ones the pool reports as locked.
    """
    def _rows() -> list:
        with closing(_connect_registry()) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT reach_name, xactions_id FROM xactions_link"
            ).fetchall()]

    out = []
    for link_row in _with_busy_retry(_rows):
        entry = pool.get(link_row["xactions_id"])
        if entry is None or not entry.locked:
            continue
        if entry.lock_reason == PLACEHOLDER_LOCK_REASON:
            continue  # linked but never provisioned — parked, not banned
        out.append(record_ban(
            link_row["reach_name"],
            "banned",
            entry.lock_reason or "locked by XActions pool",
        ))
    return out

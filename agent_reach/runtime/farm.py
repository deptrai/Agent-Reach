"""Multi-account farm registry: a SQLite-WAL accounts table under state.root()/"farm".

Scope: add/list, per-account env resolution, the actions_log ledger
(record_action/ledger_24h), quota reservation, and onboarding seed
(seed_account_home — small runtime files into the account HOME; binaries stay
shared via absolute paths, session cookies only via --share-session).
No container/netns work.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from agent_reach.utils.paths import (
    PrivatePathError,
    ensure_no_symlink_path,
    make_private_dir,
)

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
    "CREATE INDEX IF NOT EXISTS actions_log_account_class_ts ON actions_log (account, action_class, ts)",
)

_COLUMNS = "name, proxy, home, age, ban_status, created_at, updated_at"


def farm_root() -> Path:
    # The operator registry lives at $HOME/.agent-reach/farm. Dispatch pins it
    # via REACH_FARM_ROOT while a run window swaps HOME to an account home —
    # otherwise farm.* calls inside the window resolve a second, empty
    # registry under the account's own .agent-reach.
    override = os.environ.get("REACH_FARM_ROOT")
    if override:
        return Path(override)
    return state.root() / "farm"


def registry_path() -> Path:
    return farm_root() / "registry.db"


def connect() -> sqlite3.Connection:
    """Open the registry (creating dirs lazily) in WAL mode with the schema applied."""
    try:
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
            conn.execute("PRAGMA foreign_keys=ON")
            for stmt in _SCHEMA:
                conn.execute(stmt)
            if sys.platform != "win32":
                os.chmod(db, 0o600)
                # WAL/SHM sidecars are created before this chmod and carry the
                # same rows — give them the same mode.
                for suffix in ("-wal", "-shm"):
                    try:
                        os.chmod(Path(str(db) + suffix), 0o600)
                    except FileNotFoundError:
                        pass
        except Exception:
            conn.close()
            raise
    except ReachError:
        raise
    except (OSError, PrivatePathError, sqlite3.Error) as exc:
        raise ReachError(UPSTREAM_BROKEN, f"farm registry unavailable: {exc}") from exc
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
        ok = parsed.scheme in _PROXY_SCHEMES and bool(parsed.hostname) and parsed.port is not None
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


def add_account(name: str, proxy: str, *, seed: bool = True, share_session: bool = False) -> dict:
    """Validate input, create the account HOME skeleton, and insert the registry row.

    When `seed` is true (default), the operator's browser runtime is copied into
    the account HOME via `seed_account_home` — small files only, binaries stay
    shared. `share_session` additionally copies the operator's cookie snapshot
    (same logged-in identity — explicit opt-in).
    """
    _validate_name(name)
    _validate_proxy(proxy)
    home = farm_root() / "accounts" / name
    created = not home.exists()
    try:
        home = account_home(name)
    except (OSError, PrivatePathError) as exc:
        raise ReachError(UPSTREAM_BROKEN, f"cannot create account home: {exc}") from exc
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
        if created:
            shutil.rmtree(home, ignore_errors=True)
        raise ReachError(BAD_INPUT, f"farm account already exists: {name}")
    except Exception:
        if created:
            shutil.rmtree(home, ignore_errors=True)
        raise
    row = {
        "name": name,
        "proxy": proxy,
        "home": str(home),
        "age": now,
        "ban_status": "new",
        "created_at": now,
        "updated_at": now,
    }
    if not seed:
        return row
    try:
        row["seed"] = seed_account_home(name, share_session=share_session)
    except ReachError:
        raise
    except Exception as exc:
        # Seed is best-effort onboarding sugar — never fail the registry write.
        row["seed"] = {"account": name, "seeded": [], "error": str(exc)}
    return row


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
    home = Path(row["home"] or farm_root() / "accounts" / name)
    # The stored path was private when the row was written — re-validate so a
    # symlink swapped in later cannot steer HOME outside the farm tree.
    try:
        ensure_no_symlink_path(home, "account home")
    except PrivatePathError as exc:
        raise ReachError(UPSTREAM_BROKEN, f"account home is unsafe: {exc}") from exc
    env = {"HOME": str(home.absolute())}
    if row["proxy"]:
        env["REACH_BROWSER_PROXY"] = row["proxy"]
    env["REACH_ACCOUNT"] = name
    return env


def account_proxy(name: str) -> Optional[str]:
    """The registry proxy for one account, or None when the row stores none.

    Authoritative source for the dispatch kill-switch (contract §2 layer-1):
    the env var can be stale relative to the row. Raises the same errors as
    account_env — unknown → NEED_SETUP, bad name → BAD_INPUT.
    """
    _validate_name(name)

    def _query():
        with closing(connect()) as conn:
            return conn.execute(
                "SELECT proxy FROM accounts WHERE name = ?", (name,)
            ).fetchone()

    row = _with_busy_retry(_query)
    if row is None:
        raise ReachError(
            NEED_SETUP,
            f"unknown farm account: {name}",
            hint=f"Run `agent-reach farm account add {name} --proxy <url>` first.",
        )
    return row[0] or None


def account_age(name: str) -> Optional[int]:
    """The registry `age` column — the onboard unix epoch — or None.

    The warm-up ramp (contract §3, story 13.3) scales quota caps by account
    age. Rows repaired by hand or migrated before the column existed can
    store NULL/non-numeric text; those return None so the caller treats the
    account as mature instead of punishing an unmigrated row. Unknown
    account → NEED_SETUP, bad name → BAD_INPUT — same contract as
    account_proxy.
    """
    _validate_name(name)

    def _query():
        with closing(connect()) as conn:
            return conn.execute(
                "SELECT age FROM accounts WHERE name = ?", (name,)
            ).fetchone()

    row = _with_busy_retry(_query)
    if row is None:
        raise ReachError(
            NEED_SETUP,
            f"unknown farm account: {name}",
            hint=f"Run `agent-reach farm account add {name} --proxy <url>` first.",
        )
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


# ── browser runtime seed (farm onboarding) ─────────────────────────────────
#
# A farm action runs with HOME swapped to the account's private dir, so a fresh
# account would see an empty `.agent-reach` and fall into the full
# install/download path (browser setup + extension fetch + a NEED_SETUP trip).
# Seeding copies the SMALL files and records the operator's absolute binary
# paths — the Chromium tree and the launcher venv stay shared, nothing heavy is
# duplicated. The watcher pins CLOAKBROWSER_BINARY_PATH to that executable, so
# the SDK never re-downloads under the account home either.

_SEED_STATE_KEEP = frozenset({
    "backend", "executable", "launcher_python",
    "extension_asset", "extension_id", "extension_version",
    "profile_backend", "headless",
})

_SEED_FILES = (
    "cloak-binaries/license.key",
    "cloak-binaries/.free_claim",
    "cloak-binaries/.license_cache",
)


def _seed_copy(src_root: Path, dst_home: Path, rel: str, out: list) -> None:
    src = src_root / rel
    if not src.is_file():
        return
    dst = dst_home / ".agent-reach" / rel
    make_private_dir(dst.parent)
    shutil.copy2(src, dst)
    if sys.platform != "win32":
        os.chmod(dst, 0o600)
    out.append(rel)


def seed_account_home(name: str, *, share_session: bool = False) -> dict:
    """Seed the operator's browser runtime into a farm account's HOME.

    Copies the CloakBrowser license files, the OpenCLI extension bundle and a
    sanitized browser/state.json (binary paths only — no session/context
    fields). `browser/session.json` (the logged-in cookie snapshot) is NOT
    copied unless `share_session=True`: cloning the operator's identity into a
    farm account must be an explicit choice.

    Unknown account → NEED_SETUP. An operator with no browser runtime seeds
    nothing ({"seeded": []}) — onboarding still succeeds; the first action then
    goes through the normal install path.
    """
    home = Path(account_env(name)["HOME"])
    src_root = state.root()
    seeded: list = []

    for rel in _SEED_FILES:
        _seed_copy(src_root, home, rel, seeded)

    ext_src = src_root / "browser" / "extension"
    if ext_src.is_dir():
        dst = home / ".agent-reach" / "browser" / "extension"
        make_private_dir(dst.parent)
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(ext_src, dst)
        seeded.append("browser/extension")

    try:
        st = json.loads((src_root / "browser" / "state.json").read_text("utf-8"))
    except (OSError, ValueError):
        st = None
    if isinstance(st, dict) and st.get("backend"):
        keep = {k: st[k] for k in sorted(_SEED_STATE_KEEP) if k in st}
        dst = home / ".agent-reach" / "browser" / "state.json"
        make_private_dir(dst.parent)
        dst.write_text(json.dumps(keep, indent=2, sort_keys=True), "utf-8")
        if sys.platform != "win32":
            os.chmod(dst, 0o600)
        seeded.append("browser/state.json")

    if share_session:
        _seed_copy(src_root, home, "browser/session.json", seeded)

    return {"account": name, "seeded": seeded,
            "seed_source": str(src_root)}


_BUSY_RETRIES = 3
_OUTCOME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,39}")


def _with_busy_retry(fn, what: str = "registry"):
    """Run fn(); on SQLITE_BUSY after busy_timeout, retry a bounded number of times."""
    last = None
    for _ in range(_BUSY_RETRIES + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise ReachError(UPSTREAM_BROKEN, f"{what} failed: {exc}") from exc
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
    if not isinstance(outcome, str) or not (
        outcome == "success" or _OUTCOME_RE.fullmatch(outcome)
    ):
        raise ReachError(BAD_INPUT, "outcome must be 'success' or an error code (UPPER_SNAKE)")
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


# In-flight holds older than this no longer count: the process that reserved
# them is gone, and a stuck row must not freeze the cap forever.
_RESERVE_TTL = 900


class QuotaFull(Exception):
    """reserve_slots refused: `count` already-used slots vs `cap`."""

    def __init__(self, count: int, cap: int):
        self.count = count
        self.cap = cap


def reserve_slots(account: str, action_class: str, slots: int, cap: int) -> list:
    """Atomically hold `slots` ledger rows or raise QuotaFull.

    One BEGIN IMMEDIATE transaction counts 24h successes plus fresh `reserved`
    rows and inserts the holds. Two processes racing a cap of 1 cannot both
    pass: the second waits on the write lock and then sees the first hold.
    """
    _validate_name(account)
    slots = int(slots)
    if slots < 1:
        raise ReachError(BAD_INPUT, "reserve slots must be >= 1")
    cap = int(cap)

    def _tx() -> list:
        with closing(connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _require_account(conn, account)
                now = int(time.time())
                row = conn.execute(
                    "SELECT "
                    "SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN outcome = 'reserved' AND ts >= ? THEN 1 ELSE 0 END) "
                    "FROM actions_log WHERE account = ? AND action_class = ? AND ts >= ?",
                    (now - _RESERVE_TTL, account, action_class, now - 86400),
                ).fetchone()
                used = int(row[0] or 0) + int(row[1] or 0)
                if cap <= 0 or used + slots > cap:
                    conn.rollback()
                    raise QuotaFull(used, cap)
                ids = []
                for _ in range(slots):
                    cur = conn.execute(
                        "INSERT INTO actions_log (account, ts, action_class, outcome) "
                        "VALUES (?, ?, ?, 'reserved')",
                        (account, now, action_class),
                    )
                    ids.append(cur.lastrowid)
                conn.commit()
                return ids
            except QuotaFull:
                raise
            except Exception:
                conn.rollback()
                raise

    return _with_busy_retry(_tx, "quota reserve")


def settle_slots(rowids, *, successes: int, failure: Optional[str] = None) -> None:
    """Turn holds into real ledger rows: `successes` become success, one row
    records `failure` when set, leftover holds are deleted (they never posted)."""
    rowids = [r for r in (rowids or [])]
    successes = max(0, int(successes))
    if not rowids and successes <= 0 and not failure:
        return

    def _tx() -> None:
        with closing(connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                i = 0
                for _ in range(min(successes, len(rowids))):
                    conn.execute(
                        "UPDATE actions_log SET outcome = 'success' WHERE rowid = ?",
                        (rowids[i],),
                    )
                    i += 1
                if failure and i < len(rowids):
                    conn.execute(
                        "UPDATE actions_log SET outcome = ? WHERE rowid = ?",
                        (failure, rowids[i]),
                    )
                    i += 1
                elif failure and rowids:
                    src = conn.execute(
                        "SELECT account, action_class FROM actions_log WHERE rowid = ?",
                        (rowids[0],),
                    ).fetchone()
                    if src:
                        conn.execute(
                            "INSERT INTO actions_log (account, ts, action_class, outcome) "
                            "VALUES (?, ?, ?, ?)",
                            (src[0], int(time.time()), src[1], failure),
                        )
                for rid in rowids[i:]:
                    conn.execute(
                        "DELETE FROM actions_log WHERE rowid = ? AND outcome = 'reserved'",
                        (rid,),
                    )
                extra = successes - min(successes, len(rowids))
                if extra and rowids:
                    src = conn.execute(
                        "SELECT account, action_class FROM actions_log WHERE rowid = ?",
                        (rowids[0],),
                    ).fetchone()
                    if src:
                        now = int(time.time())
                        for _ in range(extra):
                            conn.execute(
                                "INSERT INTO actions_log (account, ts, action_class, outcome) "
                                "VALUES (?, ?, ?, 'success')",
                                (src[0], now, src[1]),
                            )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    _with_busy_retry(_tx, "quota settle")

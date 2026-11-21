"""Fleet manager: poll `agent-reach check` inside farm containers, populate a
runtime registry, and reopen the login window on NEED_LOGIN (story 14.4).

Discovery: ``docker ps -a --format json`` filtered to containers named
``farm-*`` or labelled ``com.agent-reach.farm=1`` (the compose service sets
both, plus ``com.agent-reach.account=$REACH_ACCOUNT``). Each running container
is polled with ``docker exec -u farm <name> agent-reach check`` — the same
JSON envelope the boot probe consumes — with a per-container budget of
``REACH_FLEET_TIMEOUT`` seconds (default 60). Results upsert the
``fleet_containers`` table inside the farm registry DB:

    name, container_id, account, proxy, image, session_state, ban_status,
    last_check_ts, last_error, pending_login, login_channel, updated_at

session_state ∈ {unknown, ok, NEED_LOGIN, degraded, unreachable, down}:

- ``ok``          — check envelope ``ok: true``; clears pending_login.
- ``NEED_LOGIN``  — a check result failed with code NEED_LOGIN (the channel is
                    kept in login_channel). On a fresh transition into this
                    state the poll fires ONE detached
                    ``docker exec -u farm -d <name> agent-reach login <channel>`` —
                    the onboarding/browser_watcher login flow opens the window
                    inside that container and a human completes it there —
                    then sets pending_login so later polls do not refire.
- ``degraded``    — check ran, failures are not NEED_LOGIN (codes in
                    last_error, e.g. PLATFORM_BLOCKED / UPSTREAM_BROKEN).
- ``unreachable`` — the exec itself failed, timed out, or emitted no JSON.
- ``down``        — container exists but is not running, or vanished from
                    ``docker ps`` since the previous poll.

``account`` comes from the ``com.agent-reach.account`` label or the ``farm-``
name prefix; ``proxy``/``ban_status`` are mirrored from the farm registry
accounts table when the account is registered (epic-11 bridge).

Cadence: ``agent-reach fleet poll --once`` for cron/manual runs, or
``agent-reach fleet poll`` looping every ``--interval`` / ``REACH_FLEET_POLL``
seconds (default 300). ``agent-reach fleet list`` is the one-place health
view; ``agent-reach fleet relogin <name> [--channel ch]`` re-fires the login
window by hand.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from typing import Mapping, Optional

from . import farm
from .result import BAD_INPUT, NEED_SETUP, UPSTREAM_BROKEN, ReachError, unix_to_iso

CONTAINER_PREFIX = "farm-"
FARM_LABEL = "com.agent-reach.farm"
ACCOUNT_LABEL = "com.agent-reach.account"

DEFAULT_INTERVAL = 300.0  # REACH_FLEET_POLL — 5min between poll cycles
DEFAULT_CHECK_TIMEOUT = 60.0  # REACH_FLEET_TIMEOUT — per-container exec budget

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS fleet_containers ("
    "name TEXT PRIMARY KEY, "
    "container_id TEXT, "
    "account TEXT, "
    "proxy TEXT, "
    "image TEXT, "
    "session_state TEXT NOT NULL DEFAULT 'unknown', "
    "ban_status TEXT, "
    "last_check_ts INTEGER, "
    "last_error TEXT, "
    "pending_login INTEGER NOT NULL DEFAULT 0, "
    "login_channel TEXT, "
    "updated_at INTEGER NOT NULL)",
)

_COLUMNS = (
    "name, container_id, account, proxy, image, session_state, ban_status, "
    "last_check_ts, last_error, pending_login, login_channel, updated_at"
)


def connect() -> sqlite3.Connection:
    """Open the farm registry DB (WAL, private) and apply the fleet schema."""
    conn = farm.connect()
    try:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def _float_env(env: Mapping[str, str], key: str, default: float, *, minimum: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ReachError(BAD_INPUT, f"{key} must be a number of seconds, got {raw!r}") from exc
    if value < minimum:
        raise ReachError(BAD_INPUT, f"{key} must be >= {minimum}, got {value}")
    return value


def check_timeout(env: Optional[Mapping[str, str]] = None) -> float:
    return _float_env(os.environ if env is None else env, "REACH_FLEET_TIMEOUT", DEFAULT_CHECK_TIMEOUT, minimum=1.0)


def poll_interval(env: Optional[Mapping[str, str]] = None) -> float:
    return _float_env(os.environ if env is None else env, "REACH_FLEET_POLL", DEFAULT_INTERVAL, minimum=1.0)


# ---------------------------------------------------------------------------
# Docker helpers (single seam for tests: _run)
# ---------------------------------------------------------------------------


def _run(argv: list, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _label_value(labels: str, key: str) -> Optional[str]:
    for pair in labels.split(","):
        if pair.strip().startswith(key + "="):
            return pair.split("=", 1)[1].strip() or None
    return None


def discover() -> list:
    """Farm containers from `docker ps -a --format json` (running and stopped)."""
    try:
        proc = _run(["docker", "ps", "-a", "--format", "json"], timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise ReachError(UPSTREAM_BROKEN, "docker ps timed out") from exc
    except FileNotFoundError as exc:
        raise ReachError(UPSTREAM_BROKEN, "docker CLI not found on this host") from exc
    if proc.returncode != 0:
        raise ReachError(UPSTREAM_BROKEN, f"docker ps failed: {(proc.stderr or '').strip()[:200]}")
    containers = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        name = entry.get("Names") or entry.get("Name") or ""
        labels = entry.get("Labels") or ""
        if not name.startswith(CONTAINER_PREFIX) and _label_value(labels, FARM_LABEL) != "1":
            continue
        account = _label_value(labels, ACCOUNT_LABEL)
        if account is None and name.startswith(CONTAINER_PREFIX):
            account = name[len(CONTAINER_PREFIX):]
        containers.append({
            "name": name,
            "container_id": entry.get("ID") or "",
            "image": entry.get("Image") or "",
            "state": (entry.get("State") or "").lower(),
            "account": account or "",
        })
    return containers


def _check(name: str, timeout: float) -> dict:
    """Run `agent-reach check` in one container; classify the JSON envelope.

    `-u farm` matters: story 14.3 keeps the container PID1 as root for the
    netns kill-switch, so a bare `docker exec` would land in /root — wrong
    HOME, empty .agent-reach, phantom NEED_SETUPs."""
    try:
        proc = _run(["docker", "exec", "-u", "farm", name, "agent-reach", "check"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"state": "unreachable", "channel": None, "error": f"check timed out after {timeout:g}s"}
    except OSError as exc:
        return {"state": "unreachable", "channel": None, "error": f"docker exec failed: {exc}"}
    payload = None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if payload is None:
        detail = (proc.stderr or proc.stdout).strip()[:200] or f"exit {proc.returncode}"
        return {"state": "unreachable", "channel": None, "error": f"no check JSON: {detail}"}
    if payload.get("ok"):
        return {"state": "ok", "channel": None, "error": None}
    codes: dict = {}
    for r in payload.get("results") or []:
        err = r.get("error") or {}
        code = err.get("code")
        if not r.get("passed") and code:
            codes.setdefault(code, []).append(f"{r.get('channel')}/{r.get('action')}")
    if "NEED_LOGIN" in codes:
        channel = codes["NEED_LOGIN"][0].split("/")[0]
        return {"state": "NEED_LOGIN", "channel": channel,
                "error": "needs login: " + ", ".join(sorted(codes["NEED_LOGIN"]))}
    summary = "; ".join(
        f"{code} at {', '.join(sorted(targets))}" for code, targets in sorted(codes.items())
    ) or (payload.get("error") or {}).get("message") or "check reported not ok"
    return {"state": "degraded", "channel": None, "error": str(summary)[:300]}


def _fire_relogin(name: str, channel: str, timeout: float) -> tuple:
    """Detached `docker exec -u farm -d <name> agent-reach login <channel>` → (ok, error)."""
    try:
        proc = _run(["docker", "exec", "-u", "farm", "-d", name, "agent-reach", "login", channel],
                    timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "docker exec -d timed out"
    except OSError as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, ((proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}")[:200]
    return True, ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _accounts() -> dict:
    """name → accounts row; empty when the registry is unavailable."""
    try:
        return {row["name"]: row for row in farm.list_accounts()}
    except ReachError:
        return {}


def _upsert(conn: sqlite3.Connection, name: str, fields: dict) -> None:
    base = {
        "container_id": None, "account": None, "proxy": None, "image": None,
        "session_state": "unknown", "ban_status": None, "last_check_ts": None,
        "last_error": None, "pending_login": 0, "login_channel": None,
    }
    base.update(fields)
    base["updated_at"] = int(time.time())
    conn.execute(
        f"INSERT INTO fleet_containers ({_COLUMNS}) "
        "VALUES (:name, :container_id, :account, :proxy, :image, :session_state, "
        ":ban_status, :last_check_ts, :last_error, :pending_login, :login_channel, :updated_at) "
        "ON CONFLICT(name) DO UPDATE SET "
        "container_id=excluded.container_id, account=excluded.account, proxy=excluded.proxy, "
        "image=excluded.image, session_state=excluded.session_state, ban_status=excluded.ban_status, "
        "last_check_ts=excluded.last_check_ts, last_error=excluded.last_error, "
        "pending_login=excluded.pending_login, login_channel=excluded.login_channel, "
        "updated_at=excluded.updated_at",
        {"name": name, **base},
    )


def list_fleet() -> list:
    """Every tracked container with derived health fields, name-sorted."""
    with closing(connect()) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(f"SELECT {_COLUMNS} FROM fleet_containers ORDER BY name")]
    now = int(time.time())
    for row in rows:
        ts = row.get("last_check_ts")
        row["pending_login"] = bool(row.get("pending_login"))
        row["last_check_at"] = unix_to_iso(ts)
        row["last_check_age_s"] = (now - ts) if ts else None
        row["healthy"] = row.get("session_state") == "ok"
    return rows


def get_container(name: str) -> dict:
    with closing(connect()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM fleet_containers WHERE name = ?", (name,)
        ).fetchone()
    if row is None:
        raise ReachError(
            NEED_SETUP,
            f"unknown fleet container: {name}",
            hint="Run `agent-reach fleet poll --once` after the container is up.",
        )
    return dict(row)


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------


def poll_once(*, timeout: Optional[float] = None) -> dict:
    """One health cycle: discover → check each running container → update rows.

    NEED_LOGIN on a fresh transition fires one detached relogin and sets
    pending_login; ok clears it. Returns the poll summary."""
    timeout = check_timeout() if timeout is None else float(timeout)
    now = int(time.time())
    containers = discover()
    accounts = _accounts()
    seen = set()
    relogins = []
    with closing(connect()) as conn:
        conn.row_factory = sqlite3.Row
        prior = {r["name"]: dict(r) for r in conn.execute(f"SELECT {_COLUMNS} FROM fleet_containers")}
        for c in containers:
            seen.add(c["name"])
            prev = prior.get(c["name"]) or {}
            account = c["account"] or prev.get("account") or ""
            registry_row = accounts.get(account) or {}
            fields = {
                "container_id": c["container_id"],
                "account": account or None,
                "image": c["image"],
                "proxy": registry_row.get("proxy") or None,
                "ban_status": registry_row.get("ban_status"),
                "last_check_ts": now,
                "pending_login": int(prev.get("pending_login") or 0),
                "login_channel": prev.get("login_channel"),
            }
            if c["state"] != "running":
                fields.update({
                    "session_state": "down",
                    "last_error": f"container state: {c['state'] or 'unknown'}",
                    "pending_login": 0,
                })
            else:
                check = _check(c["name"], timeout)
                fields["session_state"] = check["state"]
                fields["last_error"] = check["error"]
                if check["state"] == "ok":
                    fields["pending_login"] = 0
                    fields["login_channel"] = None
                elif check["state"] == "NEED_LOGIN":
                    fields["login_channel"] = check["channel"]
                    if prev.get("session_state") != "NEED_LOGIN" and not fields["pending_login"] and check["channel"]:
                        fired, err = _fire_relogin(c["name"], check["channel"], timeout)
                        if fired:
                            fields["pending_login"] = 1
                            relogins.append(c["name"])
                            fields["last_error"] = (check["error"] or "") + (
                                f"; relogin window opened for {check['channel']} — pending operator"
                            )
                        else:
                            fields["last_error"] = (check["error"] or "") + f"; relogin spawn failed: {err}"
            _upsert(conn, c["name"], fields)
        for name, prev in prior.items():
            if name not in seen:
                _upsert(conn, name, {
                    "container_id": prev.get("container_id"),
                    "account": prev.get("account"),
                    "proxy": prev.get("proxy"),
                    "image": prev.get("image"),
                    "session_state": "down",
                    "ban_status": prev.get("ban_status"),
                    "last_check_ts": now,
                    "last_error": "not present in docker ps",
                    "pending_login": 0,
                    "login_channel": prev.get("login_channel"),
                })
        conn.commit()
    return {
        "polled": len(seen),
        "relogins_fired": relogins,
        "containers": list_fleet(),
    }


def relogin(name: str, channel: Optional[str] = None, *, timeout: Optional[float] = None) -> dict:
    """Open a login window inside one container; mark it pending operator."""
    row = get_container(name)
    channel = channel or row.get("login_channel")
    if not channel:
        raise ReachError(
            BAD_INPUT,
            "no NEED_LOGIN channel recorded for this container; pass --channel <ch>",
        )
    ok, err = _fire_relogin(name, channel, check_timeout() if timeout is None else float(timeout))
    if not ok:
        raise ReachError(UPSTREAM_BROKEN, f"relogin spawn failed: {err}")
    now = int(time.time())
    with closing(connect()) as conn:
        conn.execute(
            "UPDATE fleet_containers SET pending_login = 1, login_channel = ?, "
            "last_check_ts = ?, updated_at = ? WHERE name = ?",
            (channel, now, now, name),
        )
        conn.commit()
    return {
        "container": name,
        "channel": channel,
        "pending_login": True,
        "note": (f"login window opened inside {name}; the operator completes the login in "
                 f"that container's browser, then the next `fleet poll` clears pending_login."),
    }


def run_forever(*, interval: Optional[float] = None, sleep=time.sleep) -> None:
    """Poll loop: cycle, sleep interval, repeat. Config errors exit once."""
    interval = poll_interval() if interval is None else float(interval)
    while True:
        try:
            poll_once()
        except ReachError as exc:
            print(json.dumps(
                {"ok": False, "action": "fleet poll",
                 "error": {"code": exc.code, "message": exc.message}},
                ensure_ascii=False), file=sys.stderr)
        sleep(interval)

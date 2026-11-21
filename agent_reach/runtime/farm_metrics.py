"""Per-account farm metrics (story 14.5): actions_log ledger + fleet health →
one structured JSON sink for the operator.

`metrics()` reads the registry actions_log (epic-11.5) grouped by account /
action_class / outcome inside a rolling window, plus the fleet_containers
health snapshot when the fleet manager (14.4) has populated rows. No
dashboard — the CLI prints the standard envelope and can append one JSONL
line per run to a sink file (`--file` / REACH_METRICS_FILE), which is the
intended cron/operator drain.

Outcome vocabulary: 'success' or an UPPER_SNAKE error code (what dispatch's
_record_farm_write stores). Errors bucket the four codes the ticket names —
RATE_LIMITED, PLATFORM_BLOCKED, QUOTA_EXCEEDED, PERSISTED_QUERY_DEAD — plus
`other`; `by_outcome` keeps the raw counts so nothing is aggregated away.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Mapping, Optional

from . import farm
from .result import (
    BAD_INPUT,
    NEED_SETUP,
    PERSISTED_QUERY_DEAD,
    PLATFORM_BLOCKED,
    QUOTA_EXCEEDED,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
    unix_to_iso,
)

DEFAULT_WINDOW_HOURS = 24

#: Error buckets in the per-account `errors` dict; any other non-success
#: outcome counts under "other".
_ERROR_KEYS = {
    RATE_LIMITED: "rate_limited",
    PLATFORM_BLOCKED: "platform_blocked",
    QUOTA_EXCEEDED: "quota_exceeded",
    PERSISTED_QUERY_DEAD: "persisted_query_dead",
}

_FLEET_FIELDS = (
    "name", "account", "session_state", "ban_status", "last_check_ts",
    "last_check_age_s", "pending_login", "healthy", "last_error",
)


def _window_seconds(window_hours) -> int:
    try:
        hours = float(window_hours)
    except (TypeError, ValueError):
        raise ReachError(BAD_INPUT, f"window_hours must be a number, got {window_hours!r}") from None
    if hours <= 0 or hours > 24 * 365:
        raise ReachError(BAD_INPUT, f"window_hours must be in (0, {24 * 365}], got {window_hours!r}")
    return int(hours * 3600)


def _fleet_snapshot(account: Optional[str]) -> Optional[dict]:
    """fleet_containers rows as a health snapshot, or None when empty/absent."""
    from . import fleet

    try:
        rows = fleet.list_fleet()
    except ReachError:
        return None
    if account is not None:
        rows = [r for r in rows if r.get("account") == account]
    if not rows:
        return None
    containers = [{k: r.get(k) for k in _FLEET_FIELDS} for r in rows]
    return {
        "total": len(containers),
        "healthy": sum(1 for c in containers if c.get("healthy")),
        "containers": containers,
    }


def metrics(account: Optional[str] = None, window_hours=DEFAULT_WINDOW_HOURS) -> dict:
    """Rolling-window metrics for one account or the whole registry.

    Shape: {"generated_ts", "generated_at", "window_hours", "accounts": [
    {account, window_hours, ban_status, age_days, actions: {success, attempts,
    by_class}, errors: {rate_limited, platform_blocked, quota_exceeded,
    persisted_query_dead, other}, by_outcome}], "fleet": {...} when populated}.
    """
    seconds = _window_seconds(window_hours)
    rows = farm.list_accounts()
    if account is not None:
        farm._validate_name(account)
        match = [r for r in rows if r["name"] == account]
        if not match:
            raise ReachError(
                NEED_SETUP,
                f"unknown farm account: {account}",
                hint=f"Run `agent-reach farm account add {account} --proxy <url>` first.",
            )
        rows = match
    now = int(time.time())
    since = now - seconds

    def _query():
        with closing(farm.connect()) as conn:
            conn.row_factory = sqlite3.Row
            if account is not None:
                return conn.execute(
                    "SELECT account, action_class, outcome, COUNT(*) AS n "
                    "FROM actions_log WHERE account = ? AND ts >= ? "
                    "GROUP BY account, action_class, outcome",
                    (account, since),
                ).fetchall()
            return conn.execute(
                "SELECT account, action_class, outcome, COUNT(*) AS n "
                "FROM actions_log WHERE ts >= ? "
                "GROUP BY account, action_class, outcome",
                (since,),
            ).fetchall()

    counts = {}
    for row in farm._with_busy_retry(_query, "farm metrics"):
        counts.setdefault(row["account"], []).append(
            (row["action_class"], row["outcome"], row["n"]))

    accounts = []
    for row in rows:
        name = row["name"]
        success = attempts = 0
        by_class: dict = {}
        by_outcome: dict = {}
        errors = {key: 0 for key in _ERROR_KEYS.values()}
        errors["other"] = 0
        for action_class, outcome, n in counts.get(name, ()):
            bucket = by_class.setdefault(action_class, {"success": 0, "attempts": 0})
            bucket["attempts"] += n
            attempts += n
            by_outcome[outcome] = by_outcome.get(outcome, 0) + n
            if outcome == "success":
                bucket["success"] += n
                success += n
            else:
                errors[_ERROR_KEYS.get(outcome, "other")] += n
        try:
            age_days = max(0, (now - int(row["age"])) // 86400)
        except (TypeError, ValueError):
            age_days = None
        accounts.append({
            "account": name,
            "window_hours": seconds / 3600,
            "ban_status": row["ban_status"],
            "age_days": age_days,
            "actions": {"success": success, "attempts": attempts, "by_class": by_class},
            "errors": errors,
            "by_outcome": by_outcome,
        })

    payload = {
        "generated_ts": now,
        "generated_at": unix_to_iso(now),
        "window_hours": seconds / 3600,
        "accounts": accounts,
    }
    snapshot = _fleet_snapshot(account)
    if snapshot is not None:
        payload["fleet"] = snapshot
    return payload


def append_jsonl(path, payload: Mapping) -> Path:
    """Append one JSON line (the full metrics payload) to the sink file."""
    sink = Path(path).expanduser()
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    try:
        fd = os.open(sink, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        raise ReachError(UPSTREAM_BROKEN, f"cannot write metrics sink {sink}: {exc}") from exc
    return sink


def report(account: Optional[str] = None, window_hours=DEFAULT_WINDOW_HOURS,
           sink: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> dict:
    """metrics() plus the sink side-effect: --file beats REACH_METRICS_FILE."""
    path = sink or (os.environ if env is None else env).get("REACH_METRICS_FILE")
    payload = metrics(account=account, window_hours=window_hours)
    if path:
        append_jsonl(path, payload)
        payload["sink_file"] = str(Path(path).expanduser())
    return payload

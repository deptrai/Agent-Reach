"""Doc-id drift monitor: host-level cron probe + alert (story 16.3).

Persisted GraphQL doc-ids are global per platform: when a platform rotates one,
every account in the farm dies at once. This module is the early-warning layer
— it enumerates every persisted doc-id in BOTH stores and alerts before the
rotation is discovered as a wall of failed writes.

The two stores (contract `docs/farm-xactions-bridge.md` §6):

- Reach: ``state.root()/query-ids.json`` (``query_ids.load()``) — channel →
  name → id. This is the single source of truth for the write lanes.
- XActions: ``$XACTIONS_HOME/query-ids.json`` (else ``~/.xactions``) —
  ``operations`` map name → ``{queryId}``. XActions has no separate
  ``endpoint.url`` file: ``endpoints.js`` builds the GraphQL URL from the id
  ``resolveOperation()`` returns, and that resolver reads this cache first,
  then the hardcoded table. The cache is refreshed from x.com's own bundles,
  so it doubles as a live-ish reference for the X side.

Drift signals, cheapest first:

1. **store-mismatch** (default, no network): a doc-id Reach persists for `x`
   differs from the id XActions' cache holds for the same operation. Since
   story 16.2 syncs Reach → XActions, the two must agree; a difference means
   at least one store is stale (XActions' cache refreshes from live bundles,
   so it usually knows first).
2. **live-probe** (opt-in, ``REACH_DRIFT_LIVE=1``): reuses the doctor core
   (``manage.doctor(probe_doc_ids=True)`` → ``query_ids.probe_facebook``) for
   the Facebook persisted query. One in-page probe, exactly like
   ``agent-reach doctor --probe-docids``; a dead doc-id answers the
   "GraphQL document not found" signature. Off by default because it needs a
   logged-in browser session — a headless cron host reports `unprobed`, never
   a false drift.

Cadence is the operator's cron entry calling ``agent-reach farm drift --once``
(every 6h by default spec, jittered); ``agent-reach farm drift`` without
``--once`` loops forever with ``REACH_DRIFT_INTERVAL`` ± ``REACH_DRIFT_JITTER``
between cycles. Per-probe budget is ``REACH_DRIFT_TIMEOUT`` (webhook POST;
the live probe keeps the doctor's own 30s browser budget).

Alert sinks (``REACH_DRIFT_SINK``): ``stdout`` (default — alert lines on
stdout, the JSON summary envelope follows), ``file`` (append JSON lines to
``REACH_DRIFT_FILE``, default ``~/.agent-reach/drifts.log``), ``webhook``
(POST ``{"alerts": [...]}`` to ``REACH_DRIFT_WEBHOOK_URL``). Every alert line
names the platform, the operation and both ids. Sink failures are reported in
``sink_errors`` and never abort the probe.
"""

from __future__ import annotations

import ipaddress
import json
import os
import random
import socket
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from . import query_ids, state, xactions_bridge as bridge
from .result import BAD_INPUT, ReachError

DEFAULT_INTERVAL = 6 * 60 * 60  # 6h
DEFAULT_JITTER = 30 * 60  # ±30min
DEFAULT_TIMEOUT = 30.0  # seconds, per probe/webhook call

XACTIONS_CACHE_NAME = "query-ids.json"
DEFAULT_LOG_NAME = "drifts.log"

_SINKS = ("stdout", "file", "webhook")
_LIVE_VALUES = {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Configuration (env-only; invalid values are BAD_INPUT, never silent defaults)
# ---------------------------------------------------------------------------


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


def config(env: Optional[Mapping[str, str]] = None) -> dict:
    """Resolve the monitor configuration; raises ReachError(BAD_INPUT) on bad env."""
    env = os.environ if env is None else env
    sink = (env.get("REACH_DRIFT_SINK") or "stdout").strip().lower()
    if sink not in _SINKS:
        raise ReachError(BAD_INPUT, f"REACH_DRIFT_SINK must be one of {', '.join(_SINKS)}, got {sink!r}")
    webhook_url = (env.get("REACH_DRIFT_WEBHOOK_URL") or "").strip()
    if sink == "webhook" and not webhook_url:
        raise ReachError(BAD_INPUT, "REACH_DRIFT_SINK=webhook requires REACH_DRIFT_WEBHOOK_URL")
    if webhook_url and not webhook_url.startswith(("http://", "https://")):
        raise ReachError(BAD_INPUT, "REACH_DRIFT_WEBHOOK_URL must be an http(s) URL")
    log_raw = (env.get("REACH_DRIFT_FILE") or "").strip()
    log_path = Path(log_raw) if log_raw else state.root() / DEFAULT_LOG_NAME
    return {
        "interval": _float_env(env, "REACH_DRIFT_INTERVAL", DEFAULT_INTERVAL, minimum=1.0),
        "jitter": _float_env(env, "REACH_DRIFT_JITTER", DEFAULT_JITTER, minimum=0.0),
        "timeout": _float_env(env, "REACH_DRIFT_TIMEOUT", DEFAULT_TIMEOUT, minimum=1.0),
        "sink": sink,
        "webhook_url": webhook_url,
        "log_path": log_path,
        "live": (env.get("REACH_DRIFT_LIVE") or "").strip().lower() in _LIVE_VALUES,
    }


def next_delay(cfg: Optional[Mapping[str, Any]] = None, *, rng: Optional[Callable[[float, float], float]] = None) -> float:
    """Seconds until the next cycle: interval ± jitter, never below 1s."""
    cfg = config() if cfg is None else cfg
    roll = (rng or random.uniform)(-float(cfg["jitter"]), float(cfg["jitter"]))
    return max(1.0, float(cfg["interval"]) + roll)


# ---------------------------------------------------------------------------
# Store readers — every persisted doc-id, both stores
# ---------------------------------------------------------------------------


def xactions_cache_path() -> Path:
    """``$XACTIONS_HOME/query-ids.json`` — mirrors xactions_docids.xactions_cache_path()."""
    return bridge.xactions_home() / XACTIONS_CACHE_NAME


def _reach_ids() -> list[dict]:
    """Persisted Reach doc-ids: one entry per channel/name in query-ids.json."""
    out = []
    for channel, entries in query_ids.load().items():
        if not isinstance(entries, dict):
            continue
        for name, value in entries.items():
            if str(name).startswith("_") or isinstance(value, bool):
                continue
            if isinstance(value, (str, int)):
                doc_id = str(value).strip()
                if doc_id:
                    out.append({"store": "reach", "platform": channel, "name": str(name), "doc_id": doc_id})
    return out


def _xactions_ids() -> list[dict]:
    """Persisted XActions doc-ids from the query-id cache's `operations` map."""
    try:
        doc = json.loads(xactions_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    operations = doc.get("operations") if isinstance(doc, dict) else None
    if not isinstance(operations, dict):
        return []
    out = []
    for name, entry in operations.items():
        if not isinstance(entry, dict):
            continue
        doc_id = entry.get("queryId")
        if isinstance(doc_id, str) and doc_id.strip():
            out.append({"store": "xactions", "platform": "x", "name": str(name), "doc_id": doc_id.strip()})
    return out


def collect() -> tuple[list[dict], list[dict]]:
    """(reach_ids, xactions_ids) — the full probe surface of both stores."""
    return _reach_ids(), _xactions_ids()


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


def find_drift(reach_ids: Sequence[Mapping[str, Any]], xactions_ids: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Cross-store mismatches for operations both stores persist.

    Only `x` is cross-checked: Facebook doc-ids exist solely in Reach (the
    XActions cache is X-only), so a Facebook rotation is caught by the opt-in
    live probe instead."""
    xa = {p["name"]: p["doc_id"] for p in xactions_ids if p.get("platform") == "x"}
    drift = []
    for probe in reach_ids:
        if probe.get("platform") != "x":
            continue
        other = xa.get(probe["name"])
        if other is not None and other != probe["doc_id"]:
            drift.append({
                "kind": "store-mismatch",
                "platform": "x",
                "name": probe["name"],
                "reach_id": probe["doc_id"],
                "xactions_id": other,
            })
    return drift


def _live_facebook() -> dict:
    """The doctor --probe-docids core, isolated so a browser failure is data."""
    from . import manage

    try:
        return manage.doctor(probe_doc_ids=True).get("doc_id_drift") or {}
    except Exception as exc:  # noqa: BLE001 — a failed probe must not break the monitor
        return {"probed": "facebook", "results": [], "verdict": "unprobed", "error": str(exc)[:300]}


def render_alert(drift: Mapping[str, Any]) -> str:
    """One alert line; always carries platform + operation + the ids involved."""
    if drift.get("kind") == "store-mismatch":
        return (f"[doc-id drift] {drift['platform']}/{drift['name']}: "
                f"reach={drift['reach_id']} xactions={drift['xactions_id']} (store-mismatch)")
    return (f"[doc-id drift] {drift['platform']}/{drift['name']}: "
            f"doc_id={drift.get('doc_id')} verdict={drift.get('verdict')} (live probe)")


# ---------------------------------------------------------------------------
# Alert sinks
# ---------------------------------------------------------------------------


def _send_stdout(alerts: Sequence[str]) -> list[str]:
    for line in alerts:
        print(line)
    return []


def _send_file(alerts: Sequence[str], cfg: Mapping[str, Any]) -> list[str]:
    try:
        path = Path(cfg["log_path"])
        from agent_reach.utils.paths import make_private_dir

        make_private_dir(path.parent)
        with path.open("a", encoding="utf-8") as handle:
            for line in alerts:
                handle.write(line + "\n")
        return []
    except Exception as exc:  # noqa: BLE001 — sink failure is reported, not raised
        return [f"file sink failed: {exc}"]


def _blocked_webhook_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return bool(
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
    )


def _assert_webhook_public(url: str) -> None:
    """Refuse a sink aimed at loopback, link-local or any internal address.
    An unresolvable name is left for the POST to fail — a typo is not SSRF."""
    host = (urllib.parse.urlsplit(url).hostname or "").strip("[]")
    if not host:
        raise ReachError(BAD_INPUT, "webhook URL has no host")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _blocked_webhook_ip(str(literal)):
            raise ReachError(BAD_INPUT, f"webhook host {host} is not a public address")
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return
    for info in infos:
        if _blocked_webhook_ip(info[4][0]):
            raise ReachError(
                BAD_INPUT, f"webhook host {host} resolves to a non-public address"
            )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReachError(BAD_INPUT, f"webhook redirect refused ({code})")


def _webhook_open(request, timeout):
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def _send_webhook(alerts: Sequence[str], cfg: Mapping[str, Any]) -> list[str]:
    try:
        _assert_webhook_public(cfg["webhook_url"])
    except ReachError as exc:
        return [f"webhook sink failed: {exc}"]
    payload = json.dumps({"alerts": list(alerts)}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        cfg["webhook_url"], data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with _webhook_open(request, timeout=float(cfg["timeout"])) as response:
            response.read()
        return []
    except Exception as exc:  # noqa: BLE001 — a dead webhook must not kill the cron
        return [f"webhook sink failed: {exc}"]


def send_alerts(alerts: Sequence[str], cfg: Mapping[str, Any]) -> list[str]:
    """Deliver alert lines through the configured sink; returns sink error strings."""
    if not alerts:
        return []
    sink = cfg["sink"]
    if sink == "stdout":
        return _send_stdout(alerts)
    if sink == "file":
        return _send_file(alerts, cfg)
    return _send_webhook(alerts, cfg)


# ---------------------------------------------------------------------------
# One probe cycle
# ---------------------------------------------------------------------------


def run_once(*, live: Optional[bool] = None) -> dict:
    """Probe both stores once, alert on drift, and return the full report.

    `live=None` follows ``REACH_DRIFT_LIVE``; pass an explicit bool to force.
    Raises ReachError(BAD_INPUT) on invalid env config."""
    cfg = config()
    reach_ids, xactions_ids = collect()
    probes = [dict(p) for p in reach_ids + xactions_ids]

    drifted = find_drift(reach_ids, xactions_ids)
    live_info = None
    if live is None:
        live = cfg["live"]
    if live:
        live_info = _live_facebook()
        result = (live_info.get("results") or [{}])[0]
        try:
            from agent_reach.channels import _facebook_write as fw

            fb_default = fw._DOC_ID
        except Exception:  # noqa: BLE001 — probe log must survive a channel import problem
            fb_default = ""
        probes.append({
            "store": "live", "platform": "facebook", "name": "post_create",
            "doc_id": result.get("doc_id") or query_ids.get("facebook", "post_create", fb_default),
            "verdict": live_info.get("verdict"),
        })
        if live_info.get("stale"):
            drifted.append({
                "kind": "live-probe", "platform": "facebook", "name": "post_create",
                "doc_id": probes[-1]["doc_id"], "verdict": live_info.get("verdict"),
            })

    alerts = [render_alert(d) for d in drifted]
    sink_errors = send_alerts(alerts, cfg)
    return {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cadence": {"interval": cfg["interval"], "jitter": cfg["jitter"], "timeout": cfg["timeout"]},
        "sink": cfg["sink"],
        "stores": {"reach": str(query_ids.config_path()), "xactions": str(xactions_cache_path())},
        "probes": probes,
        "drifted": drifted,
        "alerts": alerts,
        "sink_errors": sink_errors,
        "live": live_info,
    }


def run_forever(*, sleep: Callable[[float], None] = time.sleep) -> None:
    """Cron loop: probe, sleep interval±jitter, repeat. Config errors exit once."""
    cfg = config()  # BAD_INPUT surfaces before the first cycle, not inside it
    while True:
        try:
            run_once()
        except ReachError as exc:
            # A transient probe failure must not end the monitor; config was
            # already validated above, so this is an upstream/read problem.
            print(json.dumps({"ok": False, "action": "farm drift", "error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False), file=sys.stderr)
        sleep(next_delay(cfg))

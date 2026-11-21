"""Run a unified action: validate params, try backends in order, build the envelope."""

from __future__ import annotations

import os
import socket
import threading
import time
from contextlib import nullcontext
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from agent_reach.channels.base import Action, Channel

from . import state
from .browser_binding import QUEUE_WAIT, query_slot, wait_lock
from .clock import Clock, bind
from .result import (
    BAD_INPUT,
    FINAL_CODES,
    NEED_SETUP,
    PLATFORM_BLOCKED,
    QUOTA_EXCEEDED,
    RATE_LIMITED,
    TIMEOUT,
    UPSTREAM_BROKEN,
    ReachError,
    classify,
    envelope,
)

DEFAULT_TIMEOUT = 30.0
BROWSER_SEARCH_INTERVAL = 10.0
BACKEND_PAUSE_SECONDS = {TIMEOUT: 60, UPSTREAM_BROKEN: 120, NEED_SETUP: 60}
RATE_LIMIT_SECONDS = 3600


def find_action(channel: Channel, name: str) -> Optional[Action]:
    return next((a for a in channel.actions if a.name == name), None)


def _validate(action: Action, params: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(params, dict):
        raise ReachError(BAD_INPUT, "params must be an object")
    clean: Dict[str, Any] = {}
    for p in action.params:
        value = params.get(p.name)
        if isinstance(value, str):
            value = value.strip()
        if value is None or value == "":
            if p.required:
                raise ReachError(BAD_INPUT, f"missing required argument: {p.name}")
            value = p.default
        elif p.type is str and not isinstance(value, str):
            raise ReachError(BAD_INPUT, f"{p.name} must be str")
        elif p.type is bool:
            if isinstance(value, bool):
                pass
            elif isinstance(value, str):
                lowered = value.lower()
                if lowered in ("true", "1", "yes", "on"):
                    value = True
                elif lowered in ("false", "0", "no", "off"):
                    value = False
                else:
                    raise ReachError(BAD_INPUT, f"{p.name} must be bool")
            else:
                raise ReachError(BAD_INPUT, f"{p.name} must be bool")
        elif p.type is not str:
            if isinstance(value, bool) or (p.type is int and isinstance(value, float) and not value.is_integer()):
                raise ReachError(BAD_INPUT, f"{p.name} must be {p.type.__name__}")
            try:
                value = p.type(value)
            except (TypeError, ValueError):
                raise ReachError(BAD_INPUT, f"{p.name} must be {p.type.__name__}") from None
        if p.type is int and p.name in {"limit", "page"} and (not isinstance(value, int) or value < 1):
            raise ReachError(BAD_INPUT, f"{p.name} must be a positive integer")
        clean[p.name] = value
    unknown = set(params) - {p.name for p in action.params}
    if unknown:
        raise ReachError(BAD_INPUT, f"unknown argument(s): {', '.join(sorted(unknown))}")
    return clean


def validate_action(channel: Channel, action_name: str, params: Dict[str, Any], *,
                    timeout: float = DEFAULT_TIMEOUT) -> tuple[Action, dict]:
    """Validate before setup can install tools or request a login."""
    import math

    action = find_action(channel, action_name)
    if action is None:
        valid = ", ".join(a.name for a in channel.actions) or "none"
        raise ReachError(BAD_INPUT, f"unknown action '{action_name}' for {channel.name}; valid: {valid}")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ReachError(BAD_INPUT, "timeout must be finite and positive")
    clean = _validate(action, params)
    channel.validate_params(action_name, clean)
    return action, clean


def _fingerprint(channel: Channel) -> Optional[str]:
    from . import manage

    try:
        return manage.fingerprint(channel.name)
    except ReachError:
        # Unregistered channels are useful to embedders and local test harnesses.
        return None


def cooldown(channel: str) -> Optional[dict]:
    """A confirmed platform rate limit applies to every entry point and backend."""
    saved = state.load()
    for failure in (saved.get("cooldowns", {}).get(channel, {}),
                    saved.get("setup_failures", {}).get(channel, {})):
        left = RATE_LIMIT_SECONDS - state.age_seconds(failure.get("at"))
        if failure.get("code") == RATE_LIMITED and left > 0:
            return {"code": RATE_LIMITED, "message": failure.get("message") or "Rate limited",
                    "retry_after_seconds": max(1, int(left))}
    return None


def backend_pause(channel: Channel, action: str, backend: str) -> Optional[dict]:
    saved = state.load().get("backend_health", {}).get(channel.name, {}).get(action, {}).get(backend, {})
    if saved.get("fingerprint") != _fingerprint(channel):
        return None
    ttl = BACKEND_PAUSE_SECONDS.get(saved.get("code"), 0)
    remaining = ttl - state.age_seconds(saved.get("at"))
    if remaining <= 0:
        return None
    return {"code": saved["code"], "retry_after_seconds": max(1, int(remaining)),
            "message": f"{backend} is temporarily paused after {saved['code']}; retry later"}


def _remember(channel: Channel, action: str, backend: str, error: Optional[ReachError]) -> None:
    signature, stamp = _fingerprint(channel), state.now()

    def change(saved):
        health = saved.setdefault("backend_health", {}).setdefault(channel.name, {}).setdefault(action, {})
        if error is None:
            health.pop(backend, None)
        elif error.code in BACKEND_PAUSE_SECONDS:
            # Store diagnostics only; no query, result, Cookie, or upstream raw text.
            health[backend] = {"code": error.code, "at": stamp, "fingerprint": signature}
        if error is not None and error.code == RATE_LIMITED:
            saved.setdefault("cooldowns", {})[channel.name] = {
                "code": RATE_LIMITED, "message": "Platform reported a rate limit", "at": stamp}
    state.mutate(change)


def _pace_browser(channel: str, action: str, clock: Clock) -> None:
    if action not in {"search", "jobs"}:
        return
    # Same-site queries may now run side by side; read, wait and record the start
    # under one lock so two of them cannot both skip the interval.
    with wait_lock(f"pace:{channel}", wait=max(0.0, clock.remaining())):
        last = state.load().get("request_starts", {}).get(channel)
        age = max(0, time.time() - last) if isinstance(last, (int, float)) else state.age_seconds(last)
        wait = BROWSER_SEARCH_INTERVAL - age
        if wait > 0:
            if wait >= clock.remaining():
                raise ReachError(TIMEOUT, "Search pacing exceeds the remaining timeout; retry later")
            time.sleep(wait)
        state.mutate(lambda saved: saved.setdefault("request_starts", {}).__setitem__(channel, time.time()))


def _call_with_deadline(fn, params: Dict[str, Any], clock: Clock) -> List[dict]:
    """Run fn(timeout=..., **params); give up when the clock runs out.

    In-process HTTP calls cannot be interrupted, so the call runs in a daemon
    thread. Subprocess backends also receive `timeout` and kill themselves.
    The backend may pause the clock (off_the_clock) while it waits for setup.
    """
    box: Dict[str, Any] = {}
    budget = clock.remaining()

    def target() -> None:
        bind(clock)
        try:
            box["items"] = fn(timeout=budget, **params)
        except BaseException as exc:  # noqa: BLE001 - forwarded to classify()
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    while True:
        left = clock.remaining() + 1.0
        if left <= 0 and not clock.paused():
            break
        worker.join(max(left, 0.2))
        if not worker.is_alive():
            break
    if worker.is_alive():
        raise ReachError(TIMEOUT, f"backend did not answer within {budget:.0f}s")
    if "error" in box:
        raise box["error"]
    return list(box.get("items") or [])


def _pick_error(errors: List[ReachError]) -> ReachError:
    """Report the most useful failure: a real error beats 'not installed'."""
    for err in errors:
        if err.code != NEED_SETUP:
            return err
    return errors[0]


def _channel_added(channel: Channel) -> bool:
    """An added channel answers from its own site tools; public web search is only for channels not set up yet."""
    from .manage import is_added

    try:
        return is_added(channel, state.load())
    except Exception:  # noqa: BLE001 - an unreadable ledger must not block the query
        return False


#: Serializes in-process account runs: os.environ is process-global, so a second
#: run waits here instead of observing another account's HOME. Cross-process
#: runs own private os.environ and stay parallel; the site/browser file locks
#: inside the run still govern real browser concurrency.
_ACCOUNT_ENV_LOCK = threading.Lock()
_ENV_OWNER: int | None = None  # thread id holding the env window, or None


def _farm_env_enter():
    """Resolve REACH_ACCOUNT through the farm registry and apply its env; returns a restore callable.

    Unset → legacy single-account path (no-op). Set → `farm.account_env` yields
    HOME/REACH_BROWSER_PROXY/REACH_ACCOUNT (unknown → NEED_SETUP, bad name →
    BAD_INPUT). Resolved keys replace os.environ; keys absent from the
    resolution (e.g. a proxy-less account) are dropped rather than inherited,
    so no foreign proxy leaks into the run. REACH_FARM_ROOT pins the operator
    registry so farm.* calls inside the window (nested run_action, ledger
    writes) hit the same registry, not one under the account's swapped HOME.
    The caller must invoke the returned callable exactly once (finally).
    """
    global _ENV_OWNER
    tid = threading.get_ident()
    lock_held = False
    if os.environ.get("REACH_FARM_ROOT"):
        # A farm env window is already applied in this process.
        if _ENV_OWNER == tid:
            # Nested run_action inside our own window — inherits the outer
            # account (e.g. public-web first-attempt); nothing to resolve.
            return lambda: None
        # Another thread owns the window — wait for its restore first, then
        # resolve fresh. Reading the selector before the window ends would see
        # the OTHER account's REACH_ACCOUNT and silently hijack this run.
        if not _ACCOUNT_ENV_LOCK.acquire(timeout=QUEUE_WAIT):
            raise ReachError(
                "BUSY",
                "Another farm account action still owns this process's environment",
            )
        lock_held = True
    name = os.environ.get("REACH_ACCOUNT")
    if not name:
        if lock_held:
            _ACCOUNT_ENV_LOCK.release()
        return lambda: None
    from . import farm

    try:
        farm_root = str(farm.farm_root())
        env = dict(farm.account_env(name))
    except ReachError:
        if lock_held:
            _ACCOUNT_ENV_LOCK.release()
        raise
    except Exception as exc:
        if lock_held:
            _ACCOUNT_ENV_LOCK.release()
        raise ReachError(
            UPSTREAM_BROKEN, f"farm env resolve failed: {exc}"
        ) from exc
    env["REACH_FARM_ROOT"] = farm_root
    keys = ("HOME", "REACH_BROWSER_PROXY", "REACH_ACCOUNT", "REACH_FARM_ROOT")
    if not lock_held and not _ACCOUNT_ENV_LOCK.acquire(timeout=QUEUE_WAIT):
        raise ReachError(
            "BUSY",
            "Another farm account action still owns this process's environment",
        )
    try:
        saved = {key: os.environ.get(key) for key in keys}
        for key in keys:
            if key in env:
                os.environ[key] = env[key]
            else:
                os.environ.pop(key, None)  # never inherit an outer proxy silently
        _ENV_OWNER = tid
    except BaseException:
        _ACCOUNT_ENV_LOCK.release()
        raise

    def restore() -> None:
        global _ENV_OWNER
        try:
            for key in keys:
                if saved[key] is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = saved[key]
            _ENV_OWNER = None
        finally:
            _ACCOUNT_ENV_LOCK.release()

    return restore


#: Write verbs per channel for the farm ledger seam (contract §3). Only actions
#: listed here append an `actions_log` row; the verb itself is `action_class`.
#: Scoped per channel because verb names collide across platforms — xueqiu's
#: `quote` is a read while twitter's `quote`/`thread` are write lanes (12.5/12.6).
#: A new write action records automatically once its verb is listed here.
WRITE_ACTIONS = {
    "facebook": {
        "post", "post-edit", "post-delete", "react", "unreact",
        "comment", "uncomment", "share", "message",
    },
    "twitter": {"post", "reply", "quote", "thread"},
}

#: Default per-class caps (successes / rolling 24h). Override with
#: REACH_CAP_<CLASS> (`post-edit` → REACH_CAP_POST_EDIT). A class absent here
#: is unlimited until an operator sets the env var. `0` refuses every write.
DEFAULT_CAPS = {
    "post": 30,
    "reply": 60,
    "comment": 60,
    "react": 200,
    "share": 20,
}


def _cap_for(action_name: str) -> Optional[int]:
    """Resolved cap, or None when this class is unlimited."""
    key = "REACH_CAP_" + action_name.upper().replace("-", "_")
    raw = os.environ.get(key)
    if raw is not None:
        try:
            cap = int(raw)
        except ValueError:
            raise ReachError(BAD_INPUT, f"invalid {key}={raw!r}; expected a non-negative integer") from None
        if cap < 0:
            raise ReachError(BAD_INPUT, f"invalid {key}={raw!r}; expected a non-negative integer")
        return cap
    return DEFAULT_CAPS.get(action_name)


#: Warm-up ramp (story 13.3): (inclusive account-age bound in days, cap
#: factor). Older than the last bound → factor 1.0 (mature). Override with
#: REACH_RAMP="days:factor,..." e.g. "3:0.25,7:0.5,14:0.75".
RAMP_SCHEDULE = ((3, 0.25), (7, 0.50), (14, 0.75))


def _ramp_schedule() -> tuple:
    """The active ramp table — REACH_RAMP parsed, else RAMP_SCHEDULE."""
    raw = os.environ.get("REACH_RAMP")
    if raw is None:
        return RAMP_SCHEDULE
    try:
        steps = sorted(
            (int(days), float(factor))
            for days, factor in (part.split(":") for part in raw.split(","))
        )
    except ValueError:
        steps = []
    if not steps or any(d < 0 or not 0 < f <= 1 for d, f in steps):
        raise ReachError(
            BAD_INPUT,
            f"invalid REACH_RAMP={raw!r}; expected 'days:factor,...' like '3:0.25,7:0.5'",
        )
    return tuple(steps)


def _ramp_factor(age_days: Optional[int], schedule: tuple) -> float:
    """Cap scale for an account age_days old; unknown age (None) → mature."""
    if age_days is None:
        return 1.0
    for bound, factor in schedule:
        if age_days <= bound:
            return factor
    return 1.0


def _account_age_days(account: str, farm) -> Optional[int]:
    """Whole days since the onboard epoch, or None when unusable.

    None covers a missing/non-numeric epoch AND a zero/future one: `age` is
    the onboard epoch, so 0 or a timestamp ahead of now cannot be an age —
    those rows are unmigrated or hand-repaired and get the mature factor
    instead of the strictest ramp.
    """
    onboard = farm.account_age(account)
    if not onboard or onboard > int(time.time()):
        return None
    return (int(time.time()) - onboard) // 86400


_PROXY_CHECK_TTL = 60.0
_PROXY_CHECK_TIMEOUT = 5.0
# proxy URL → (ok, expires_at). Process-local: a CLI run is one process.
_PROXY_VERDICTS: Dict[str, tuple] = {}


def _proxy_check_ttl() -> float:
    raw = os.environ.get("REACH_PROXY_CHECK_TTL")
    if raw is None:
        return _PROXY_CHECK_TTL
    try:
        ttl = float(raw)
    except ValueError:
        raise ReachError(BAD_INPUT, f"invalid REACH_PROXY_CHECK_TTL={raw!r}; expected seconds") from None
    if ttl < 0:
        raise ReachError(BAD_INPUT, f"invalid REACH_PROXY_CHECK_TTL={raw!r}; expected seconds")
    return ttl


class _ProxyUncheckable(Exception):
    """The proxy host cannot even be resolved — nothing to probe."""


def _proxy_alive(proxy: str) -> None:
    """TCP-connect the proxy host:port, or raise the reason it is dead.

    A completed handshake is the whole liveness signal: the ticket's
    HEAD/CONNECT probe would need a target host, which is itself egress and
    false-fails on a proxy that is up but blocks that one URL. A dead proxy
    refuses the handshake either way. shortcut: handshake only — does not
    prove the proxy will relay, upgrade when a relay probe has a safe target.

    An unresolvable host raises _ProxyUncheckable instead of failing the run:
    placeholder proxies (tests, accounts not yet wired) have no socket to
    refuse, and blocking them would kill every existing farm test.
    """
    try:
        parsed = urlparse(proxy)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        host = port = None
    if not host or port is None:
        raise OSError(f"proxy URL has no host:port: {proxy!r}")
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise _ProxyUncheckable(str(exc)) from exc
    with socket.create_connection((host, port), timeout=_PROXY_CHECK_TIMEOUT):
        pass


def _enforce_proxy_kill_switch() -> None:
    """Refuse the run when the account's proxy cannot be confirmed alive.

    Contract §2 layer-1, zero egress: applies to every action (reads leak the
    IP too), only when REACH_ACCOUNT is set and the registry row stores a
    proxy. Proxy-less rows and the legacy path skip. Verdicts — alive and
    dead — cache for ~60s per proxy URL so a burst of actions pays one probe.
    The check failing itself (bad URL, DNS, anything unexpected) fails closed:
    an unconfirmed proxy is a dead proxy, never a free pass to direct egress.
    REACH_PROXY_CHECK=0/off/false/no is the operator escape hatch.
    """
    if os.environ.get("REACH_PROXY_CHECK", "").strip().lower() in ("0", "off", "false", "no"):
        return
    account = os.environ.get("REACH_ACCOUNT")
    if not account:
        return
    try:
        from . import farm

        proxy = farm.account_proxy(account)
    except ReachError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable registry cannot vouch for the proxy
        raise ReachError(
            PLATFORM_BLOCKED,
            f"proxy kill-switch fail-closed for account {account}: registry unreadable ({exc})",
        ) from exc
    if not proxy:
        return
    ttl = _proxy_check_ttl()
    cached = _PROXY_VERDICTS.get(proxy)
    now = time.monotonic()
    if cached is not None and cached[1] > now:
        if cached[0]:
            return
        raise ReachError(
            PLATFORM_BLOCKED,
            f"proxy dead for account {account}: {proxy} (cached verdict, rechecked within {ttl:.0f}s)",
        )
    try:
        _proxy_alive(proxy)
    except _ProxyUncheckable:
        # Host does not resolve — no probe is possible. Not a verdict: a
        # genuinely dead proxy (refused/timed-out connect) still fails closed.
        return
    except Exception as exc:  # noqa: BLE001 - any probe failure is a dead proxy
        _PROXY_VERDICTS[proxy] = (False, now + ttl)
        raise ReachError(
            PLATFORM_BLOCKED,
            f"proxy dead for account {account}: {proxy} ({type(exc).__name__}: {exc})",
        ) from exc
    _PROXY_VERDICTS[proxy] = (True, now + ttl)


def _enforce_write_quota(channel_name: str, action_name: str) -> None:
    """Refuse a write whose 24h success count has reached the cap.

    No-op for reads, verbs outside WRITE_ACTIONS, the legacy path (no
    REACH_ACCOUNT), and unlimited classes — those never touch the ledger.
    An unreadable ledger refuses the write (fail-closed): a cap that cannot
    be checked must not become a free pass.

    Warm-up ramp (13.3): the cap scales by account age — the registry `age`
    onboard epoch — via RAMP_SCHEDULE/REACH_RAMP. Effective cap =
    max(1, floor(cap x factor)); a missing onboard epoch means a mature
    account (factor 1.0), and an explicit cap of 0 stays refuse-all.
    """
    if action_name not in WRITE_ACTIONS.get(channel_name, ()):
        return
    account = os.environ.get("REACH_ACCOUNT")
    if not account:
        return
    cap = _cap_for(action_name)
    if cap is None:
        return
    try:
        from . import farm

        snap = farm.ledger_24h(account)
        age_days = _account_age_days(account, farm) if cap > 0 else None
    except ReachError:
        raise
    except Exception as exc:  # noqa: BLE001 - any ledger failure blocks the write
        raise ReachError(UPSTREAM_BROKEN, f"quota check failed for account {account}: {exc}") from exc
    by_class = snap.get("by_class") or {}
    count = int((by_class.get(action_name) or {}).get("success") or 0)
    effective = cap
    ramp = 1.0
    if cap > 0:
        ramp = _ramp_factor(age_days, _ramp_schedule())
        if ramp < 1.0:
            effective = max(1, int(cap * ramp))
    if count >= effective:
        detail = (
            f"cap {cap} x ramp {ramp:g} (account age {age_days}d) = {effective}"
            if ramp < 1.0
            else f"cap {cap}"
        )
        raise ReachError(
            QUOTA_EXCEEDED,
            f"quota cap exceeded for account {account} action_class {action_name}: "
            f"{count} success in 24h ({detail})",
        )


def _record_farm_write(channel_name: str, action_name: str, result: dict) -> None:
    """Append one `actions_log` row for a write run under REACH_ACCOUNT.

    Reads the selector from the (restored) env: a nested run_action inside an
    outer window still sees it, and the REACH_FARM_ROOT pin keeps the write on
    the operator registry. Unset → single-account path, skip silently.
    Fail-safe: ledger errors never reach the result envelope.
    """
    if action_name not in WRITE_ACTIONS.get(channel_name, ()):
        return
    account = os.environ.get("REACH_ACCOUNT")
    if not account:
        return
    outcome = "success" if result.get("ok") else str(
        (result.get("error") or {}).get("code") or UPSTREAM_BROKEN
    )
    # Any refusal from the quota gate never reached the platform — not an
    # attempt, whatever its code (over cap, unreadable ledger, bad cap).
    if (result.get("meta") or {}).get("quota_refused"):
        return
    try:
        from . import farm

        farm.record_action(account, action_name, outcome)
    except Exception:  # noqa: BLE001 - ledger writes must never break the action
        pass


def run_action(
    channel: Channel,
    action_name: str,
    params: Dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    only_backend: Optional[str] = None,
) -> dict:
    """Execute one unified action and return the envelope dict.

    Under REACH_ACCOUNT a write verb also records the run's final outcome in
    the farm ledger (after fallback attempts); the recording never changes
    the returned result.
    """
    result = _run_action(
        channel, action_name, params, timeout=timeout, only_backend=only_backend
    )
    _record_farm_write(channel.name, action_name, result)
    return result


def _run_action(
    channel: Channel,
    action_name: str,
    params: Dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    only_backend: Optional[str] = None,
) -> dict:
    """Dispatch one action across its backends (see run_action for the ledger seam)."""
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        action, clean = validate_action(channel, action_name, params, timeout=timeout)
    except ReachError as validation_error:
        return envelope(channel=channel.name, action=action_name, elapsed_ms=elapsed(), error=validation_error)

    from . import public_web

    public_only = only_backend == public_web.BACKEND and bool(action.public_web)
    backends = [b for b in action.backends if only_backend in (None, b[0]) or public_only]
    if not backends:
        return envelope(channel=channel.name, action=action_name, elapsed_ms=elapsed(),
                        error=ReachError(BAD_INPUT, f"no backend named '{only_backend}'"))
    clock = Clock(timeout - (time.monotonic() - started))
    attempts: List[dict] = []
    errors: List[ReachError] = []
    cached_errors: List[ReachError] = []
    try:
        release_env = _farm_env_enter()
    except ReachError as exc:
        return envelope(channel=channel.name, action=action_name, elapsed_ms=elapsed(), error=exc)
    try:
        # Kill-switch runs first: a dead proxy makes quota counting moot, and
        # both checks sit inside the env window (account resolved) before
        # cooldown, public-web, and every backend. A gate refusal — dead
        # proxy, over cap, unreadable registry/ledger, bad gate env value —
        # never touched the platform, so the envelope is flagged for the
        # record seam to skip.
        try:
            _enforce_proxy_kill_switch()
            _enforce_write_quota(channel.name, action_name)
        except ReachError as gate_error:
            out = envelope(channel=channel.name, action=action_name,
                           elapsed_ms=elapsed(), error=gate_error)
            out["meta"]["quota_refused"] = True
            return out
        limited = cooldown(channel.name)
        if limited:
            raise ReachError(RATE_LIMITED, limited["message"],
                             hint=f"Wait {limited['retry_after_seconds']} seconds before retrying this channel.")
        public_first = bool(action.public_web) and only_backend is None and not _channel_added(channel)
        if public_first or public_only:
            budget = clock.remaining() if public_only else min(public_web.MAX_SECONDS, clock.remaining() / 2)
            public = public_web.attempt(channel, action, clean, timeout=budget)
            if public is not None:
                attempts.extend(public["meta"]["attempts"])
                if public["ok"] or public_only:
                    public["meta"]["elapsed_ms"] = elapsed()
                    return public
            elif public_only:
                raise ReachError(BAD_INPUT, "This input has no public web route; use a full supported URL")
        # Browser queries wait in line: a few per site and overall share the browser at once.
        browser_action = any(b[0] in {"opencli", "boss-agent-cli"} for b in action.backends)
        lock = query_slot(channel.name, clock) if browser_action else nullcontext()
        with lock:
            limited = cooldown(channel.name)
            if limited:
                raise ReachError(RATE_LIMITED, limited["message"])
            paced = False
            for label, method_name in backends:
                paused = backend_pause(channel, action_name, label) if not only_backend else None
                if paused:
                    attempts.append({"backend": label, "result": paused["code"], "skipped": True,
                                     "retry_after_seconds": paused["retry_after_seconds"], "message": paused["message"]})
                    cached_errors.append(ReachError(paused["code"], paused["message"]))
                    continue
                if clock.remaining() <= 0:
                    attempts.append({"backend": label, "result": TIMEOUT})
                    errors.append(ReachError(TIMEOUT, "no time left for this backend"))
                    break
                if browser_action and not paced:
                    _pace_browser(channel.name, action_name, clock)
                    paced = True
                try:
                    items = _call_with_deadline(getattr(channel, method_name), clean, clock)
                except BaseException as exc:  # noqa: BLE001
                    err = classify(exc)
                    attempts.append({"backend": label, "result": err.code, "message": err.message})
                    errors.append(err)
                    _remember(channel, action_name, label, err)
                    if err.code in FINAL_CODES:
                        break
                    continue
                attempts.append({"backend": label, "result": "ok"})
                _remember(channel, action_name, label, None)
                return envelope(
                    channel=channel.name,
                    action=action_name,
                    backend=label,
                    items=items,
                    elapsed_ms=elapsed(),
                    attempts=attempts,
                )
        # Last resort for an added channel: every site backend failed (or is
        # paused after failing) with a transient error, so the public-web
        # route is tried once. This keeps the public-first rule (site tools
        # stay preferred) while fixing the case where a logged-in channel
        # reports failure even though the public path works (e.g. the site
        # tool returns HTML instead of JSON). Explicit --backend selection and
        # final errors (bad input, login wall, rate limit, platform block, ...)
        # never fall back.
        failures = errors + cached_errors
        if (action.public_web and only_backend is None and not public_first and failures
                and not any(e.code in FINAL_CODES for e in failures)
                and any(e.code in (UPSTREAM_BROKEN, TIMEOUT) for e in failures)
                and clock.remaining() > 0):
            public = public_web.attempt(channel, action, clean, timeout=clock.remaining())
            if public is not None:
                attempts.extend(public["meta"]["attempts"])
                if public["ok"]:
                    # Keep the site-backend failure in the trail, not just
                    # the public-web rows, so the fallback is auditable.
                    public["meta"]["attempts"] = attempts
                    public["meta"]["elapsed_ms"] = elapsed()
                    return public
    except ReachError as exc:
        errors.append(exc)
    finally:
        release_env()

    errors = errors or cached_errors
    final = next((e for e in errors if e.code in FINAL_CODES), None)
    err = final or (_pick_error(errors) if errors else ReachError(BAD_INPUT, f"no backend named '{only_backend}'"))
    return envelope(
        channel=channel.name,
        action=action_name,
        elapsed_ms=elapsed(),
        attempts=attempts,
        error=err,
    )

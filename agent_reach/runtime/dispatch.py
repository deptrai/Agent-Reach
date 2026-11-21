"""Run a unified action: validate params, try backends in order, build the envelope."""

from __future__ import annotations

import os
import threading
import time
from contextlib import nullcontext
from typing import Any, Dict, List, Optional

from agent_reach.channels.base import Action, Channel

from . import state
from .browser_binding import QUEUE_WAIT, query_slot, wait_lock
from .clock import Clock, bind
from .result import (
    BAD_INPUT,
    FINAL_CODES,
    NEED_SETUP,
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


def run_action(
    channel: Channel,
    action_name: str,
    params: Dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    only_backend: Optional[str] = None,
) -> dict:
    """Execute one unified action and return the envelope dict."""
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

"""Shared fake-browser harness for the facebook_* write-path test modules.

Each test file declares one :class:`FbWriteHarness` configured with its module's
regexes and per-module quirks, then exposes::

    harness = FbWriteHarness(fw).patch(monkeypatch)

The returned ``calls`` dict keeps the shape the per-file fixtures always had:
``send`` / ``poll`` / ``evals`` / ``navigations`` / ``send_results`` /
``poll_results`` plus one ``<label>`` list and ``<label>_results`` queue per
configured locate probe.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from agent_reach.runtime import cdp, session_reuse

FB_HOME = "https://www.facebook.com/"


def locate_json(builder: Callable[[dict], dict]) -> Callable[[dict], str]:
    """``calls -> json.dumps(builder(calls))`` for a locate probe's canned reply."""

    def build(calls: dict) -> str:
        return json.dumps(builder(calls))

    return build


def locate(
    pattern: "str | re.Pattern",
    *,
    label: str,
    queue: str | None = None,
    empty: Callable[[dict], Any] | None = None,
    default: Callable[[dict], Any] | None = None,
    record: bool = True,
) -> dict:
    """One locate probe. ``queue`` names the ``calls`` key holding its result
    queue (``None`` = fixed reply, no queue); ``empty`` answers an exhausted /
    ``ALWAYS_NONE`` queue, ``default`` answers an unused one."""
    return {
        "re": re.compile(pattern) if isinstance(pattern, str) else pattern,
        "label": label,
        "queue": queue,
        "empty": empty,
        "default": default,
        "record": record,
    }


class FbWriteHarness:
    """Configurable fake-CDP harness for one facebook channel module.

    Class attributes are defaults; pass the same names as kwargs to
    ``__init__`` to override per module::

        FbWriteHarness(
            fc,
            poll_re=POLL_RE,
            send_re=SEND_CREATE_RE.pattern + "|" + SEND_DELETE_RE.pattern,
            locates=(...),
            strict=False,
            href_default=POST_URL,
            fallback_href=POST_URL,
            state={"uid": UID},
            extra_patches=_extra,
        )
    """

    module: Any = None
    #: extra modules whose ``_eval``/``_wait_ready`` bindings must be stubbed too
    #: (share borrows react's ``_ensure_post_loaded``, which evals through the
    #: react module's own ``_eval``).
    modules: tuple = ()
    poll_re: "str | re.Pattern | None" = None
    send_re: "str | re.Pattern | None" = None
    #: ordered locate probes — first match wins, mirroring each fixture's chain.
    locates: tuple = ()
    #: unmatched exprs raise AssertionError (write/react/share) when True, or
    #: fall through to a benign ``True`` (comment/post_edit) when False.
    strict: bool = True
    #: ``location.href`` stub: a string is mirrored into ``calls["href"]`` (tests
    #: may mutate that key); a callable receives ``calls``. ``None`` disables
    #: the branch entirely.
    href_default: Any = None
    fallback_href: str = FB_HOME
    #: extra ``calls`` keys seeded before the first eval (uid, privacy, rid…).
    state: dict = {}
    #: optional transform applied to a dequeued poll result before returning.
    poll_transform: Callable[[Any, dict], Any] | None = None
    #: regex lifting an id out of the send expr (react's ``@sent`` echo).
    rid_re: "str | re.Pattern | None" = None

    def __init__(self, module: Any = None, **overrides: Any) -> None:
        self.module = module if module is not None else type(self).module
        for key, value in overrides.items():
            if value is not None or key == "strict":
                setattr(self, key, value)

    # -- per-module escape hatches -----------------------------------------

    def extra_patches(self, monkeypatch, calls: dict) -> None:
        """Hook for module-specific stubs (``_ensure_post_loaded``, resolvers…)."""

    # -- fixture factory ---------------------------------------------------

    def patch(self, monkeypatch) -> dict:
        mod = self.module
        poll_re = self.poll_re if isinstance(self.poll_re, re.Pattern) else (
            re.compile(self.poll_re) if self.poll_re else None
        )
        send_re = self.send_re if isinstance(self.send_re, re.Pattern) else (
            re.compile(self.send_re) if self.send_re else None
        )
        rid_re = self.rid_re if isinstance(self.rid_re, re.Pattern) else (
            re.compile(self.rid_re) if self.rid_re else None
        )
        locs = tuple(spec for spec in self.locates)

        calls: dict[str, Any] = {
            "send": [],
            "poll": [],
            "evals": [],
            "navigations": [],
            "send_results": [{"sent": True}],
            "poll_results": [],
        }
        calls.update(self.state)
        for spec in locs:
            if spec["record"]:
                calls[spec["label"]] = []
            if spec["queue"]:
                calls[spec["queue"]] = []
        if isinstance(self.href_default, str):
            calls["href"] = self.href_default

        def fake_eval(target_id: str, expr: str, timeout: float) -> Any:
            calls["evals"].append(expr)
            if poll_re is not None:
                m = poll_re.match(expr)
                if m:
                    calls["poll"].append(m.group(1))
                    if not calls["poll_results"]:
                        return None
                    res = calls["poll_results"].pop(0)
                    if self.poll_transform is not None:
                        res = self.poll_transform(res, calls)
                    return res
            if send_re is not None and send_re.search(expr):
                calls["send"].append(expr)
                if rid_re is not None:
                    m2 = rid_re.search(expr)
                    if m2:
                        calls["rid"] = m2.group(1)
                return calls["send_results"].pop(0) if calls["send_results"] else {"sent": True}
            for spec in locs:
                if spec["re"].search(expr):
                    if spec["record"]:
                        calls[spec["label"]].append(expr)
                    if spec["queue"] is None:
                        return spec["default"](calls)
                    if calls[spec["queue"]]:
                        item = calls[spec["queue"]].pop(0)
                        if item == "ALWAYS_NONE":
                            calls[spec["queue"]].append(item)  # keep returning empty
                            return spec["empty"](calls)
                        return item
                    return spec["default"](calls)
            if self.href_default is not None and expr == "location.href":
                navs = calls["navigations"]
                if navs and navs[-1][0] == "Page.navigate":
                    return (navs[-1][1] or {}).get("url") or self.fallback_href
                hd = self.href_default
                if callable(hd):
                    return hd(calls)
                return calls["href"]
            if self.strict:
                raise AssertionError(f"unexpected eval expression: {expr[:160]}")
            # focus_expr / scrollIntoView probe and anything else: benign truthy.
            return True

        def fake_target(create: bool = True):
            return {"id": "T1", "type": "page", "url": "https://www.facebook.com/"}

        def fake_wait(target_id: str, timeout: float) -> None:
            return None

        def fake_prepare(channel, *, force: bool = False):
            return None

        def fake_cdp(method: str, params: dict | None = None, **kw: Any):
            calls["navigations"].append((method, params))
            return {}

        for target in (mod, *self.modules):
            monkeypatch.setattr(target, "_eval", fake_eval)
            monkeypatch.setattr(target, "_wait_ready", fake_wait)
        monkeypatch.setattr(mod, "_fb_target", fake_target)
        monkeypatch.setattr(session_reuse, "prepare", fake_prepare)
        monkeypatch.setattr(cdp, "call", fake_cdp)
        self.extra_patches(monkeypatch, calls)
        return calls

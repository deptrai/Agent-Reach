# -*- coding: utf-8 -*-
"""Threads write lane — REST replay classification + send shape (stubbed)."""

from __future__ import annotations

import pytest

from agent_reach.channels import _threads_write as tw
from agent_reach.runtime.result import (
    BAD_INPUT,
    NEED_LOGIN,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
)


def _stub_arm(monkeypatch, cookies=("sessionid", "ds_user_id", "csrftoken")):
    monkeypatch.setattr(tw, "prepare_threads", lambda: None)
    monkeypatch.setattr(tw, "_threads_target", lambda create: {"id": "T", "url": "https://www.threads.com/"})
    monkeypatch.setattr(tw, "_wait_ready", lambda *a, **k: None)

    class _CDP:
        @staticmethod
        def cookies(domain):
            return [{"name": n, "value": "v"} for n in cookies] if "threads" in domain else []

    monkeypatch.setattr(tw, "_session", lambda tid, dl: "csrftok" if "sessionid" in cookies else (_ for _ in ()).throw(ReachError(NEED_LOGIN, "x")))


def _minted():
    return {
        "params": {"av": "17841456940728410", "fb_dtsg": "TOK", "__s": "wsid:abc:123", "jazoest": "21"},
        "actor": "17841456940728410",
    }


def test_post_sends_configure_call_through_stubbed_eval(monkeypatch):
    sent = {}

    def fake_eval(_tid, expr, timeout=30):
        if "configure_text_only_post" in expr:
            sent["expr"] = expr
            return {"sent": True}
        if "getAsyncParams" in expr:
            return _minted()
        if expr.startswith("window["):
            return {
                "sent": True,
                "status": 200,
                "body": {"media": {"id": "3612345678901234567", "code": "AbC"}},
            }
        raise AssertionError(expr[:120])

    _stub_arm(monkeypatch)
    monkeypatch.setattr(tw, "_eval", fake_eval)
    items = tw.threads_post(text="real caption here", timeout=30)
    expr = sent["expr"]
    assert "/api/v1/media/configure_text_only_post/" in expr
    assert "publish_mode" in expr and "text_post" in expr
    assert "real caption here" in expr
    assert "x-csrftoken" in expr and "238260118697367" in expr
    assert "wsid:abc:123" in expr  # web_session_id from __s
    assert items[0]["text"] == "Posted"
    assert items[0]["url"] == "https://www.threads.com/_/post/AbC"
    assert items[0]["raw"]["media_id"] == "3612345678901234567"


def test_empty_and_overlong_captions_rejected():
    with pytest.raises(ReachError) as exc:
        tw.threads_post(text="   ", timeout=5)
    assert exc.value.code == BAD_INPUT
    with pytest.raises(ReachError) as exc:
        tw.threads_post(text="x" * 501, timeout=5)
    assert exc.value.code == BAD_INPUT


def test_checkpoint_maps_platform_blocked():
    out = {"sent": True, "status": 400, "body": {"message": "checkpoint_required", "checkpoint_url": "https://www.instagram.com/accounts/suspended/"}}
    with pytest.raises(ReachError) as exc:
        tw.classify_threads(out, confirmed=lambda: False)
    assert exc.value.code == PLATFORM_BLOCKED


def test_rate_limit_and_login_classification():
    with pytest.raises(ReachError) as exc:
        tw.classify_threads({"sent": True, "status": 429, "body": {"message": "rate limited"}}, confirmed=lambda: False)
    assert exc.value.code == RATE_LIMITED
    with pytest.raises(ReachError) as exc:
        tw.classify_threads({"sent": True, "status": 401, "body": {"message": "not logged in"}}, confirmed=lambda: False)
    assert exc.value.code == NEED_LOGIN


def test_confirmed_media_wins_over_error_shape():
    out = {"sent": True, "status": 200, "body": {"media": {"id": "3611", "code": "Z"}}}
    assert tw.classify_threads(out, confirmed=lambda: True) is out


def test_send_timeout_is_upstream_broken(monkeypatch):
    def fake_eval(_tid, expr, timeout=30):
        if "getAsyncParams" in expr:
            return _minted()
        if "configure_text_only_post" in expr:
            return {"sent": True}
        if expr.startswith("window["):
            return None
        raise AssertionError(expr[:120])

    _stub_arm(monkeypatch)
    monkeypatch.setattr(tw, "_eval", fake_eval)
    monkeypatch.setattr(tw.time, "sleep", lambda s: None)
    with pytest.raises(ReachError) as exc:
        tw.threads_post(text="hi", timeout=3)
    assert exc.value.code == UPSTREAM_BROKEN


def test_mint_failure_is_login_or_broken(monkeypatch):
    def fake_eval(_tid, expr, timeout=30):
        if "getAsyncParams" in expr:
            return {"error": "no_require"}
        raise AssertionError(expr[:120])

    _stub_arm(monkeypatch)
    monkeypatch.setattr(tw, "_eval", fake_eval)
    with pytest.raises(ReachError) as exc:
        tw.threads_post(text="hi", timeout=30)
    assert exc.value.code in (NEED_LOGIN, UPSTREAM_BROKEN)


def test_channel_wired():
    from agent_reach.channels import get_channel

    ch = get_channel("threads")
    assert ch is not None
    assert "post" in {a.name for a in ch.actions}
    assert ch.login_url.startswith("https://www.threads.com")

    from agent_reach.runtime.dispatch import WRITE_ACTIONS

    assert "post" in WRITE_ACTIONS.get("threads", ())

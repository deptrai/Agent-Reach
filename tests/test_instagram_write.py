"""Unit tests for the instagram comment/like write lane — no network, no browser."""

import json

import pytest

from agent_reach.channels import _instagram_write as iw
from agent_reach.channels.instagram import InstagramChannel
from agent_reach.runtime.dispatch import WRITE_ACTIONS
from agent_reach.runtime.result import BAD_INPUT, NEED_LOGIN, PLATFORM_BLOCKED, RATE_LIMITED, UPSTREAM_BROKEN, ReachError


def test_shortcode_media_id_roundtrip_known_pair():
    assert iw.shortcode_to_media_id("Df5AG_slAwS") == 4033255421997485074
    assert iw.media_id_to_shortcode("4033255421997485074") == "Df5AG_slAwS"


def test_resolve_media_id_accepts_id_url_and_shortcode():
    assert iw.resolve_media_id("4033255421997485074") == ("4033255421997485074", "Df5AG_slAwS")
    assert iw.resolve_media_id("https://www.instagram.com/p/Df5AG_slAwS/") == (
        "4033255421997485074",
        "Df5AG_slAwS",
    )
    assert iw.resolve_media_id("https://www.instagram.com/reel/Df5AG_slAwS/?igsh=abc") == (
        "4033255421997485074",
        "Df5AG_slAwS",
    )
    assert iw.resolve_media_id("Df5AG_slAwS") == ("4033255421997485074", "Df5AG_slAwS")


def test_resolve_media_id_bad_target():
    with pytest.raises(ReachError) as exc:
        iw.resolve_media_id("instagram.com/explore")
    assert exc.value.code == BAD_INPUT
    with pytest.raises(ReachError) as exc:
        iw.resolve_media_id("https://example.com/p/abc")
    assert exc.value.code == BAD_INPUT


def test_write_actions_registers_instagram_verbs_and_aliases():
    assert WRITE_ACTIONS["instagram"] == {"comment", "like", "unlike"}


def test_like_caps_under_react_class():
    from agent_reach.runtime import dispatch

    assert dispatch._write_class("instagram", "like") == "react"
    assert dispatch._write_class("instagram", "unlike") == "unlike"
    assert dispatch._write_class("instagram", "comment") == "comment"


def test_require_session_needs_sessionid_and_csrf():
    c = lambda n, v: {"name": n, "value": v}
    with pytest.raises(ReachError) as exc:
        iw.require_session([c("csrftoken", "t")])
    assert exc.value.code == NEED_LOGIN
    with pytest.raises(ReachError) as exc:
        iw.require_session([c("sessionid", "s"), c("ds_user_id", "1")])
    assert exc.value.code == NEED_LOGIN
    assert iw.require_session(
        [c("sessionid", "s"), c("ds_user_id", "1"), c("csrftoken", "t")]
    ) == ("t", "1")


def test_classify_http_and_terminal_blocks():
    with pytest.raises(ReachError) as exc:
        iw.classify_ig({"status": 429, "parsed": False}, confirmed=lambda: False)
    assert exc.value.code == RATE_LIMITED
    with pytest.raises(ReachError) as exc:
        iw.classify_ig({"status": 401, "parsed": False}, confirmed=lambda: False)
    assert exc.value.code == NEED_LOGIN
    with pytest.raises(ReachError) as exc:
        iw.classify_ig({"status": 503, "parsed": False}, confirmed=lambda: False)
    assert exc.value.code == UPSTREAM_BROKEN
    # checkpoint/challenge are terminal platform blocks — not retried, not bad input.
    with pytest.raises(ReachError) as exc:
        iw.classify_ig(
            {"status": 200, "parsed": True, "code": "checkpoint_required"},
            confirmed=lambda: False,
        )
    assert exc.value.code == PLATFORM_BLOCKED
    with pytest.raises(ReachError) as exc:
        iw.classify_ig(
            {"status": 200, "parsed": True, "code": "feedback_required"},
            confirmed=lambda: False,
        )
    assert exc.value.code == PLATFORM_BLOCKED


def test_classify_login_wall_html_and_not_found():
    with pytest.raises(ReachError) as exc:
        iw.classify_ig(
            {"status": 404, "parsed": False, "body": "<html>not-logged-in</html>"},
            confirmed=lambda: False,
        )
    assert exc.value.code == NEED_LOGIN
    with pytest.raises(ReachError) as exc:
        iw.classify_ig(
            {"status": 200, "parsed": True, "message": "media not found"},
            confirmed=lambda: False,
        )
    assert exc.value.code == BAD_INPUT
    with pytest.raises(ReachError) as exc:
        iw.classify_ig(
            {"status": 200, "parsed": True, "message": "Please wait a few minutes before you try again."},
            confirmed=lambda: False,
        )
    assert exc.value.code == RATE_LIMITED
    # confirmed signal wins over an unparseable oddity, mirroring reddit's contract.
    iw.classify_ig({"status": 200, "parsed": True, "ok": True}, confirmed=lambda: True)


def _stub_arm(monkeypatch):
    monkeypatch.setattr(iw, "prepare_instagram", lambda: None)
    monkeypatch.setattr(iw, "_ig_target", lambda create: {"id": "T", "url": "https://www.instagram.com/"})
    monkeypatch.setattr(iw, "_wait_ready", lambda *a, **k: None)
    monkeypatch.setattr(iw, "_session", lambda tid, deadline: ("csrftok", "14519691243"))


def test_comment_sends_form_through_stubbed_eval(monkeypatch):
    sent = {}

    def fake_eval(_tid, expr, timeout=30):
        if "/api/graphql" in expr:
            sent["expr"] = expr
            return {"sent": True}
        if expr.startswith("window["):
            return {
                "status": 200,
                "parsed": True,
                "ok": True,
                "commentId": "18012345678901234",
            }
        raise AssertionError(expr[:120])

    _stub_arm(monkeypatch)
    monkeypatch.setattr(iw, "_eval", fake_eval)
    items = iw.ig_comment(target="https://www.instagram.com/p/Df5AG_slAwS/", text="nice post", timeout=30)
    expr = sent["expr"]
    assert "usePolarisCommentCreateMutation" in expr
    assert "comment_text" in expr and "nice post" in expr
    assert "4033255421997485074" in expr
    assert items[0]["text"] == "Commented"
    assert items[0]["url"] == "https://www.instagram.com/p/Df5AG_slAwS/#c18012345678901234"
    assert items[0]["raw"]["media_id"] == "4033255421997485074"


def test_like_and_unlike_paths_and_dup_guard(monkeypatch):
    sent = {}

    def fake_eval(_tid, expr, timeout=30):
        if "/api/graphql" in expr:
            sent["expr"] = expr
            return {"sent": True, "dup": True}  # dedup sentinel short-circuits
        if expr.startswith("window["):
            return {"status": 200, "parsed": True, "ok": True, "liked": True, "unliked": True}
        raise AssertionError(expr[:120])

    _stub_arm(monkeypatch)
    monkeypatch.setattr(iw, "_eval", fake_eval)
    items = iw.ig_like(target="Df5AG_slAwS", unlike=False, timeout=30)
    assert "usePolarisLikeMediaXIGLikeMutation" in sent["expr"]
    assert "4033255421997485074" in sent["expr"]
    assert items[0]["text"] == "Liked"
    iw.ig_like(target="Df5AG_slAwS", unlike=True, timeout=30)
    assert "usePolarisLikeMediaXIGUnlikeMutation" in sent["expr"]


def test_send_no_result_is_upstream_broken(monkeypatch):
    def fake_eval(_tid, expr, timeout=30):
        if "/api/graphql" in expr:
            return {"sent": True}
        if expr.startswith("window["):
            return None
        raise AssertionError(expr[:120])

    _stub_arm(monkeypatch)
    monkeypatch.setattr(iw, "_eval", fake_eval)
    monkeypatch.setattr(iw.time, "sleep", lambda s: None)
    with pytest.raises(ReachError) as exc:
        iw.ig_like(target="Df5AG_slAwS", unlike=False, timeout=3)
    assert exc.value.code == UPSTREAM_BROKEN


def test_post_is_descoped_with_clear_error():
    ch = InstagramChannel()
    with pytest.raises(ReachError) as exc:
        ch._post(timeout=1)
    assert exc.value.code == BAD_INPUT
    assert "descoped" in exc.value.message

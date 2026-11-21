"""Unit tests for the reddit post/comment write lane — no network, no browser."""

import pytest

from agent_reach.channels import _reddit_write as rw
from agent_reach.runtime.dispatch import WRITE_ACTIONS
from agent_reach.runtime.result import BAD_INPUT, NEED_LOGIN, RATE_LIMITED, ReachError


def test_submit_self_fields_include_sr_kind_title_uh():
    fields = rw.build_submit_fields(subreddit="r/test", title="hello", text="body", url=None, uh="mod1")
    assert fields["sr"] == "test"
    assert fields["kind"] == "self"
    assert fields["title"] == "hello"
    assert fields["uh"] == "mod1"
    assert fields["text"] == "body"
    assert fields["api_type"] == "json"


def test_submit_link_and_profile_sr():
    link = rw.build_submit_fields(
        subreddit="test", title="a link", text=None, url="https://example.com/x", uh="m"
    )
    assert link["kind"] == "link"
    assert link["url"] == "https://example.com/x"
    assert "text" not in link
    profile = rw.build_submit_fields(
        subreddit="u/Objective_Bet_368", title="t", text="b", url=None, uh="m"
    )
    assert profile["sr"] == "u_Objective_Bet_368"


def test_success_parse_url_present_errors_empty():
    out = {
        "status": 200,
        "parsed": True,
        "errors": [],
        "url": "https://www.reddit.com/r/test/comments/abc/title/",
        "name": "t3_abc",
    }
    rw.classify_reddit(out, confirmed=lambda: bool(out.get("url")))
    assert out["url"]
    assert out["errors"] == []


def test_ratelimit_maps_to_rate_limited():
    out = {
        "status": 200,
        "parsed": True,
        "errors": [["RATELIMIT", "you are doing that too much. try again in 7 minutes.", "ratelimit"]],
        "url": None,
    }
    with pytest.raises(ReachError) as exc:
        rw.classify_reddit(out, confirmed=lambda: False)
    assert exc.value.code == RATE_LIMITED


def test_no_modhash_maps_to_need_login():
    with pytest.raises(ReachError) as exc:
        rw.require_session({"status": 200, "name": None, "modhash": ""})
    assert exc.value.code == NEED_LOGIN
    with pytest.raises(ReachError) as exc:
        rw.require_session({"status": 200, "name": "someone", "modhash": ""})
    assert exc.value.code == NEED_LOGIN


def test_validation_error_maps_to_bad_input():
    out = {
        "status": 200,
        "parsed": True,
        "errors": [["SUBREDDIT_NOEXIST", "that subreddit doesn't exist", "sr"]],
        "url": None,
    }
    with pytest.raises(ReachError) as exc:
        rw.classify_reddit(out, confirmed=lambda: False)
    assert exc.value.code == BAD_INPUT
    out["errors"] = [["NO_TEXT", "we need something here", "text"]]
    with pytest.raises(ReachError) as exc:
        rw.classify_reddit(out, confirmed=lambda: False)
    assert exc.value.code == BAD_INPUT


def test_comment_payload():
    fields = rw.build_comment_fields(thing_id="t3_abc", text="nice post", uh="mod1")
    assert fields == {"api_type": "json", "uh": "mod1", "thing_id": "t3_abc", "text": "nice post"}
    with pytest.raises(ReachError) as exc:
        rw.build_comment_fields(thing_id="abc", text="x", uh="m")
    assert exc.value.code == BAD_INPUT


def test_write_actions_registers_reddit_post_and_comment():
    assert WRITE_ACTIONS["reddit"] == {"post", "comment"}


def test_post_sends_form_through_stubbed_eval(monkeypatch):
    sent = {}

    def fake_eval(_tid, expr, timeout=30):
        if "/api/me.json" in expr:
            return {"status": 200, "name": "Objective_Bet_368", "modhash": "uh1"}
        if "/api/submit" in expr:
            sent["expr"] = expr
            return {"sent": True}
        if expr.startswith("window["):
            return {
                "status": 200,
                "parsed": True,
                "errors": [],
                "url": "https://www.reddit.com/r/test/comments/abc/hello.json",
                "name": "t3_abc",
                "id": "abc",
            }
        raise AssertionError(expr[:120])

    monkeypatch.setattr(rw, "_eval", fake_eval)
    monkeypatch.setattr(rw, "prepare_reddit", lambda: None)
    monkeypatch.setattr(rw, "_reddit_target", lambda create: {"id": "T", "url": "https://www.reddit.com/"})
    monkeypatch.setattr(rw, "_wait_ready", lambda *a, **k: None)
    items = rw.reddit_post(subreddit="test", title="hello", text="body text", timeout=30)
    expr = sent["expr"]
    assert "sr=test" in expr
    assert "kind=self" in expr
    assert "title=hello" in expr
    assert "uh1" in expr
    assert items[0]["url"] == "https://www.reddit.com/r/test/comments/abc/hello"
    assert items[0]["raw"]["name"] == "t3_abc"

# -*- coding: utf-8 -*-
"""facebook share — GraphQL mutation replay tests.

`tests/test_facebook_share.py` mirrors `test_facebook_react.py`: the browser
is stubbed — `fs._eval`/`fr._eval` return canned results routed by the
expression's shape (send expr contains the mutation name, poll expr is the
per-call window key, locate expr collects post_id values, `location.href`
reports the faked tab location). `_ensure_post_loaded` runs for real so the
navigate/wait behaviour is exercised end to end.
"""

import json
import re

import pytest

import agent_reach.channels._facebook_react as fr
import agent_reach.channels._facebook_share as fs
from agent_reach.runtime import session_reuse
from agent_reach.runtime.result import BAD_INPUT, NEED_LOGIN, RATE_LIMITED, UPSTREAM_BROKEN, ReachError

POST_URL = "https://www.facebook.com/61554949615037/posts/122311659992164987"
POST_ID = "122311659992164987"
SHARE_URL = "https://www.facebook.com/61554949615037/posts/999888777666555"

SEND_RE = re.compile(r"ComposerStoryCreateMutation")
POLL_RE = re.compile(r'^window\["(__fbShareResult_[0-9a-f]{32})"\]\|\|null$')
# Locate expr collects numeric post ids: "post_id":"([0-9]{8,})"
LOCATE_PID_RE = re.compile(r'post_id[^\n]{0,40}\[0-9\]\{8,\}')


@pytest.fixture
def harness(monkeypatch):
    """Returns a calls dict. Queues are consumed in order."""
    calls = {"send": [], "locate": [], "poll": [], "evals": [], "navigations": []}
    calls["locate_results"] = []
    calls["send_results"] = [{"sent": True}]
    calls["poll_results"] = []

    def fake_eval(target_id, expr, timeout):
        calls["evals"].append(expr)
        m = POLL_RE.match(expr)
        if m:
            calls["poll"].append(m.group(1))
            if not calls["poll_results"]:
                return None
            return calls["poll_results"].pop(0)
        if SEND_RE.search(expr):
            calls["send"].append(expr)
            return calls["send_results"].pop(0) if calls["send_results"] else {"sent": True}
        if LOCATE_PID_RE.search(expr):
            calls["locate"].append(expr)
            if calls["locate_results"]:
                item = calls["locate_results"].pop(0)
                if item == "ALWAYS_NONE":
                    calls["locate_results"].append(item)  # keep returning no pid
                    return json.dumps({"href": POST_URL, "pid": None, "count": 0})
                return item
            return json.dumps({"href": POST_URL, "pid": "555777999888", "count": 1})
        if expr == "location.href":
            navs = calls["navigations"]
            if navs and navs[-1][0] == "Page.navigate":
                return (navs[-1][1] or {}).get("url") or POST_URL
            return POST_URL
        raise AssertionError(f"unexpected eval expression: {expr[:160]}")

    for mod in (fs, fr):
        monkeypatch.setattr(mod, "_eval", fake_eval)
        monkeypatch.setattr(mod, "_wait_ready", lambda tid, timeout: None)
    monkeypatch.setattr(fs, "_fb_target", lambda create: {"id": "T1", "type": "page", "url": "https://www.facebook.com/"})
    monkeypatch.setattr(session_reuse, "prepare", lambda channel, *, force=False: None)
    from agent_reach.runtime import cdp

    monkeypatch.setattr(cdp, "call", lambda method, params=None, **kw: calls["navigations"].append((method, params)) or {})
    return calls


def ok_result(**over):
    base = {
        "status": 200,
        "parsed": True,
        "errors": None,
        "storyUrl": SHARE_URL,
        "storyId": "999888777666555",
        "postId": None,
        "newPostId": None,
        "actorId": "61554949615037",
    }
    base.update(over)
    return base


def test_share_success_returns_item(harness):
    harness["poll_results"] = [ok_result()]
    items = fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert len(items) == 1
    assert items[0]["url"] == SHARE_URL
    assert items[0]["text"] == "Shared (only_me)"
    assert items[0]["raw"]["sourcePostId"] == POST_ID
    assert items[0]["raw"]["storyId"] == "999888777666555"
    send = harness["send"][0]
    assert '"share_type":22' in send
    # id is concatenated as a string — Number() would round past 2^53
    assert 'share_params":[' in send
    assert f'+{POST_ID}+' in send


@pytest.mark.parametrize("audience,state", [
    ("only_me", "SELF"),
    ("friends", "FRIENDS"),
    ("public", "EVERYONE"),
])
def test_audience_mapping(harness, audience, state):
    harness["poll_results"] = [ok_result()]
    items = fs.facebook_share(url=POST_URL, text="", audience=audience, timeout=30)
    assert f'base_state:"{state}"' in harness["send"][0]
    assert items[0]["text"] == f"Shared ({audience})"


def test_text_lands_in_message(harness):
    harness["poll_results"] = [ok_result()]
    fs.facebook_share(url=POST_URL, text='chia sẻ "hay" ✍️', audience="friends", timeout=30)
    assert 'text:"chia sẻ \\"hay\\" ✍️"' in harness["send"][0]


def test_text_substituted_last_does_not_clobber_post(harness):
    """User text containing a sentinel-like token must not eat __FB_POST."""
    harness["poll_results"] = [ok_result()]
    fs.facebook_share(url=POST_URL, text="__FB_POST", audience="only_me", timeout=30)
    send = harness["send"][0]
    assert 'text:"__FB_POST"' in send
    assert f'+{POST_ID}+' in send


def test_bad_audience_rejected_before_browser(harness):
    with pytest.raises(ReachError) as exc:
        fs.facebook_share(url=POST_URL, text="", audience="banana", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_non_facebook_url_rejected_before_browser(harness):
    with pytest.raises(ReachError) as exc:
        fs.facebook_share(url="https://example.com/posts/1", text="", audience="only_me", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_no_navigation_when_tab_already_on_post(harness):
    harness["poll_results"] = [ok_result()]
    fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert harness["navigations"] == []


def test_navigates_when_tab_on_another_page(harness, monkeypatch):
    def fake_eval(target_id, expr, timeout):
        calls = harness
        calls["evals"].append(expr)
        if expr == "location.href":
            navs = calls["navigations"]
            if navs and navs[-1][0] == "Page.navigate":
                return (navs[-1][1] or {}).get("url") or POST_URL
            return "https://www.facebook.com/"
        m = POLL_RE.match(expr)
        if m:
            calls["poll"].append(m.group(1))
            return calls["poll_results"].pop(0) if calls["poll_results"] else None
        if SEND_RE.search(expr):
            calls["send"].append(expr)
            return {"sent": True}
        raise AssertionError(f"unexpected eval expression: {expr[:160]}")

    monkeypatch.setattr(fs, "_eval", fake_eval)
    monkeypatch.setattr(fr, "_eval", fake_eval)
    harness["poll_results"] = [ok_result()]
    fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert ("Page.navigate", {"url": POST_URL}) in harness["navigations"]


def test_post_id_resolved_from_page_when_url_has_none(harness):
    """pfbid URLs carry no numeric id — resolve from page's first post_id."""
    harness["poll_results"] = [ok_result()]
    harness["locate_results"] = [json.dumps({"href": POST_URL, "pid": "555777999888", "count": 1})]
    pfbid_url = "https://www.facebook.com/SomePage/posts/pfbid02abcXYZ"
    fs.facebook_share(url=pfbid_url, text="", audience="only_me", timeout=30)
    assert harness["locate"], "expected the post_id locate eval"
    assert '+555777999888+' in harness["send"][0]


def test_no_post_id_anywhere_is_upstream_broken(harness):
    pfbid_url = "https://www.facebook.com/SomePage/posts/pfbid02abcXYZ"
    harness["locate_results"] = ["ALWAYS_NONE"]
    with pytest.raises(ReachError) as exc:
        fs.facebook_share(url=pfbid_url, text="", audience="only_me", timeout=5)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "post id" in str(exc.value).lower()
    assert harness["send"] == []


def test_no_tokens_without_uid_is_need_login_immediately(harness):
    harness["send_results"] = [{"error": "no_tokens", "hasUid": False}]
    with pytest.raises(ReachError) as exc:
        fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert exc.value.code == NEED_LOGIN
    assert len(harness["send"]) == 1


def test_no_tokens_with_uid_retries_until_sent(harness):
    harness["send_results"] = [
        {"error": "no_tokens", "hasUid": True},
        {"error": "no_tokens", "hasUid": True},
        {"sent": True},
    ]
    harness["poll_results"] = [ok_result()]
    items = fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert len(harness["send"]) == 3
    assert items


def test_sentinel_guard_in_send_js(harness):
    harness["poll_results"] = [ok_result()]
    fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    send = harness["send"][0]
    assert 'window[KEY+"_sent"]' in send
    assert 'window[KEY+"_sent"]=true' in send


def test_per_call_result_keys_differ(harness):
    harness["poll_results"] = [ok_result(), ok_result()]
    fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert len(harness["poll"]) == 2
    assert harness["poll"][0] != harness["poll"][1]
    assert all(k.startswith("__fbShareResult_") for k in harness["poll"])


def test_http_429_maps_to_rate_limited(harness):
    harness["poll_results"] = [ok_result(status=429, parsed=False, body="rate limit")]
    with pytest.raises(ReachError) as exc:
        fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert exc.value.code == RATE_LIMITED


def test_http_5xx_maps_to_upstream_broken(harness):
    harness["poll_results"] = [ok_result(status=503, parsed=False, body="oops")]
    with pytest.raises(ReachError) as exc:
        fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_graphql_errors_map_to_upstream_broken(harness):
    harness["poll_results"] = [
        ok_result(storyUrl=None, storyId=None, errors=[{"message": "The story is no longer available"}])
    ]
    with pytest.raises(ReachError) as exc:
        fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_login_error_in_graphql_maps_to_need_login(harness):
    harness["poll_results"] = [ok_result(storyUrl=None, storyId=None, errors=[{"message": "Session invalid — please login"}])]
    with pytest.raises(ReachError) as exc:
        fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert exc.value.code == NEED_LOGIN


def test_unparseable_body_is_upstream_broken(harness):
    harness["poll_results"] = [ok_result(status=200, parsed=False, body="<html>weird</html>")]
    with pytest.raises(ReachError) as exc:
        fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_login_words_in_body_map_to_need_login(harness):
    harness["poll_results"] = [ok_result(status=200, parsed=False, body="...login_form...")]
    with pytest.raises(ReachError) as exc:
        fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert exc.value.code == NEED_LOGIN


def test_data_without_story_is_upstream_broken(harness):
    """Fail-closed: parsed data but no story/newPostId anywhere."""
    harness["poll_results"] = [ok_result(storyUrl=None, storyId=None, postId=None, newPostId=None)]
    with pytest.raises(ReachError) as exc:
        fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_story_url_fallback_from_story_id(harness):
    harness["poll_results"] = [ok_result(storyUrl=None)]
    items = fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert items[0]["url"] == "https://www.facebook.com/permalink.php?story_fbid=999888777666555&id=61554949615037"


def test_send_error_maps_to_upstream_broken(harness):
    harness["send_results"] = [{"error": "some_js_boom"}]
    with pytest.raises(ReachError) as exc:
        fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_no_poll_result_is_upstream_broken(harness):
    with pytest.raises(ReachError) as exc:
        fs.facebook_share(url=POST_URL, text="", audience="only_me", timeout=5)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "no result" in str(exc.value).lower()


def test_dispatch_wires_share_action(harness):
    """The channel exposes `share` and routes it to facebook_share."""
    from agent_reach.channels.facebook import FacebookChannel

    ch = FacebookChannel()
    actions = {a.name: a for a in ch.actions}
    assert "share" in actions
    params = {p.name: p for p in actions["share"].params}
    assert params["audience"].default == "only_me"
    assert params["text"].default == ""
    with pytest.raises(ReachError) as exc:
        ch._share(url=POST_URL, text="", audience="banana", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []

"""Unit tests for the facebook `post` write path — no real browser involved.

`facebook_post` is exercised end-to-end with `session_reuse`, `_fb_target`,
`_wait_ready` and `_eval` stubbed; the fake `_eval` inspects each expression
and returns queued results for the send eval and the per-call result poll.
"""

import json
import re

import pytest

from agent_reach.channels import _facebook_write as fw
from agent_reach.runtime.result import NEED_LOGIN, RATE_LIMITED, UPSTREAM_BROKEN, ReachError
from conftest_facebook import FbWriteHarness

SEND_RE = re.compile(r"ComposerStoryCreateMutation")
POLL_RE = re.compile(r'^window\["(__fbPostResult_[0-9a-f]{32})"\]\|\|null$')


@pytest.fixture
def harness(monkeypatch):
    """Stubbed browser; queues are consumed in order. An empty poll queue makes
    `_eval` return None forever (never a result)."""
    return FbWriteHarness(fw, poll_re=POLL_RE, send_re=SEND_RE).patch(monkeypatch)


def ok_result(**over):
    base = {
        "status": 200,
        "parsed": True,
        "errors": None,
        "storyUrl": "https://www.facebook.com/permalink.php?story_fbid=111&id=615",
        "storyId": "111",
        "newPostId": "615_111",
        "actorId": "615",
    }
    base.update(over)
    return base


def test_post_success_returns_story_permalink(harness):
    harness["poll_results"] = [ok_result()]
    items = fw.facebook_post(text="hello world", audience="only_me", timeout=30)
    assert items[0]["url"] == "https://www.facebook.com/permalink.php?story_fbid=111&id=615"
    assert items[0]["title"] == "hello world"
    assert items[0]["raw"]["newPostId"] == "615_111"


def test_permalink_built_from_numeric_story_id_when_url_missing(harness):
    harness["poll_results"] = [ok_result(storyUrl=None, storyId="456", actorId="999")]
    items = fw.facebook_post(text="x", audience="only_me", timeout=30)
    assert items[0]["url"] == "https://www.facebook.com/permalink.php?story_fbid=456&id=999"


def test_post_without_any_story_reference_is_an_error_not_a_silent_success(harness):
    harness["poll_results"] = [ok_result(storyUrl=None, storyId=None, newPostId=None)]
    with pytest.raises(ReachError) as exc:
        fw.facebook_post(text="x", audience="only_me", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


@pytest.mark.parametrize("audience,base_state", [
    ("only_me", "SELF"),
    ("friends", "FRIENDS"),
    ("public", "EVERYONE"),
])
def test_audience_maps_to_facebook_base_state(harness, audience, base_state):
    harness["poll_results"] = [ok_result()]
    fw.facebook_post(text="x", audience=audience, timeout=30)
    assert f'base_state:{json.dumps(base_state)}' in harness["send"][0]


def test_no_tokens_without_uid_is_need_login_immediately(harness):
    harness["send_results"] = [{"error": "no_tokens", "hasUid": False}]
    with pytest.raises(ReachError) as exc:
        fw.facebook_post(text="x", audience="only_me", timeout=30)
    assert exc.value.code == NEED_LOGIN
    assert len(harness["send"]) == 1


def test_no_tokens_with_uid_retries_until_send(harness):
    harness["send_results"] = [
        {"error": "no_tokens", "hasUid": True},
        {"error": "no_tokens", "hasUid": True},
        {"sent": True},
    ]
    harness["poll_results"] = [ok_result()]
    items = fw.facebook_post(text="x", audience="only_me", timeout=30)
    assert items and len(harness["send"]) == 3


def test_http_429_maps_to_rate_limited(harness):
    harness["poll_results"] = [{"status": 429, "parsed": False, "body": "rate limited"}]
    with pytest.raises(ReachError) as exc:
        fw.facebook_post(text="x", audience="only_me", timeout=30)
    assert exc.value.code == RATE_LIMITED


def test_http_5xx_maps_to_upstream_broken(harness):
    harness["poll_results"] = [{"status": 503, "parsed": False, "body": "oops"}]
    with pytest.raises(ReachError) as exc:
        fw.facebook_post(text="x", audience="only_me", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_graphql_errors_map_to_upstream_broken(harness):
    harness["poll_results"] = [
        ok_result(storyUrl=None, storyId=None, newPostId=None, postId=None, errors=[{"message": "missing_required_variable_value"}])
    ]
    with pytest.raises(ReachError) as exc:
        fw.facebook_post(text="x", audience="only_me", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "missing_required_variable_value" in exc.value.message


def test_login_shaped_graphql_error_maps_to_need_login(harness):
    harness["poll_results"] = [
        ok_result(storyUrl=None, storyId=None, newPostId=None, postId=None, errors=[{"message": "Login required to continue"}])
    ]
    with pytest.raises(ReachError) as exc:
        fw.facebook_post(text="x", audience="only_me", timeout=30)
    assert exc.value.code == NEED_LOGIN


def test_unparseable_login_page_maps_to_need_login(harness):
    harness["poll_results"] = [{"status": 200, "parsed": False, "body": "<form class=login_form>"}]
    with pytest.raises(ReachError) as exc:
        fw.facebook_post(text="x", audience="only_me", timeout=30)
    assert exc.value.code == NEED_LOGIN


def test_unparseable_other_body_maps_to_upstream_broken(harness):
    harness["poll_results"] = [{"status": 200, "parsed": False, "body": "<html>maintenance</html>"}]
    with pytest.raises(ReachError) as exc:
        fw.facebook_post(text="x", audience="only_me", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_user_text_containing_a_sentinel_is_not_resubstituted(harness):
    # "__FB_AUD" inside the post text must not be replaced by the audience.
    text = 'posting literally __FB_AUD and "__FB_DOC" verbatim'
    harness["poll_results"] = [ok_result()]
    fw.facebook_post(text=text, audience="friends", timeout=30)
    expr = harness["send"][0]
    # The escaped user text appears verbatim inside the message assignment,
    # and the audience placeholder was still substituted correctly.
    assert json.dumps(text) in expr
    assert 'base_state:"FRIENDS"' in expr


def test_each_call_polls_its_own_result_key(harness):
    harness["poll_results"] = [ok_result(), ok_result()]
    fw.facebook_post(text="one", audience="only_me", timeout=30)
    fw.facebook_post(text="two", audience="only_me", timeout=30)
    assert len(harness["poll"]) == 2
    assert harness["poll"][0] != harness["poll"][1]
    # The send evals carry the same key the poll later reads.
    for expr, key in zip(harness["send"], harness["poll"]):
        assert json.dumps(key) in expr


def test_missing_result_is_upstream_broken_with_duplicate_warning(harness):
    harness["poll_results"] = []  # _eval returns None until the deadline
    with pytest.raises(ReachError) as exc:
        fw.facebook_post(text="x", audience="only_me", timeout=0.6)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "duplicates" in exc.value.message


def test_bad_audience_is_rejected_before_any_browser_work(harness):
    from agent_reach.channels.facebook import FacebookChannel

    with pytest.raises(ReachError) as exc:
        FacebookChannel()._post(text="x", audience="everyone", timeout=30)
    assert exc.value.code == "BAD_INPUT"
    assert harness["evals"] == []


def test_is_facebook_url_parses_hostname_not_substring():
    from agent_reach.channels._facebook_write import _is_facebook_url

    assert _is_facebook_url("https://www.facebook.com/story")
    assert _is_facebook_url("https://m.facebook.com/")
    assert _is_facebook_url("https://fb.watch/abc")
    assert not _is_facebook_url("https://notfacebook.com.evil.example/")
    assert not _is_facebook_url("https://facebook.com.evil.example/")
    assert not _is_facebook_url("")
    assert not _is_facebook_url("about:blank")

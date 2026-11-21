"""Unit tests for the facebook `react` / `unreact` write path — no real browser.

`facebook_react` / `facebook_unreact` are exercised end-to-end with
`session_reuse`, `_fb_target`, `_wait_ready` and `_eval` stubbed; the fake
`_eval` inspects each expression and returns queued results for the
feedback_id lookup, the send eval and the per-call result poll.
"""

import json
import re

import pytest

from agent_reach.channels import _facebook_react as fr
from agent_reach.runtime.result import (
    BAD_INPUT,
    NEED_LOGIN,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
)
from conftest_facebook import FbWriteHarness, locate, locate_json

POST_URL = "https://www.facebook.com/61554949615037/posts/122311659992164987"
SEND_RE = re.compile(r"CometUFIFeedbackReactMutation")
POLL_RE = re.compile(r'^window\["(__fbReactResult_[0-9a-f]{32})"\]\|\|null$')
LOCATE_RE = re.compile(r'"feedback_id":"\(\[\^"\]\+\)"')
RID_RE = re.compile(r'feedback_reaction_id="(\d+)"')


def _echo_sent_reaction(res, calls):
    """``@sent`` = echo the reaction id actually sent, like FB does."""
    if isinstance(res, dict) and res.get("reactionId") == "@sent":
        rid = calls["rid"]
        return dict(res, reactionId=None if rid == "0" else rid)
    return res


@pytest.fixture
def harness(monkeypatch):
    """Stubbed browser; queues are consumed in order. An empty poll queue makes
    `_eval` return None forever (never a result)."""
    return FbWriteHarness(
        fr,
        poll_re=POLL_RE,
        send_re=SEND_RE,
        rid_re=RID_RE,
        poll_transform=_echo_sent_reaction,
        locates=(
            locate(
                LOCATE_RE,
                label="locate",
                queue="locate_results",
                empty=locate_json(lambda c: {"href": POST_URL, "fid": None, "count": 0}),
                default=locate_json(lambda c: {"href": POST_URL, "fid": "FID-POST-1", "count": 1}),
            ),
        ),
        href_default=POST_URL,
        fallback_href=POST_URL,
        state={"rid": None},
    ).patch(monkeypatch)


def ok_result(**over):
    base = {
        "status": 200,
        "parsed": True,
        "errors": None,
        "reactionId": "@sent",  # harness echoes the rid actually sent
        "reactionCount": 1,
        "actorId": "61554949615037",
    }
    base.update(over)
    return base


def test_react_success_returns_item(harness):
    harness["poll_results"] = [ok_result()]
    items = fr.facebook_react(url=POST_URL, reaction="love", timeout=30)
    assert len(items) == 1
    assert items[0]["url"] == POST_URL
    assert items[0]["raw"]["reactionId"] == "1678524932434102"
    # The send eval carries the reaction id and the resolved feedback_id.
    assert 'feedback_reaction_id="1678524932434102"' in harness["send"][0]
    assert '"FID-POST-1"' in harness["send"][0]


def test_unreact_sends_reaction_id_zero(harness):
    harness["poll_results"] = [ok_result(reactionId=None, reactionCount=0)]
    items = fr.facebook_unreact(url=POST_URL, timeout=30)
    assert items[0]["raw"]["url"] == POST_URL
    assert 'feedback_reaction_id="0"' in harness["send"][0]


@pytest.mark.parametrize("reaction,rid", [
    ("like", "1635855486666999"),
    ("love", "1678524932434102"),
    ("haha", "115940658764963"),
    ("wow", "478547315650144"),
    ("sad", "908563459236466"),
    ("angry", "444813342392137"),
])
def test_every_reaction_type_maps_to_its_id(harness, reaction, rid):
    harness["poll_results"] = [ok_result()]
    fr.facebook_react(url=POST_URL, reaction=reaction, timeout=30)
    assert f'feedback_reaction_id="{rid}"' in harness["send"][0]


def test_bad_reaction_rejected_before_browser_work(harness):
    with pytest.raises(ReachError) as exc:
        fr.facebook_react(url=POST_URL, reaction="dislike", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_non_facebook_url_rejected_before_browser_work(harness):
    with pytest.raises(ReachError) as exc:
        fr.facebook_react(url="https://example.com/post/1", reaction="like", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_no_feedback_id_on_page_is_upstream_broken(harness):
    harness["locate_results"] = ["ALWAYS_NONE"]
    with pytest.raises(ReachError) as exc:
        fr.facebook_react(url=POST_URL, reaction="like", timeout=1.5)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "feedback_id" in exc.value.message


def test_comment_id_is_substituted_into_locate_expr(harness):
    url = POST_URL + "?comment_id=123456789"
    harness["poll_results"] = [ok_result()]
    # Tab shows the post without the comment → navigate, then bind the comment.
    fr.facebook_react(url=url, reaction="like", timeout=30)
    assert any('"123456789"' in e for e in harness["locate"])


def test_navigates_when_tab_on_another_page(monkeypatch, harness):
    # location.href returns the facebook.com home until the navigate lands.
    orig = fr._eval

    def fake_eval2(target_id, expr, timeout):
        if expr == "location.href":
            navs = harness["navigations"]
            if navs:
                return (navs[-1][1] or {}).get("url") or POST_URL
            return "https://www.facebook.com/"
        return orig(target_id, expr, timeout)

    monkeypatch.setattr(fr, "_eval", fake_eval2)
    harness["poll_results"] = [ok_result()]
    fr.facebook_react(url=POST_URL, reaction="like", timeout=30)
    assert any(m == "Page.navigate" and p.get("url") == POST_URL for m, p in harness["navigations"])


def test_navigates_when_comment_id_differs(monkeypatch, harness):
    # Same post path but the tab shows no comment while the URL targets one.
    orig = fr._eval

    def fake_eval2(target_id, expr, timeout):
        if expr == "location.href":
            navs = harness["navigations"]
            if navs:
                return (navs[-1][1] or {}).get("url") or POST_URL
            return POST_URL  # same path, no comment_id
        return orig(target_id, expr, timeout)

    monkeypatch.setattr(fr, "_eval", fake_eval2)
    harness["poll_results"] = [ok_result()]
    url = POST_URL + "?comment_id=123456789"
    fr.facebook_react(url=url, reaction="like", timeout=30)
    assert any(m == "Page.navigate" and p.get("url") == url for m, p in harness["navigations"])


def test_no_navigation_when_same_post_and_comment(monkeypatch, harness):
    harness["poll_results"] = [ok_result()]
    fr.facebook_react(url=POST_URL, reaction="like", timeout=30)
    assert harness["navigations"] == []


def test_no_tokens_without_uid_is_need_login_immediately(harness):
    harness["send_results"] = [{"error": "no_tokens", "hasUid": False}]
    with pytest.raises(ReachError) as exc:
        fr.facebook_react(url=POST_URL, reaction="like", timeout=30)
    assert exc.value.code == NEED_LOGIN
    assert len(harness["send"]) == 1


def test_no_tokens_with_uid_retries_until_send(harness):
    harness["send_results"] = [
        {"error": "no_tokens", "hasUid": True},
        {"error": "no_tokens", "hasUid": True},
        {"sent": True},
    ]
    harness["poll_results"] = [ok_result()]
    items = fr.facebook_react(url=POST_URL, reaction="like", timeout=30)
    assert items and len(harness["send"]) == 3


def test_http_429_maps_to_rate_limited(harness):
    harness["poll_results"] = [{"status": 429, "parsed": False, "body": "rate limited"}]
    with pytest.raises(ReachError) as exc:
        fr.facebook_react(url=POST_URL, reaction="like", timeout=30)
    assert exc.value.code == RATE_LIMITED


def test_http_5xx_maps_to_upstream_broken(harness):
    harness["poll_results"] = [{"status": 503, "parsed": False, "body": "oops"}]
    with pytest.raises(ReachError) as exc:
        fr.facebook_react(url=POST_URL, reaction="like", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_graphql_errors_map_to_upstream_broken(harness):
    harness["poll_results"] = [ok_result(reactionId=None, errors=[{"message": "missing_required_variable_value"}])]
    with pytest.raises(ReachError) as exc:
        fr.facebook_react(url=POST_URL, reaction="like", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "missing_required_variable_value" in exc.value.message


def test_login_shaped_graphql_error_maps_to_need_login(harness):
    harness["poll_results"] = [ok_result(reactionId=None, errors=[{"message": "Login required to continue"}])]
    with pytest.raises(ReachError) as exc:
        fr.facebook_react(url=POST_URL, reaction="like", timeout=30)
    assert exc.value.code == NEED_LOGIN


def test_unparseable_login_page_maps_to_need_login(harness):
    harness["poll_results"] = [{"status": 200, "parsed": False, "body": "<form class=login_form>"}]
    with pytest.raises(ReachError) as exc:
        fr.facebook_react(url=POST_URL, reaction="like", timeout=30)
    assert exc.value.code == NEED_LOGIN


def test_unparseable_other_body_maps_to_upstream_broken(harness):
    harness["poll_results"] = [{"status": 200, "parsed": False, "body": "<html>maintenance</html>"}]
    with pytest.raises(ReachError) as exc:
        fr.facebook_react(url=POST_URL, reaction="like", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_missing_result_is_upstream_broken_with_duplicate_warning(harness):
    harness["poll_results"] = []  # _eval returns None until the deadline
    with pytest.raises(ReachError) as exc:
        fr.facebook_react(url=POST_URL, reaction="like", timeout=0.6)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "before retrying" in exc.value.message


def test_react_without_reaction_id_is_upstream_broken(harness):
    # GraphQL returned data but no viewer reaction → silent no-op must not pass.
    harness["poll_results"] = [ok_result(reactionId=None, reactionCount=0)]
    with pytest.raises(ReachError) as exc:
        fr.facebook_react(url=POST_URL, reaction="love", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "not applied" in exc.value.message


def test_react_with_mismatched_reaction_id_is_upstream_broken(harness):
    # FB answered with a DIFFERENT reaction than requested.
    harness["poll_results"] = [ok_result(reactionId="1635855486666999")]
    with pytest.raises(ReachError) as exc:
        fr.facebook_react(url=POST_URL, reaction="love", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "Unexpected reaction id" in exc.value.message


def test_unreact_with_reaction_still_set_is_upstream_broken(harness):
    harness["poll_results"] = [ok_result(reactionId="1678524932434102", reactionCount=1)]
    with pytest.raises(ReachError) as exc:
        fr.facebook_unreact(url=POST_URL, timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "did not take effect" in exc.value.message


def test_non_string_url_is_bad_input(harness):
    with pytest.raises(ReachError) as exc:
        fr.facebook_react(url=None, reaction="like", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_each_call_polls_its_own_result_key(harness):
    harness["poll_results"] = [ok_result(), ok_result()]
    fr.facebook_react(url=POST_URL, reaction="like", timeout=30)
    fr.facebook_unreact(url=POST_URL, timeout=30)
    assert len(harness["poll"]) == 2
    assert harness["poll"][0] != harness["poll"][1]
    for expr, key in zip(harness["send"], harness["poll"]):
        assert json.dumps(key) in expr


def test_channel_react_validates_type_before_browser_work(harness):
    from agent_reach.channels.facebook import FacebookChannel

    with pytest.raises(ReachError) as exc:
        FacebookChannel()._react(url=POST_URL, type="bogus", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_channel_react_missing_type_is_bad_input(harness):
    # --type is a CLI flag now (required=False); omitting it must still fail.
    from agent_reach.channels.facebook import FacebookChannel

    with pytest.raises(ReachError) as exc:
        FacebookChannel()._react(url=POST_URL, type=None, timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_channel_unreact_wires_through(harness):
    from agent_reach.channels.facebook import FacebookChannel

    harness["poll_results"] = [ok_result(reactionId=None, reactionCount=0)]
    items = FacebookChannel()._unreact(url=POST_URL, timeout=30)
    assert items[0]["url"] == POST_URL
    assert 'feedback_reaction_id="0"' in harness["send"][0]

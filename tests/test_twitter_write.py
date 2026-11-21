"""Unit tests for the twitter `post` write path — no real browser involved.

`twitter_post` is exercised end-to-end with `session_reuse`, `_x_target`,
`_wait_x_ready` and `_eval` stubbed; the fake `_eval` inspects each
expression and returns queued results for the send eval and the per-call
result poll (mirrors the facebook write harness, X-shaped).
"""

import json
import re

import pytest

from agent_reach.channels import _twitter_write as tw
from agent_reach.runtime import session_reuse
from agent_reach.runtime.result import (
    BAD_INPUT,
    NEED_LOGIN,
    PERSISTED_QUERY_DEAD,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
)

SEND_RE = re.compile(r"/i/api/graphql/.*CreateTweet")
POLL_RE = re.compile(r'^window\["(__x(?:Post|Reply|Quote)Result_[0-9a-f]{32})"\]\|\|null$')


@pytest.fixture
def harness(monkeypatch):
    """Stubbed browser; queues are consumed in order. An empty poll queue makes
    `_eval` return None forever (never a result)."""
    calls = {
        "evals": [],
        "send": [],
        "poll": [],
        "send_results": [{"sent": True}],
        "poll_results": [],
    }

    def fake_eval(target_id: str, expr: str, timeout: float):
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
        raise AssertionError(f"unexpected eval expression: {expr[:160]}")

    def fake_target(create: bool = True):
        return {"id": "T1", "type": "page", "url": "https://x.com/home"}

    monkeypatch.setattr(tw, "_eval", fake_eval)
    monkeypatch.setattr(tw, "_x_target", fake_target)
    monkeypatch.setattr(tw, "_wait_x_ready", lambda tid, timeout: None)
    monkeypatch.setattr(session_reuse, "prepare", lambda channel, force=False: {})
    return calls


def ok_result(**over):
    base = {
        "status": 200,
        "parsed": True,
        "errors": None,
        "tweetId": "1983000000000000001",
        "screenName": "reach_test",
        "tweetUrl": "https://x.com/reach_test/status/1983000000000000001",
        "echoText": "hello world",
    }
    base.update(over)
    return base


def test_post_success_returns_tweet_url(harness):
    harness["poll_results"] = [ok_result()]
    items = tw.twitter_post(text="hello world", timeout=30)
    assert items[0]["url"] == "https://x.com/reach_test/status/1983000000000000001"
    assert items[0]["title"] == "hello world"
    assert items[0]["raw"]["tweetId"] == "1983000000000000001"


def test_url_falls_back_to_web_status_when_screen_name_missing(harness):
    harness["poll_results"] = [ok_result(tweetUrl=None, screenName=None)]
    items = tw.twitter_post(text="x", timeout=30)
    assert items[0]["url"] == "https://x.com/i/web/status/1983000000000000001"


def test_post_without_any_tweet_reference_is_an_error_not_a_silent_success(harness):
    harness["poll_results"] = [ok_result(tweetUrl=None, tweetId=None)]
    with pytest.raises(ReachError) as exc:
        tw.twitter_post(text="x", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_confirmed_tweet_wins_over_nonfatal_errors(harness):
    harness["poll_results"] = [ok_result(errors=[{"message": "some warning"}])]
    items = tw.twitter_post(text="x", timeout=30)
    assert items[0]["raw"]["tweetId"] == "1983000000000000001"


def test_empty_text_is_bad_input_before_any_browser_work(harness):
    with pytest.raises(ReachError) as exc:
        tw.twitter_post(text="   ", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_overlong_text_is_bad_input_before_any_browser_work(harness):
    with pytest.raises(ReachError) as exc:
        tw.twitter_post(text="a" * 281, timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_no_tokens_without_uid_is_need_login_with_x_message(harness):
    harness["send_results"] = [{"error": "no_tokens", "hasUid": False}]
    with pytest.raises(ReachError) as exc:
        tw.twitter_post(text="x", timeout=30)
    assert exc.value.code == NEED_LOGIN
    assert "x.com" in exc.value.message
    assert "facebook.com" not in exc.value.message
    assert len(harness["send"]) == 1


def test_no_tokens_with_uid_retries_until_send(harness):
    harness["send_results"] = [
        {"error": "no_tokens", "hasUid": True},
        {"error": "no_tokens", "hasUid": True},
        {"sent": True},
    ]
    harness["poll_results"] = [ok_result()]
    items = tw.twitter_post(text="x", timeout=30)
    assert items and len(harness["send"]) == 3


def test_http_429_maps_to_rate_limited(harness):
    harness["poll_results"] = [{"status": 429, "parsed": False, "body": "rate limited"}]
    with pytest.raises(ReachError) as exc:
        tw.twitter_post(text="x", timeout=30)
    assert exc.value.code == RATE_LIMITED


def test_http_5xx_maps_to_upstream_broken(harness):
    harness["poll_results"] = [{"status": 503, "parsed": False, "body": "oops"}]
    with pytest.raises(ReachError) as exc:
        tw.twitter_post(text="x", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_http_404_maps_to_persisted_query_dead(harness):
    harness["poll_results"] = [{"status": 404, "parsed": False, "body": "not found"}]
    with pytest.raises(ReachError) as exc:
        tw.twitter_post(text="x", timeout=30)
    assert exc.value.code == PERSISTED_QUERY_DEAD


def test_graphql_errors_map_to_upstream_broken(harness):
    harness["poll_results"] = [
        ok_result(tweetUrl=None, tweetId=None, errors=[{"message": "over capacity"}])
    ]
    with pytest.raises(ReachError) as exc:
        tw.twitter_post(text="x", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "over capacity" in exc.value.message


def test_graphql_error_code_88_maps_to_rate_limited(harness):
    harness["poll_results"] = [
        ok_result(tweetUrl=None, tweetId=None, errors=[{"message": "Rate limit exceeded", "code": 88}])
    ]
    with pytest.raises(ReachError) as exc:
        tw.twitter_post(text="x", timeout=30)
    assert exc.value.code == RATE_LIMITED


def test_graphql_error_code_32_maps_to_need_login(harness):
    harness["poll_results"] = [
        ok_result(tweetUrl=None, tweetId=None, errors=[{"message": "Could not authenticate you", "code": 32}])
    ]
    with pytest.raises(ReachError) as exc:
        tw.twitter_post(text="x", timeout=30)
    assert exc.value.code == NEED_LOGIN


def test_unparseable_login_page_maps_to_need_login(harness):
    harness["poll_results"] = [{"status": 401, "parsed": False, "body": "unauthorized"}]
    with pytest.raises(ReachError) as exc:
        tw.twitter_post(text="x", timeout=30)
    assert exc.value.code == NEED_LOGIN


def test_unparseable_other_body_maps_to_upstream_broken(harness):
    harness["poll_results"] = [{"status": 200, "parsed": False, "body": "<html>maintenance</html>"}]
    with pytest.raises(ReachError) as exc:
        tw.twitter_post(text="x", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_query_id_resolves_through_query_ids_config(harness, monkeypatch):
    monkeypatch.setattr(tw.query_ids, "get", lambda ch, name, default="": "OVERRIDE_QID" if (ch, name) == ("x", "CreateTweet") else default)
    harness["poll_results"] = [ok_result()]
    tw.twitter_post(text="x", timeout=30)
    assert "/i/api/graphql/OVERRIDE_QID/CreateTweet" in harness["send"][0]
    assert '"queryId":"OVERRIDE_QID"' in harness["send"][0] or '"OVERRIDE_QID"' in harness["send"][0]


def test_user_text_containing_a_sentinel_is_not_resubstituted(harness):
    # "__X_QID"/"__X_KEY" inside the tweet text must not break substitution.
    text = 'posting literally __X_QID and "__X_KEY" verbatim'
    harness["poll_results"] = [ok_result()]
    tw.twitter_post(text=text, timeout=30)
    expr = harness["send"][0]
    assert json.dumps(text, ensure_ascii=False) in expr
    # The real KEY substitution still landed — the sentinel was replaced with
    # a per-call window key everywhere it appeared as a sentinel.
    assert re.search(r'const KEY="__xPostResult_[0-9a-f]{32}"', expr)


def test_each_call_polls_its_own_result_key(harness):
    harness["poll_results"] = [ok_result(), ok_result()]
    tw.twitter_post(text="one", timeout=30)
    tw.twitter_post(text="two", timeout=30)
    assert len(harness["poll"]) == 2
    assert harness["poll"][0] != harness["poll"][1]
    for expr, key in zip(harness["send"], harness["poll"]):
        assert json.dumps(key) in expr


def test_missing_result_is_upstream_broken_with_duplicate_warning(harness):
    harness["poll_results"] = []  # _eval returns None until the deadline
    with pytest.raises(ReachError) as exc:
        tw.twitter_post(text="x", timeout=0.6)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "duplicates" in exc.value.message


def test_text_is_substituted_last_and_unicode_safe(harness):
    text = "unicode 🐦 hành động"
    harness["poll_results"] = [ok_result()]
    tw.twitter_post(text=text, timeout=30)
    assert json.dumps(text, ensure_ascii=False) in harness["send"][0]


def test_is_x_url_parses_hostname_not_substring():
    assert tw._is_x_url("https://x.com/home")
    assert tw._is_x_url("https://mobile.twitter.com/u")
    assert not tw._is_x_url("https://notx.com.evil.example/")
    assert not tw._is_x_url("https://x.com.evil.example/")
    assert not tw._is_x_url("")
    assert not tw._is_x_url("about:blank")


def test_post_action_wires_through_channel():
    from agent_reach.channels.twitter import TwitterChannel
    from agent_reach.runtime.dispatch import find_action

    ch = TwitterChannel()
    action = find_action(ch, "post")
    assert action is not None
    assert action.backends == (("opencli", "_post"),)


# ── reply ─────────────────────────────────────────────────────────────

TWEET_URL = "https://x.com/someuser/status/1900111222333444555"


def test_reply_success_sends_in_reply_to_and_returns_new_tweet(harness):
    harness["poll_results"] = [ok_result()]
    items = tw.twitter_reply(url=TWEET_URL, text="nice take", timeout=30)
    expr = harness["send"][0]
    assert '"reply": {"in_reply_to_tweet_id": "1900111222333444555", "exclude_reply_user_ids": []}' in expr
    assert items[0]["url"] == "https://x.com/reach_test/status/1983000000000000001"
    assert items[0]["text"] == "Replied"
    assert items[0]["raw"]["inReplyTo"] == "1900111222333444555"
    assert re.search(r'const KEY="__xReplyResult_[0-9a-f]{32}"', expr)


def test_reply_accepts_twitter_com_url(harness):
    harness["poll_results"] = [ok_result()]
    tw.twitter_reply(url="https://twitter.com/u/status/1900111222333444555", text="x", timeout=30)
    assert '"in_reply_to_tweet_id": "1900111222333444555"' in harness["send"][0]


def test_reply_accepts_raw_numeric_id(harness):
    harness["poll_results"] = [ok_result()]
    tw.twitter_reply(url="1900111222333444555", text="x", timeout=30)
    assert '"in_reply_to_tweet_id": "1900111222333444555"' in harness["send"][0]


def test_reply_url_with_query_and_photo_suffix_still_finds_id(harness):
    harness["poll_results"] = [ok_result()]
    tw.twitter_reply(url="https://x.com/u/status/1900111222333444555/photo/1?s=20&t=abc", text="x", timeout=30)
    assert '"in_reply_to_tweet_id": "1900111222333444555"' in harness["send"][0]


def test_reply_non_x_url_is_bad_input_before_any_browser_work(harness):
    with pytest.raises(ReachError) as exc:
        tw.twitter_reply(url="https://example.com/nope", text="x", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_reply_x_url_without_status_id_is_bad_input_before_browser(harness):
    with pytest.raises(ReachError) as exc:
        tw.twitter_reply(url="https://x.com/home", text="x", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_reply_empty_text_is_bad_input_before_browser(harness):
    with pytest.raises(ReachError) as exc:
        tw.twitter_reply(url=TWEET_URL, text="  ", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_reply_graphql_errors_map_to_upstream_broken(harness):
    harness["poll_results"] = [
        ok_result(tweetUrl=None, tweetId=None, errors=[{"message": "Tweet not found"}])
    ]
    with pytest.raises(ReachError) as exc:
        tw.twitter_reply(url=TWEET_URL, text="x", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_reply_no_tokens_without_uid_is_need_login(harness):
    harness["send_results"] = [{"error": "no_tokens", "hasUid": False}]
    with pytest.raises(ReachError) as exc:
        tw.twitter_reply(url=TWEET_URL, text="x", timeout=30)
    assert exc.value.code == NEED_LOGIN
    assert "x.com" in exc.value.message


def test_reply_each_call_polls_its_own_result_key(harness):
    harness["poll_results"] = [ok_result(), ok_result()]
    tw.twitter_reply(url=TWEET_URL, text="one", timeout=30)
    tw.twitter_reply(url=TWEET_URL, text="two", timeout=30)
    assert harness["poll"][0] != harness["poll"][1]
    assert all(k.startswith("__xReplyResult_") for k in harness["poll"])


# ── quote ─────────────────────────────────────────────────────────────


def test_quote_success_sends_attachment_url_and_returns_new_tweet(harness):
    harness["poll_results"] = [ok_result()]
    items = tw.twitter_quote(url=TWEET_URL, text="this", timeout=30)
    expr = harness["send"][0]
    assert '"attachment_url": "https://x.com/someuser/status/1900111222333444555"' in expr
    assert items[0]["url"] == "https://x.com/reach_test/status/1983000000000000001"
    assert items[0]["text"] == "Quoted"
    assert items[0]["raw"]["attachmentUrl"] == "https://x.com/someuser/status/1900111222333444555"
    assert re.search(r'const KEY="__xQuoteResult_[0-9a-f]{32}"', expr)


def test_quote_canonicalizes_twitter_com_and_drops_tracking(harness):
    harness["poll_results"] = [ok_result()]
    tw.twitter_quote(url="https://twitter.com/u/status/1900111222333444555/photo/2?s=20", text="x", timeout=30)
    assert '"attachment_url": "https://x.com/u/status/1900111222333444555"' in harness["send"][0]


def test_quote_raw_numeric_id_uses_web_status_attachment(harness):
    harness["poll_results"] = [ok_result()]
    tw.twitter_quote(url="1900111222333444555", text="x", timeout=30)
    assert '"attachment_url": "https://x.com/i/web/status/1900111222333444555"' in harness["send"][0]


def test_quote_non_x_url_is_bad_input_before_any_browser_work(harness):
    with pytest.raises(ReachError) as exc:
        tw.twitter_quote(url="https://example.com/nope", text="x", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_quote_http_429_maps_to_rate_limited(harness):
    harness["poll_results"] = [{"status": 429, "parsed": False, "body": "rate limited"}]
    with pytest.raises(ReachError) as exc:
        tw.twitter_quote(url=TWEET_URL, text="x", timeout=30)
    assert exc.value.code == RATE_LIMITED


def test_reply_quote_actions_wire_through_channel():
    from agent_reach.channels.twitter import TwitterChannel
    from agent_reach.runtime.dispatch import find_action

    ch = TwitterChannel()
    for name, method in (("reply", "_reply"), ("quote", "_quote")):
        action = find_action(ch, name)
        assert action is not None
        assert action.backends == (("opencli", method),)

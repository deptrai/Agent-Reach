"""Unit tests for the facebook `post-edit` / `post-delete` write path — no real browser.

`facebook_post_edit` / `facebook_post_delete` are exercised end-to-end with
`session_reuse`, `_fb_target`, `_wait_ready`, `_ensure_post_loaded` and `_eval`
stubbed; the fake `_eval` inspects each expression and returns queued results
for the story-id locate, the audience locate, the session uid, the send eval
and the per-call result poll.
"""

import base64
import json
import re

import pytest

from agent_reach.channels import _facebook_post_edit as fpe
from agent_reach.runtime import session_reuse
from agent_reach.runtime.result import (
    BAD_INPUT,
    NEED_LOGIN,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
)

POST_URL = "https://www.facebook.com/61554949615037/posts/122311668092164987"
POST_ID = "122311668092164987"
UID = "61554949615037"
STORY_ID = "UzpfSTYxNTU0OTQ5NjE1MDM3OjEyMjMxMTY2ODA5MjE2NDk4NzoxMjIzMTE2NjgwOTIxNjQ5ODc"

SEND_EDIT_RE = re.compile(r"ComposerStoryEditMutation")
SEND_DELETE_RE = re.compile(r"useCometFeedStoryDeleteMutation")
POLL_RE = re.compile(r'^window\["(__fbPostResult_[0-9a-f]{32})"\]\|\|null$')
# story-id locate JS — match the literal regex source text inside it.
STORY_LOCATE_RE = re.compile(re.escape('"storyID":"([A-Za-z0-9+/=_-]{20,400})"'))
# privacy locate JS — the audience object lives in "privacy_row_input".
PRIVACY_RE = re.compile(re.escape('"privacy_row_input"'))
UID_RE = re.compile(r"USER_ID\|\|null")

PRIVACY_SELF = {"allow": [], "base_state": "SELF", "deny": [], "tag_expansion_state": "UNSPECIFIED"}
PRIVACY_FRIENDS_DENY = {"allow": [], "base_state": "FRIENDS", "deny": ["999888777"], "tag_expansion_state": "UNSPECIFIED"}


@pytest.fixture
def harness(monkeypatch):
    """Returns a calls dict; queues are consumed in order.

    - evals records every expr for assertions on the JS sent.
    - story_results / audience_results / poll_results queue fake eval outputs.
      "ALWAYS_NONE" keeps returning the empty/None shape; an empty poll queue
      makes `_eval` return None forever (no result).
    """
    calls = {
        "send": [],
        "story": [],
        "audience": [],
        "poll": [],
        "evals": [],
        "uid": UID,
        "story_uid": UID,
        "privacy": dict(PRIVACY_SELF),
        "story_results": [],
        "audience_results": [],
        "poll_results": [],
        "send_results": [{"sent": True}],
    }

    def fake_eval(target_id, expr, timeout):
        calls["evals"].append(expr)
        m = POLL_RE.match(expr)
        if m:
            calls["poll"].append(m.group(1))
            return calls["poll_results"].pop(0) if calls["poll_results"] else None
        if SEND_EDIT_RE.search(expr) or SEND_DELETE_RE.search(expr):
            calls["send"].append(expr)
            return calls["send_results"].pop(0) if calls["send_results"] else {"sent": True}
        if STORY_LOCATE_RE.search(expr):
            calls["story"].append(expr)
            if calls["story_results"]:
                item = calls["story_results"].pop(0)
                if item == "ALWAYS_NONE":
                    calls["story_results"].append(item)
                    return json.dumps({"href": POST_URL, "ids": [], "uid": None, "count": 0})
                return item
            return json.dumps(
                {"href": POST_URL, "ids": ["STORY-RESOLVED"], "uid": calls["story_uid"], "count": 1}
            )
        if PRIVACY_RE.search(expr):
            calls["audience"].append(expr)
            if calls["audience_results"]:
                item = calls["audience_results"].pop(0)
                if item == "ALWAYS_NONE":
                    calls["audience_results"].append(item)
                    return json.dumps({"href": POST_URL, "privacy": None, "count": 0})
                return item
            return json.dumps({"href": POST_URL, "privacy": calls["privacy"], "count": 1})
        if UID_RE.search(expr):
            return calls["uid"]
        return True

    monkeypatch.setattr(fpe, "_eval", fake_eval)
    monkeypatch.setattr(fpe, "_fb_target", lambda create: {"id": "T1", "type": "page", "url": "https://www.facebook.com/"})
    monkeypatch.setattr(fpe, "_wait_ready", lambda tid, timeout: None)
    monkeypatch.setattr(fpe, "_ensure_post_loaded", lambda tid, url, timeout: None)
    monkeypatch.setattr(session_reuse, "prepare", lambda channel, *, force=False: None)
    return calls


def ok_edit(**over):
    base = {
        "status": 200,
        "parsed": True,
        "errors": None,
        "dataKeys": ["story_edit"],
        "storyId": "STORY-RESOLVED",
        "body": '{"data":{"story_edit":{"story":{"id":"STORY-RESOLVED"}}},"text":"edited post body"}',
    }
    base.update(over)
    return base


def ok_delete(**over):
    base = {
        "status": 200,
        "parsed": True,
        "errors": None,
        "dataKeys": ["story_delete"],
        "deletedStoryId": "STORY-RESOLVED",
        "body": '{"data":{"story_delete":{"deleted_story_id":"STORY-RESOLVED"}}}',
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- post target


def test_post_target_permalink_returns_post_id():
    permalink, post_id = fpe._post_target(POST_URL)
    assert permalink == POST_URL
    assert post_id == POST_ID


def test_post_target_permalink_php_story_fbid():
    url = f"https://www.facebook.com/permalink.php?story_fbid={POST_ID}&id={UID}"
    _, post_id = fpe._post_target(url)
    assert post_id == POST_ID


def test_post_target_raw_story_id_builds_permalink():
    permalink, post_id = fpe._post_target(STORY_ID)
    assert post_id == POST_ID
    assert permalink == f"https://www.facebook.com/{UID}/posts/{POST_ID}"


def test_post_target_profile_url_is_bad_input():
    with pytest.raises(ReachError) as exc:
        fpe._post_target(f"https://www.facebook.com/{UID}")
    assert exc.value.code == BAD_INPUT


def test_post_target_non_facebook_is_bad_input():
    with pytest.raises(ReachError) as exc:
        fpe._post_target("https://example.com/posts/123")
    assert exc.value.code == BAD_INPUT


def test_raw_story_id_accepts_base64url():
    raw = base64.urlsafe_b64encode(b"S:_I61554949615037:122311720316164987:122311720316164987").decode().rstrip("=")
    assert fpe._raw_story_id(raw) == raw
    # A permalink is never mistaken for a raw story id.
    assert fpe._raw_story_id(POST_URL) is None
    # Random base64 that does not decode to a story shape is rejected.
    junk = base64.urlsafe_b64encode(b"not-a-story").decode().rstrip("=")
    assert fpe._raw_story_id(junk) is None


# ---------------------------------------------------------------- edit


def test_edit_success_returns_item(harness):
    harness["poll_results"] = [ok_edit()]
    items = fpe.facebook_post_edit(url=POST_URL, text="edited post body", timeout=30)
    assert len(items) == 1
    item = items[0]
    assert item["url"] == POST_URL
    assert item["text"] == "Edited"
    assert item["raw"]["storyId"] == "STORY-RESOLVED"
    assert item["raw"]["audience"] == "SELF"
    assert item["raw"]["privacy"] == PRIVACY_SELF
    expr = harness["send"][0]
    assert "ComposerStoryEditMutation" in expr
    assert "30241673288768290" in expr
    assert '"STORY-RESOLVED"' in expr
    assert '"edited post body"' in expr
    # full privacy object substituted wholesale (not just base_state)
    assert '"base_state": "SELF"' in expr
    assert '"tag_expansion_state": "UNSPECIFIED"' in expr
    # story id was resolved live from the page (locate ran before the send).
    assert len(harness["story"]) == 1
    assert len(harness["audience"]) == 1


def test_edit_text_with_quotes_and_unicode_substituted(harness):
    harness["poll_results"] = [ok_edit(body='{"data":{"story_edit":{}},"text":"say \\"hi\\" 👋"}')]
    fpe.facebook_post_edit(url=POST_URL, text='say "hi" 👋', timeout=30)
    # json.dumps escaping keeps the JS string literal valid (emoji -> \uXXXX).
    assert 'say \\"hi\\" \\ud83d\\udc4b' in harness["send"][0]


def test_edit_empty_text_is_bad_input_before_browser(harness):
    for bad in ("", "   ", None):
        with pytest.raises(ReachError) as exc:
            fpe.facebook_post_edit(url=POST_URL, text=bad, timeout=30)
        assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_edit_bad_url_is_bad_input_before_browser(harness):
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url="https://example.com/x", text="hi", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_edit_post_id_substituted_into_locate(harness):
    harness["poll_results"] = [ok_edit()]
    fpe.facebook_post_edit(url=POST_URL, text="edited post body", timeout=30)
    assert "__FB_PID" not in harness["story"][0]
    assert json.dumps(POST_ID) in harness["story"][0]


def test_edit_missing_story_edit_payload_is_upstream_broken(harness):
    harness["poll_results"] = [ok_edit(dataKeys=["feed_story_edit"], body='{"data":{"feed_story_edit":{}}}')]
    # "feed_story_edit" does not start with "story_edit" -> no confirmation.
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="edited post body", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "no confirmation" in exc.value.message


def test_edit_text_not_echoed_is_upstream_broken(harness):
    harness["poll_results"] = [ok_edit(body='{"data":{"story_edit":{"story":{"id":"X"}}},"text":"other"}')]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="edited post body", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "no confirmation" in exc.value.message


def test_edit_no_tokens_without_uid_is_need_login(harness):
    harness["send_results"] = [{"error": "no_tokens", "hasUid": False}]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == NEED_LOGIN
    assert len(harness["send"]) == 1


def test_edit_no_tokens_with_uid_retries_until_send(harness):
    harness["send_results"] = [
        {"error": "no_tokens", "hasUid": True},
        {"error": "no_tokens", "hasUid": True},
        {"sent": True},
    ]
    harness["poll_results"] = [ok_edit()]
    items = fpe.facebook_post_edit(url=POST_URL, text="edited post body", timeout=30)
    assert items and len(harness["send"]) == 3


def test_edit_http_429_maps_to_rate_limited(harness):
    harness["poll_results"] = [{"status": 429, "parsed": False, "body": "rate limited"}]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == RATE_LIMITED


def test_edit_http_5xx_maps_to_upstream_broken(harness):
    harness["poll_results"] = [{"status": 503, "parsed": False, "body": "oops"}]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_edit_graphql_errors_map_to_upstream_broken(harness):
    harness["poll_results"] = [ok_edit(errors=[{"message": "missing_required_variable_value"}])]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "missing_required_variable_value" in exc.value.message


def test_edit_login_shaped_graphql_error_maps_to_need_login(harness):
    harness["poll_results"] = [ok_edit(errors=[{"message": "Login required to continue"}])]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == NEED_LOGIN


def test_edit_unparseable_login_page_maps_to_need_login(harness):
    harness["poll_results"] = [{"status": 200, "parsed": False, "body": "<form class=login_form>"}]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == NEED_LOGIN


def test_edit_unparseable_other_body_maps_to_upstream_broken(harness):
    harness["poll_results"] = [{"status": 200, "parsed": False, "body": "<html>maintenance</html>"}]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_edit_missing_result_is_upstream_broken(harness):
    harness["poll_results"] = []
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=0.6)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "before retrying" in exc.value.message


def test_edit_in_page_error_maps_to_upstream_broken(harness):
    harness["poll_results"] = [{"error": "boom"}]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


# ---------------------------------------------------------------- ownership / resolution


def test_edit_story_not_yours_is_upstream_broken(harness):
    harness["story_uid"] = "999999999999999"
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "not yours" in exc.value.message
    assert harness["send"] == []  # never fire the mutation on someone else's post


def test_edit_unverifiable_owner_is_upstream_broken(harness):
    harness["story_uid"] = None
    # story_uid None -> uid key is None -> _check_ownership fails closed.
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert harness["send"] == []


def test_edit_missing_session_uid_is_upstream_broken(harness):
    harness["uid"] = None
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert harness["send"] == []


def test_edit_story_never_resolved_is_upstream_broken(harness):
    harness["story_results"] = ["ALWAYS_NONE"]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=1.5)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "Could not resolve" in exc.value.message


def test_edit_ambiguous_story_locate_is_upstream_broken(harness):
    harness["story_results"] = [
        json.dumps({"href": POST_URL, "ids": ["A", "B"], "uid": UID, "count": 2})
    ]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "Ambiguous" in exc.value.message
    assert harness["send"] == []


def test_edit_audience_never_resolved_is_upstream_broken(harness):
    harness["audience_results"] = ["ALWAYS_NONE"]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="hi", timeout=1.5)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "audience" in exc.value.message.lower()
    assert harness["send"] == []


# ---------------------------------------------------------------- delete


def test_delete_requires_confirm_before_browser(harness):
    for bad in (False, None, "false", "no", 0):
        with pytest.raises(ReachError) as exc:
            fpe.facebook_post_delete(url=POST_URL, confirm=bad, timeout=30)
        assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []
    assert harness["send"] == []


def test_delete_success_returns_removed_item(harness):
    harness["poll_results"] = [ok_delete()]
    items = fpe.facebook_post_delete(url=POST_URL, confirm=True, timeout=30)
    assert len(items) == 1
    item = items[0]
    assert item["raw"]["removed"] is True
    assert item["raw"]["storyId"] == "STORY-RESOLVED"
    assert item["raw"]["deletedStoryId"] == "STORY-RESOLVED"
    expr = harness["send"][0]
    assert "useCometFeedStoryDeleteMutation" in expr
    assert "29557927263819997" in expr
    assert '"STORY-RESOLVED"' in expr


def test_delete_raw_story_id_skips_permalink_input(harness):
    harness["poll_results"] = [ok_delete()]
    items = fpe.facebook_post_delete(url=STORY_ID, confirm=True, timeout=30)
    assert items[0]["url"] == f"https://www.facebook.com/{UID}/posts/{POST_ID}"
    assert '"STORY-RESOLVED"' in harness["send"][0]


def test_delete_null_story_delete_payload_is_upstream_broken(harness):
    # {"data":{"story_delete":null}} is a silent no-op — must fail closed.
    harness["poll_results"] = [
        ok_delete(deletedStoryId=None, body='{"data":{"story_delete":null}}')
    ]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_delete(url=POST_URL, confirm=True, timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "no confirmation" in exc.value.message


def test_delete_empty_data_is_upstream_broken(harness):
    harness["poll_results"] = [ok_delete(dataKeys=[], deletedStoryId=None, body='{"data":{}}')]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_delete(url=POST_URL, confirm=True, timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_delete_story_id_mismatch_is_upstream_broken(harness):
    harness["poll_results"] = [
        ok_delete(deletedStoryId="OTHER-STORY", body='{"data":{"story_delete":{"deleted_story_id":"OTHER-STORY"}}}')
    ]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_delete(url=POST_URL, confirm=True, timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "no confirmation" in exc.value.message


def test_delete_echo_padding_insensitive(harness):
    # Response echoes the id with different '=' padding — still a match.
    harness["poll_results"] = [
        ok_delete(body='{"data":{"story_delete":{"deleted_story_id":"STORY-RESOLVED=="}}}')
    ]
    items = fpe.facebook_post_delete(url=POST_URL, confirm=True, timeout=30)
    assert items[0]["raw"]["removed"] is True


def test_delete_story_not_yours_is_upstream_broken(harness):
    harness["story_uid"] = "999999999999999"
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_delete(url=POST_URL, confirm=True, timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "not yours" in exc.value.message
    assert harness["send"] == []


def test_delete_http_429_maps_to_rate_limited(harness):
    harness["poll_results"] = [{"status": 429, "parsed": False, "body": "rate limited"}]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_delete(url=POST_URL, confirm=True, timeout=30)
    assert exc.value.code == RATE_LIMITED


def test_delete_missing_result_is_upstream_broken(harness):
    harness["poll_results"] = []
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_delete(url=POST_URL, confirm=True, timeout=0.6)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "before retrying" in exc.value.message


# ---------------------------------------------------------------- plumbing


def test_each_call_polls_its_own_result_key(harness):
    harness["poll_results"] = [ok_edit(), ok_delete()]
    fpe.facebook_post_edit(url=POST_URL, text="edited post body", timeout=30)
    fpe.facebook_post_delete(url=POST_URL, confirm=True, timeout=30)
    assert len(harness["poll"]) == 2
    assert harness["poll"][0] != harness["poll"][1]
    for expr, key in zip(harness["send"], harness["poll"]):
        assert json.dumps(key) in expr


def test_no_leftover_placeholders_in_send_expr(harness):
    harness["poll_results"] = [ok_edit()]
    fpe.facebook_post_edit(url=POST_URL, text="edited post body", timeout=30)
    expr = harness["send"][0]
    assert "__FB_" not in expr
    assert 'p.set("variables",JSON.stringify(vars))' in expr
    assert "story_id" in expr


def test_channel_post_edit_wires_through(harness):
    from agent_reach.channels.facebook import FacebookChannel

    harness["poll_results"] = [ok_edit()]
    items = FacebookChannel()._post_edit(url=POST_URL, text="edited post body", timeout=30)
    assert items[0]["raw"]["storyId"] == "STORY-RESOLVED"


def test_channel_post_delete_requires_confirm(harness):
    from agent_reach.channels.facebook import FacebookChannel

    with pytest.raises(ReachError) as exc:
        FacebookChannel()._post_delete(url=POST_URL, confirm=False, timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_channel_post_delete_wires_through(harness):
    from agent_reach.channels.facebook import FacebookChannel

    harness["poll_results"] = [ok_delete()]
    items = FacebookChannel()._post_delete(url=POST_URL, confirm=True, timeout=30)
    assert items[0]["raw"]["removed"] is True


# ---------------------------------------------------------------- bool flag plumbing


def _bool_action():
    from agent_reach.channels.base import Action, Param

    return Action(
        "post-delete",
        "delete",
        (
            Param("url", "u"),
            Param("confirm", "c", required=False, type=bool, default=False),
        ),
        (("opencli", "_post_delete"),),
    )


def test_validate_bool_param_accepts_flag_and_strings():
    from agent_reach.runtime.dispatch import _validate

    action = _bool_action()
    assert _validate(action, {"url": "x"})["confirm"] is False
    assert _validate(action, {"url": "x", "confirm": True})["confirm"] is True
    assert _validate(action, {"url": "x", "confirm": "true"})["confirm"] is True
    assert _validate(action, {"url": "x", "confirm": "false"})["confirm"] is False
    assert _validate(action, {"url": "x", "confirm": "yes"})["confirm"] is True
    assert _validate(action, {"url": "x", "confirm": "0"})["confirm"] is False
    with pytest.raises(ReachError) as exc:
        _validate(action, {"url": "x", "confirm": "maybe"})
    assert exc.value.code == BAD_INPUT


def test_parse_action_args_bool_flag():
    from agent_reach.runtime.cli import _parse_action_args

    action = _bool_action()
    ns = _parse_action_args(action, ["https://x", "--confirm"])
    assert ns["confirm"] is True
    ns2 = _parse_action_args(action, ["https://x"])
    assert ns2["confirm"] is None  # store_true default=None -> _validate maps to default False


def test_usage_renders_bool_param_as_flag():
    from agent_reach.runtime.cli import _usage
    from agent_reach.channels.facebook import FacebookChannel

    action = next(a for a in FacebookChannel.actions if a.name == "post-delete")
    usage = _usage(FacebookChannel(), action)
    assert "[--confirm]" in usage
    assert "CONFIRM" not in usage


# ---------------------------------------------------------------- JS sanity


def _balanced(expr: str) -> bool:
    """Cheap brace/paren/bracket balance check ignoring strings/comments."""
    stack = []
    pairs = {"}": "{", "]": "[", ")": "("}
    in_str = None
    esc = False
    i = 0
    while i < len(expr):
        c = expr[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == in_str:
                in_str = None
        elif c in "\"'`":
            in_str = c
        elif c in "([{":
            stack.append(c)
        elif c in ")]}":
            if not stack or stack.pop() != pairs[c]:
                return False
        i += 1
    return not stack and in_str is None


def test_rendered_send_exprs_are_balanced_and_substituted():
    for kind in ("edit", "delete"):
        expr = (
            fpe._JS[kind]
            .replace("__FB_PARAMS", json.dumps(fpe._PARAMS[kind]))
            .replace("__FB_VARS", json.dumps(fpe._VARS[kind], ensure_ascii=False))
            .replace("__FB_FRIENDLY", json.dumps(fpe._DOC[kind][0]))
            .replace("__FB_DOC", json.dumps(fpe._DOC[kind][1]))
            .replace("__FB_KEY", json.dumps("K"))
        )
        # Single-pass substitution mirrors _send_mutation: every sentinel is
        # replaced by ONE re.sub scan, so a value containing another
        # placeholder is never re-scanned.
        fields = {"__FB_STORY": "S", "__FB_TEXT": "t", "__FB_PRIVACY": PRIVACY_SELF}
        pattern = re.compile("|".join(re.escape(p) for p in fields))
        expr = pattern.sub(lambda m: json.dumps(fields[m.group(0)]), expr)
        assert "__FB_" not in expr, kind
        assert _balanced(expr), kind


def test_locate_exprs_are_balanced_and_substituted():
    locate = fpe._STORY_LOCATE_JS.replace("__FB_PID", json.dumps(POST_ID))
    assert _balanced(locate)
    assert "__FB_PID" not in locate
    privacy = fpe._PRIVACY_LOCATE_JS.replace("__FB_PID", json.dumps(POST_ID))
    assert _balanced(privacy)
    assert "__FB_PID" not in privacy
    assert _balanced(fpe._UID_JS)


# ------------------------------------------------------------- review fixes


def test_permalinks_resolve_to_same_post(harness):
    """permalink.php?story_fbid=N is what `facebook post` returns; Facebook
    302s it to /<uid>/posts/N — `_same_post_path` must accept the canonical
    href instead of looping to UPSTREAM_BROKEN."""
    from agent_reach.channels import _facebook_react as fr

    permalink_php = "https://www.facebook.com/permalink.php?story_fbid=122311668092164987&id=61554949615037"
    canonical = "https://www.facebook.com/61554949615037/posts/122311668092164987"
    # tab already on canonical, target is permalink.php form -> same post
    assert fr._same_post_path(canonical, permalink_php)
    assert fr._same_post_path(permalink_php, canonical)
    # different post -> still navigates
    other = "https://www.facebook.com/permalink.php?story_fbid=999999999999999999&id=61554949615037"
    assert not fr._same_post_path(canonical, other)
    # comment_id must still match
    assert not fr._same_post_path(
        canonical + "?comment_id=1", permalink_php + "&comment_id=2"
    )
    assert fr._same_post_path(
        canonical + "?comment_id=7", permalink_php + "&comment_id=7"
    )


def test_delete_confirmed_despite_partial_graphql_errors(harness):
    """FB can attach non-fatal errors to a payload that still confirms the
    delete — the positive signal must win over the errors array."""
    harness["poll_results"] = [
        ok_delete(errors=[{"message": "partial non-fatal"}], deletedStoryId="STORY-RESOLVED")
    ]
    items = fpe.facebook_post_delete(url=POST_URL, confirm=True, timeout=30)
    assert items[0]["raw"]["removed"] is True


def test_edit_confirmed_despite_partial_graphql_errors(harness):
    harness["poll_results"] = [
        ok_edit(errors=[{"message": "partial non-fatal"}], textEchoed=True)
    ]
    items = fpe.facebook_post_edit(url=POST_URL, text="t", timeout=30)
    assert items[0]["text"] == "Edited"


def test_edit_text_containing_placeholder_literal_is_not_corrupted(harness):
    """User text may literally contain `__FB_PRIVACY` — single-pass re.sub
    inserts it as data and never re-scans it for placeholders."""
    harness["poll_results"] = [ok_edit(textEchoed=True)]
    fpe.facebook_post_edit(url=POST_URL, text="see __FB_PRIVACY marker", timeout=30)
    expr = harness["send"][0]
    # the placeholder occurrence inside the quoted text survives verbatim;
    # the real substitution site (privacy=...) was replaced.
    assert expr.count("__FB_PRIVACY") == 1
    assert "vars.input.audience.privacy={" in expr
    assert _balanced(expr)


def test_edit_echo_beyond_body_slice_still_confirms(harness):
    """out['body'] is sliced to 4000 chars; when the echo lands past the
    slice the JS-side textEchoed flag (computed on the FULL body) decides."""
    big = "x" * 5000
    harness["poll_results"] = [ok_edit(body=big, textEchoed=True)]
    items = fpe.facebook_post_edit(url=POST_URL, text="edited post body", timeout=30)
    assert items[0]["text"] == "Edited"


def test_edit_text_echoed_false_is_upstream_broken(harness):
    harness["poll_results"] = [ok_edit(textEchoed=False)]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="edited post body", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_edit_preserves_full_privacy_object(harness):
    """'Friends except X' must not silently broaden to FRIENDS — the whole
    privacy object (allow/base_state/deny/tag_expansion_state) is sent."""
    harness["privacy"] = dict(PRIVACY_FRIENDS_DENY)
    harness["poll_results"] = [ok_edit(textEchoed=True)]
    items = fpe.facebook_post_edit(url=POST_URL, text="t", timeout=30)
    expr = harness["send"][0]
    assert '"deny": ["999888777"]' in expr
    assert '"base_state": "FRIENDS"' in expr
    assert items[0]["raw"]["privacy"]["deny"] == ["999888777"]


def test_privacy_on_wrong_post_keeps_polling(harness):
    """A privacy node correlated to the post id but rendered while the page
    shows a DIFFERENT post is not trusted — keep polling, then fail closed."""
    harness["audience_results"] = [
        json.dumps({
            "href": "https://www.facebook.com/61554949615037/posts/999999999999999999",
            "privacy": PRIVACY_SELF,
            "count": 1,
        }),
        "ALWAYS_NONE",
    ]
    with pytest.raises(ReachError) as exc:
        fpe.facebook_post_edit(url=POST_URL, text="t", timeout=1.5)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "audience" in exc.value.message


def test_unicode_digits_in_post_id_are_bad_input():
    """Arabic-Indic digits must not be accepted as a post id."""
    with pytest.raises(ReachError) as exc:
        fpe._post_target("https://www.facebook.com/61554949615037/posts/١٢٣٤٥٦٧٨٩٠")
    assert exc.value.code == BAD_INPUT
    with pytest.raises(ReachError) as exc2:
        fpe._post_target("https://www.facebook.com/permalink.php?story_fbid=١٢٣&id=61554949615037")
    assert exc2.value.code == BAD_INPUT


def test_sent_sentinel_guards_all_write_send_exprs():
    """Every write module's send JS must set KEY+'_sent' BEFORE fetch so a
    retried eval after a transport timeout cannot re-fire the mutation."""
    from agent_reach.channels import _facebook_comment as fc
    from agent_reach.channels import _facebook_react as fr
    from agent_reach.channels import _facebook_write as fw

    templates = [
        ("post_edit.edit", fpe._JS["edit"]),
        ("post_edit.delete", fpe._JS["delete"]),
        ("write", fw._POST_JS),
        ("comment.create", fc._CREATE_JS),
        ("comment.delete", fc._DELETE_JS),
        ("react", fr._SEND_JS),
    ]
    for name, expr in templates:
        assert 'window[KEY+"_sent"]' in expr, name
        assert expr.index('window[KEY+"_sent"]') < expr.index("fetch("), name


def test_edit_text_echo_check_is_unicode_aware():
    """Facebook JSON-escapes non-ASCII response text as \\uXXXX. The edit
    confirmation must try BOTH the raw needle and its \\u-escaped form —
    otherwise every Vietnamese/emoji edit reports a false-negative
    UPSTREAM_BROKEN even though the mutation landed (found via live e2e)."""
    edit_js = fpe._JS["edit"]
    assert "textEchoed" in edit_js
    # The escaped-form needle builder must exist right at the echo check.
    assert 'toString(16).padStart(4,"0")' in edit_js
    assert "_txtEsc" in edit_js

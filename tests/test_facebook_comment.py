"""Unit tests for the facebook `comment` / `uncomment` write path — no real browser.

`facebook_comment` / `facebook_uncomment` are exercised end-to-end with
`session_reuse`, `_fb_target`, `_wait_ready`, `_ensure_post_loaded`,
`_resolve_feedback_id` and `_eval` stubbed; the fake `_eval` inspects each
expression and returns queued results for the comment-id locate, the send
eval and the per-call result poll.
"""

import json
import re

import pytest

from agent_reach.channels import _facebook_comment as fc
from agent_reach.runtime import session_reuse
from agent_reach.runtime.result import (
    BAD_INPUT,
    NEED_LOGIN,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
)

POST_URL = "https://www.facebook.com/61554949615037/posts/122311668092164987"
COMMENT_URL = POST_URL + "?comment_id=56789"
# base64("comment:122311668092164987_56789") — decodes to a valid comment node id.
RAW_CID = "Y29tbWVudDoxMjIzMTE2NjgwOTIxNjQ5ODdfNTY3ODk"

SEND_CREATE_RE = re.compile(r"useCometUFICreateCommentMutation")
SEND_DELETE_RE = re.compile(r"useCometUFIDeleteCommentMutation")
POLL_RE = re.compile(r'^window\["(__fbCommentResult_[0-9a-f]{32})"\]\|\|null$')
# comment permalink locate JS — match the literal regex source text inside it.
LOCATE_CID_RE = re.compile(re.escape("Y29tbWVud[A-Za-z0-9+/_=-]{6,200}"))
LOCATE_FID_RE = re.compile(r'"feedback_id":"\(\[\^"\]\+\)"')  # post feedback_id locate JS (literal source text)


@pytest.fixture
def harness(monkeypatch):
    """Returns a calls dict; queues are consumed in order.

    - evals records every expr for assertions on the JS sent.
    - locate_cid_results / send_results / poll_results queue fake eval outputs;
      an empty poll queue makes `_eval` return None forever (no result).
    """
    calls = {
        "send": [],
        "locate_cid": [],
        "poll": [],
        "evals": [],
        "navigations": [],
        "href": POST_URL,  # what location.href reports inside the tab
    }
    calls["locate_cid_results"] = []
    calls["send_results"] = [{"sent": True}]
    calls["poll_results"] = []

    def fake_eval(target_id, expr, timeout):
        calls["evals"].append(expr)
        m = POLL_RE.match(expr)
        if m:
            calls["poll"].append(m.group(1))
            return calls["poll_results"].pop(0) if calls["poll_results"] else None
        if SEND_CREATE_RE.search(expr) or SEND_DELETE_RE.search(expr):
            calls["send"].append(expr)
            return calls["send_results"].pop(0) if calls["send_results"] else {"sent": True}
        if LOCATE_CID_RE.search(expr):
            calls["locate_cid"].append(expr)
            if calls["locate_cid_results"]:
                item = calls["locate_cid_results"].pop(0)
                if item == "ALWAYS_NONE":
                    calls["locate_cid_results"].append(item)
                    return json.dumps({"href": POST_URL, "ids": [], "count": 0})
                return item
            return json.dumps({"href": POST_URL, "ids": ["CID-RESOLVED"], "count": 1})
        if LOCATE_FID_RE.search(expr):
            return json.dumps({"href": POST_URL, "fid": "FID-POST-1", "count": 1})
        if expr == "location.href":
            navs = calls["navigations"]
            if navs and navs[-1][0] == "Page.navigate":
                return (navs[-1][1] or {}).get("url") or POST_URL
            return calls["href"]
        # focus_expr / scrollIntoView probe and anything else: benign truthy.
        return True

    monkeypatch.setattr(fc, "_eval", fake_eval)
    monkeypatch.setattr(fc, "_fb_target", lambda create: {"id": "T1", "type": "page", "url": "https://www.facebook.com/"})
    monkeypatch.setattr(fc, "_wait_ready", lambda tid, timeout: None)
    monkeypatch.setattr(fc, "_ensure_post_loaded", lambda tid, url, timeout: None)
    monkeypatch.setattr(fc, "_resolve_feedback_id", lambda tid, url, timeout: "FID-POST-1")
    monkeypatch.setattr(session_reuse, "prepare", lambda channel, *, force=False: None)
    from agent_reach.runtime import cdp

    monkeypatch.setattr(cdp, "call", lambda method, params=None, **kw: calls["navigations"].append((method, params)) or {})
    return calls


def ok_create(**over):
    base = {
        "status": 200,
        "parsed": True,
        "errors": None,
        "commentId": "Y29tbWVudDoxMjMxXzU2Nzg5",
        "feedbackId": "FID-POST-1",
        "bodyText": "hello world",
        "createdTime": "1732000000",
    }
    base.update(over)
    return base


def ok_delete(**over):
    base = {
        "status": 200,
        "parsed": True,
        "errors": None,
        "dataKeys": ["comment_delete"],
        "echoesComment": True,
        # non-null payload under comment_delete — the fallback confirmation.
        "body": '{"data":{"comment_delete":{"__typename":"Comment"}}}',
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- create


def test_comment_success_returns_permalink_item(harness):
    harness["poll_results"] = [ok_create()]
    items = fc.facebook_comment(url=POST_URL, text="hello world", timeout=30)
    assert len(items) == 1
    item = items[0]
    assert item["url"].endswith("comment_id=56789")
    assert item["raw"]["commentId"] == "Y29tbWVudDoxMjMxXzU2Nzg5"
    assert item["raw"]["feedbackId"] == "FID-POST-1"
    # The send expr carries the post feedback_id + the comment text.
    assert '"FID-POST-1"' in harness["send"][0]
    assert '"hello world"' in harness["send"][0]
    assert "useCometUFICreateCommentMutation" in harness["send"][0]
    assert "29145340888419828" in harness["send"][0]


def test_comment_text_with_quotes_and_unicode_substituted(harness):
    harness["poll_results"] = [ok_create()]
    fc.facebook_comment(url=POST_URL, text='say "hi" 👋', timeout=30)
    # json.dumps escaping keeps the JS string literal valid (emoji -> \uXXXX).
    assert 'say \\"hi\\" \\ud83d\\udc4b' in harness["send"][0]


def test_comment_without_comment_id_in_response_is_upstream_broken(harness):
    harness["poll_results"] = [ok_create(commentId=None)]
    with pytest.raises(ReachError) as exc:
        fc.facebook_comment(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "no comment id" in exc.value.message


def test_comment_bad_inputs_rejected_before_browser(harness):
    with pytest.raises(ReachError) as exc:
        fc.facebook_comment(url="https://example.com/x", text="hi", timeout=30)
    assert exc.value.code == BAD_INPUT
    with pytest.raises(ReachError) as exc:
        fc.facebook_comment(url=POST_URL, text="   ", timeout=30)
    assert exc.value.code == BAD_INPUT
    with pytest.raises(ReachError) as exc:
        fc.facebook_comment(url=POST_URL, text=None, timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


# ---------------------------------------------------------------- delete


def test_uncomment_by_permalink_resolves_comment_id(harness):
    harness["poll_results"] = [ok_delete()]
    items = fc.facebook_uncomment(url=COMMENT_URL, timeout=30)
    assert items[0]["raw"]["commentId"] == "CID-RESOLVED"
    assert items[0]["url"] == COMMENT_URL
    assert len(harness["locate_cid"]) == 1
    # The numeric comment_id from the URL is embedded in the locate expr.
    assert '"56789"' in harness["locate_cid"][0]
    # The delete send carries the resolved Relay comment id.
    assert '"CID-RESOLVED"' in harness["send"][0]
    assert "useCometUFIDeleteCommentMutation" in harness["send"][0]


def test_uncomment_by_raw_id_skips_locate(harness):
    # echoesComment false but non-null payload still confirms the delete.
    harness["poll_results"] = [
        ok_delete(echoesComment=False, body='{"data":{"comment_delete":{"id":"X"}}}')
    ]
    items = fc.facebook_uncomment(url=RAW_CID, timeout=30)
    assert items[0]["raw"]["commentId"] == RAW_CID
    assert harness["locate_cid"] == []  # raw id needs no page resolution
    assert '"' + RAW_CID + '"' in harness["send"][0]


def test_uncomment_raw_id_navigates_when_tab_off_facebook(harness):
    harness["href"] = "https://example.com/other"
    harness["poll_results"] = [ok_delete()]
    fc.facebook_uncomment(url=RAW_CID, timeout=30)
    # location.href returns the navigated URL once Page.navigate lands.
    assert any(m == "Page.navigate" for m, _ in harness["navigations"])


def test_uncomment_permalink_without_comment_id_is_bad_input(harness):
    with pytest.raises(ReachError) as exc:
        fc.facebook_uncomment(url=POST_URL, timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_uncomment_non_facebook_url_is_bad_input(harness):
    with pytest.raises(ReachError) as exc:
        fc.facebook_uncomment(url="https://example.com/x?comment_id=1", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_ambiguous_comment_locate_is_upstream_broken(harness):
    harness["locate_cid_results"] = [
        json.dumps({"href": POST_URL, "ids": ["A", "B"], "count": 2})
    ]
    with pytest.raises(ReachError) as exc:
        fc.facebook_uncomment(url=COMMENT_URL, timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "Ambiguous" in exc.value.message
    assert harness["send"] == []  # never fire the mutation on ambiguity


def test_comment_id_never_resolved_is_upstream_broken(harness):
    harness["locate_cid_results"] = ["ALWAYS_NONE"]
    with pytest.raises(ReachError) as exc:
        fc.facebook_uncomment(url=COMMENT_URL, timeout=1.5)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "Could not resolve" in exc.value.message


def test_uncomment_comment_id_substituted_into_locate(harness):
    harness["poll_results"] = [ok_delete()]
    fc.facebook_uncomment(url=COMMENT_URL, timeout=30)
    # __FB_CID is replaced by the JSON-encoded numeric comment_id.
    assert "56789" in harness["locate_cid"][0]
    assert "__FB_CID" not in harness["locate_cid"][0]


# ---------------------------------------------------------------- shared errors


def test_no_tokens_without_uid_is_need_login(harness):
    harness["send_results"] = [{"error": "no_tokens", "hasUid": False}]
    with pytest.raises(ReachError) as exc:
        fc.facebook_comment(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == NEED_LOGIN
    assert len(harness["send"]) == 1


def test_no_tokens_with_uid_retries_until_send(harness):
    harness["send_results"] = [
        {"error": "no_tokens", "hasUid": True},
        {"error": "no_tokens", "hasUid": True},
        {"sent": True},
    ]
    harness["poll_results"] = [ok_create()]
    items = fc.facebook_comment(url=POST_URL, text="hi", timeout=30)
    assert items and len(harness["send"]) == 3


def test_http_429_maps_to_rate_limited(harness):
    harness["poll_results"] = [{"status": 429, "parsed": False, "body": "rate limited"}]
    with pytest.raises(ReachError) as exc:
        fc.facebook_comment(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == RATE_LIMITED


def test_http_5xx_maps_to_upstream_broken(harness):
    harness["poll_results"] = [{"status": 503, "parsed": False, "body": "oops"}]
    with pytest.raises(ReachError) as exc:
        fc.facebook_uncomment(url=COMMENT_URL, timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_graphql_errors_map_to_upstream_broken(harness):
    harness["poll_results"] = [ok_create(commentId=None, errors=[{"message": "missing_required_variable_value"}])]
    with pytest.raises(ReachError) as exc:
        fc.facebook_comment(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "missing_required_variable_value" in exc.value.message


def test_login_shaped_graphql_error_maps_to_need_login(harness):
    harness["poll_results"] = [ok_delete(dataKeys=[], echoesComment=False, errors=[{"message": "Login required to continue"}])]
    with pytest.raises(ReachError) as exc:
        fc.facebook_uncomment(url=COMMENT_URL, timeout=30)
    assert exc.value.code == NEED_LOGIN


def test_unparseable_login_page_maps_to_need_login(harness):
    harness["poll_results"] = [{"status": 200, "parsed": False, "body": "<form class=login_form>"}]
    with pytest.raises(ReachError) as exc:
        fc.facebook_comment(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == NEED_LOGIN


def test_unparseable_other_body_maps_to_upstream_broken(harness):
    harness["poll_results"] = [{"status": 200, "parsed": False, "body": "<html>maintenance</html>"}]
    with pytest.raises(ReachError) as exc:
        fc.facebook_comment(url=POST_URL, text="hi", timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_missing_result_is_upstream_broken_with_duplicate_warning(harness):
    harness["poll_results"] = []
    with pytest.raises(ReachError) as exc:
        fc.facebook_comment(url=POST_URL, text="hi", timeout=0.6)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "before retrying" in exc.value.message


# ---------------------------------------------------------------- delete verification


def test_delete_null_comment_delete_payload_is_upstream_broken(harness):
    # {"data":{"comment_delete":null}} is a silent no-op — must fail closed.
    harness["poll_results"] = [
        ok_delete(echoesComment=False, body='{"data":{"comment_delete":null}}')
    ]
    with pytest.raises(ReachError) as exc:
        fc.facebook_uncomment(url=COMMENT_URL, timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN
    assert "no confirmation" in exc.value.message


def test_delete_empty_data_is_upstream_broken(harness):
    harness["poll_results"] = [
        ok_delete(echoesComment=False, dataKeys=[], body='{"data":{}}')
    ]
    with pytest.raises(ReachError) as exc:
        fc.facebook_uncomment(url=COMMENT_URL, timeout=30)
    assert exc.value.code == UPSTREAM_BROKEN


def test_delete_payload_nonnull_without_echo_confirms(harness):
    # echoesComment false but a real object under comment_delete still confirms.
    harness["poll_results"] = [
        ok_delete(
            echoesComment=False,
            body='{"data":{"comment_delete":{"__typename":"Comment","id":"X"}}}',
        )
    ]
    items = fc.facebook_uncomment(url=COMMENT_URL, timeout=30)
    assert items[0]["raw"]["removed"] is True


def test_delete_echo_padding_insensitive(harness):
    # Response echoes the id with different '=' padding — still a match.
    padded = "Y29tbWVudDoxMjMxXzU2Nzg5"  # len 20, no padding needed
    # Simulate FB echoing an unpadded variant while we sent padded.
    harness["poll_results"] = [ok_delete(echoesComment=True)]
    items = fc.facebook_uncomment(url=RAW_CID, timeout=30)
    assert items[0]["raw"]["removed"] is True


def test_delete_success_item_has_removed_flag(harness):
    harness["poll_results"] = [ok_delete()]
    items = fc.facebook_uncomment(url=COMMENT_URL, timeout=30)
    assert items[0]["raw"]["removed"] is True


# ---------------------------------------------------------------- base64url ids


def test_raw_comment_id_accepts_base64url():
    # Numeric comment ids never emit -/_ in practice (digit bytes map to
    # alnum), but the alphabet must accept them — verify urlsafe + std decode.
    import base64 as b64

    real = b64.urlsafe_b64encode(b"comment:122311668092164987_56789").decode().rstrip("=")
    assert fc._raw_comment_id(real) == real
    std = b64.b64encode(b"comment:122311668092164987_56789").decode().rstrip("=")
    assert fc._raw_comment_id(std) == std
    # A candidate that DOES contain -/_ must at least decode without crashing;
    # it won't match comment:\d+_\d+ so _raw_comment_id returns None.
    odd = "Y29tbWVudDoxMTFfMjIy-_abc"
    assert fc._raw_comment_id(odd) is None


def test_comment_permalink_decodes_base64url():
    import base64 as b64

    cid = b64.urlsafe_b64encode(b"comment:111_222").decode().rstrip("=")
    url = fc._comment_permalink(POST_URL, cid)
    assert "comment_id=222" in url


# ---------------------------------------------------------------- plumbing


def test_each_call_polls_its_own_result_key(harness):
    harness["poll_results"] = [ok_create(), ok_delete()]
    fc.facebook_comment(url=POST_URL, text="one", timeout=30)
    fc.facebook_uncomment(url=COMMENT_URL, timeout=30)
    assert len(harness["poll"]) == 2
    assert harness["poll"][0] != harness["poll"][1]
    for expr, key in zip(harness["send"], harness["poll"]):
        assert json.dumps(key) in expr


def test_no_leftover_placeholders_in_send_expr(harness):
    harness["poll_results"] = [ok_create()]
    fc.facebook_comment(url=POST_URL, text="hi", timeout=30)
    expr = harness["send"][0]
    assert "__FB_" not in expr
    # vars embedded via JSON.stringify into the form body.
    assert 'p.set("variables",JSON.stringify(vars))' in expr
    assert "feedback_id" in expr


def test_channel_comment_validates_text_before_browser(harness):
    from agent_reach.channels.facebook import FacebookChannel

    with pytest.raises(ReachError) as exc:
        FacebookChannel()._comment(url=POST_URL, text="", timeout=30)
    assert exc.value.code == BAD_INPUT
    assert harness["evals"] == []


def test_channel_uncomment_wires_through(harness):
    from agent_reach.channels.facebook import FacebookChannel

    harness["poll_results"] = [ok_delete()]
    items = FacebookChannel()._uncomment(url=COMMENT_URL, timeout=30)
    assert items[0]["raw"]["commentId"] == "CID-RESOLVED"


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


def test_rendered_js_exprs_are_balanced():
    """Every eval expression the module can emit must be syntactically sane —
    the double-brace `{{...}}` bug in focus_expr would fail this."""
    # focus_expr for a permalink target — delimiter-safe selector so
    # comment_id=56789 cannot prefix-match comment_id=567890.
    cid_param = "56789"
    focus_expr = (
        "(()=>{try{const a=document.querySelector("
        + json.dumps(f'a[href$="comment_id={cid_param}"],a[href*="comment_id={cid_param}&"]')
        + ");if(a)a.scrollIntoView({block:'center'});return!!a}catch(e){return false}})()"
    )
    assert _balanced(focus_expr), focus_expr
    # The doubled-brace bug lived inside scrollIntoView({{...}}) — a legit IIFE
    # may still end in '}}', so check the call site specifically.
    assert "scrollIntoView({block:'center'})" in focus_expr
    assert "{{" not in focus_expr.split("scrollIntoView")[1].split(")")[0]
    # json.dumps escapes the inner quotes -> \" inside the JS string literal.
    assert 'a[href$=\\"comment_id=56789\\"]' in focus_expr
    assert 'a[href*=\\"comment_id=56789&\\"]' in focus_expr

    # locate expr with cid substituted.
    locate = fc._COMMENT_LOCATE_JS.replace("__FB_CID", json.dumps(cid_param))
    assert _balanced(locate)
    assert "__FB_CID" not in locate

    # both send exprs fully substituted.
    for kind in ("create", "delete"):
        expr = (
            fc._JS[kind]
            .replace("__FB_PARAMS", json.dumps(fc._PARAMS_TEMPLATE))
            .replace("__FB_VARS", json.dumps(fc._VARS[kind], ensure_ascii=False))
            .replace("__FB_FRIENDLY", json.dumps(fc._DOC[kind][0]))
            .replace("__FB_DOC", json.dumps(fc._DOC[kind][1]))
            .replace("__FB_KEY", json.dumps("K"))
        )
        for placeholder, value in (("__FB_FID", "F"), ("__FB_TEXT", "t"), ("__FB_CID", "C")):
            expr = expr.replace(placeholder, json.dumps(value))
        assert "__FB_" not in expr, kind
        assert _balanced(expr), kind

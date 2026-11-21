# -*- coding: utf-8 -*-
"""Facebook comment create + delete in the dedicated browser.

Replays Facebook's own GraphQL `useCometUFICreateCommentMutation` and
`useCometUFIDeleteCommentMutation` inside a logged-in facebook.com page,
fire-and-poll under a per-call window key — the same pipeline as
`_facebook_react` / `_facebook_write`, no new machinery.

Both mutations were captured once on a test post via
`scripts/capture_fb_comment.py` and kept in `_fb_comment_vars.json`
(sanitized: actor/uid -> "0", uuids -> zero-uuid, epochs -> 0). The delete
mutation's session-bound `tracking` blob was dropped (it is an opaque
per-action token, not replayable); the create mutation keeps its
`conversation_guide_session_id` with a zeroed uuid.

`doc_id` is a persisted-query id that rotates with FB builds — re-capture if
comments start failing with "document not found".

Comment delete needs the comment's OWN Relay `comment:<post>_<id>` node id,
NOT the post's feedback_id. Two inputs are accepted: a raw comment id (as
returned by `facebook comment`) passed through unchanged, or a comment
permalink (`?comment_id=<n>`) whose target comment id is resolved live from
the loaded page — never constructed from post_id/comment_id arithmetic.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.parse

from agent_reach.runtime.result import (
    BAD_INPUT,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
)

from ._facebook_react import (
    _FB_HOST_RE,
    _check_url,
    _ensure_post_loaded,
    _resolve_feedback_id,
)
from ._facebook_write import (
    _eval,
    _fb_target,
    _result_key,
    _wait_ready,
    classify_result,
    fb_send_js,
    fb_write_target,
    load_skeleton_sections,
    send_mutation,
)

# Persisted-query ids, one per mutation (rotate with FB builds).
_DOC = {
    "create": ("useCometUFICreateCommentMutation", "29145340888419828"),
    "delete": ("useCometUFIDeleteCommentMutation", "28931450023144805"),
}

_VARS_DOC = load_skeleton_sections("_fb_comment_vars.json")
_CREATE_VARS = (_VARS_DOC["sections"].get("create") or {}).get("variables") or {}
_DELETE_VARS = (_VARS_DOC["sections"].get("delete") or {}).get("variables") or {}

# base64 OR base64url alphabet (+ optional '=' padding).
_COMMENT_ID_RE = re.compile(r"^[A-Za-z0-9+/=_-]{20,400}$")

# Live-resolve a comment permalink's target comment Relay id. __FB_CID is the
# numeric comment_id from the URL. The page embeds comment node ids as base64
# "comment:<post>_<id>" strings; we keep only those whose decoded suffix equals
# the requested comment_id, then normalize to the padded form FB sends.
_COMMENT_LOCATE_JS = r"""
(()=>{try{
const cid=String(__FB_CID);
const h=document.documentElement.innerHTML;
const re=/Y29tbWVud[A-Za-z0-9+/_=-]{6,200}/g;
const found={};
let m;
while((m=re.exec(h))){
const v=m[0];
if(found[v])continue;
let d="";
try{d=atob(v.replace(/-/g,"+").replace(/_/g,"/"));}catch(e){continue}
const mm=/^comment:[0-9]+_([0-9]+)$/.exec(d);
if(mm&&mm[1]===cid){
const pad=v+"=".repeat((4-v.length%4)%4);
found[pad]=1;
}
}
const ids=Object.keys(found);
return JSON.stringify({href:location.href,ids:ids,count:ids.length});
}catch(e){return JSON.stringify({error:String(e)})}})()
"""

# Fire the comment mutation WITHOUT awaiting it — the result lands in
# window[<key>] under a per-call key so overlapping comments cannot clobber
# each other's result (same fire-and-poll pattern as _facebook_react). The
# __FB_* placeholders are substituted before eval; __FB_FID/__FB_TEXT/__FB_CID
# are JSON strings, __FB_VARS is a JS object literal.
_CREATE_JS = fb_send_js(
    """\
vars.input.feedback_id=__FB_FID;
vars.input.message.text=__FB_TEXT;
vars.input.client_mutation_id=String(Math.floor(Math.random()*900)+100);
vars.input.idempotence_token="client:"+crypto.randomUUID();
vars.input.session_id=crypto.randomUUID();
vars.input.downstream_share_session_id=crypto.randomUUID();
vars.input.downstream_share_session_origin_uri=location.href;
vars.input.downstream_share_session_start_time=String(Date.now());""",
    """\
const node=(((j.data||{}).comment_create||{}).feedback_comment_edge||{}).node||{};
out.commentId=node.id||null;
out.feedbackId=(node.feedback||{}).id||null;
out.bodyText=(((node.body||{}).text)||((node.preferred_body||{}).text))||null;
out.createdTime=node.created_time||null;""",
)

_DELETE_JS = fb_send_js(
    """\
vars.input.comment_id=__FB_CID;
vars.input.actor_id=uid;
vars.input.client_mutation_id=String(Math.floor(Math.random()*900)+100);""",
    """\
const dd=j.data||{};
out.dataKeys=Object.keys(dd);
// Compare padding-insensitively: the response may echo the id with a different
// '=' padding than the one we sent.
const stripPad=s=>String(s||"").split("=").join("");
out.echoesComment=stripPad(JSON.stringify(dd)).indexOf(stripPad(__FB_CID))>=0;""",
)

_JS = {"create": _CREATE_JS, "delete": _DELETE_JS}
_VARS = {"create": _CREATE_VARS, "delete": _DELETE_VARS}


def _raw_comment_id(target: str) -> str | None:
    """Return the padded Relay comment id when `target` is a raw comment id."""
    if not isinstance(target, str):
        return None
    s = target.strip()
    if not _COMMENT_ID_RE.match(s):
        return None
    try:
        pad = s + "=" * (-len(s) % 4)
        decoded = base64.urlsafe_b64decode(pad).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None
    if not re.fullmatch(r"comment:\d+_\d+", decoded):
        return None
    # Keep the caller's own padding; FB accepts unpadded base64 too, and the
    # delete response echoes the id verbatim for the `echoesComment` check.
    return s


def _resolve_comment_id(target_id: str, url: str, timeout: float) -> str:
    """Resolve a comment permalink's target comment Relay id live from the page.

    Fails closed: zero matches keeps polling, more than one distinct match is
    UPSTREAM_BROKEN (never delete an ambiguous object)."""
    cid = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("comment_id", [None])[0]
    if not cid:
        raise ReachError(BAD_INPUT, "uncomment needs a comment permalink with ?comment_id= or a raw comment id")
    expr = _COMMENT_LOCATE_JS.replace("__FB_CID", json.dumps(cid))
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        raw = _eval(target_id, expr, 10)
        try:
            info = json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError):
            info = {}
        if info.get("count") == 1:
            return info["ids"][0]
        if (info.get("count") or 0) > 1:
            raise ReachError(
                UPSTREAM_BROKEN,
                f"Ambiguous comment permalink — {info['count']} comment nodes match comment_id={cid}; refusing to delete",
            )
        last = info
        time.sleep(0.8)
    raise ReachError(
        UPSTREAM_BROKEN,
        f"Could not resolve the comment id on the page ({last or 'no data'}) — is this a comment permalink?",
    )


def _send_mutation(kind: str, target_id: str, *, deadline: float, **fields) -> dict:
    """Fire-and-poll one comment mutation; returns the parsed page result."""
    friendly, doc_id = _DOC[kind]
    key = _result_key("__fbCommentResult")
    expr = (
        _JS[kind]
        .replace("__FB_VARS", json.dumps(_VARS[kind], ensure_ascii=False))
        .replace("__FB_FRIENDLY", json.dumps(friendly))
        .replace("__FB_DOC", json.dumps(doc_id))
        .replace("__FB_KEY", json.dumps(key))
    )
    for placeholder, value in fields.items():
        expr = expr.replace(placeholder, json.dumps(value))
    return send_mutation(target_id, expr, deadline=deadline, key=key, label=f"Comment {kind}", eval_fn=_eval)


def _deleted_comment(out: dict) -> bool:
    """Positive deletion signal for kind="delete".

    Requires the `comment_delete` payload key AND either the response echoing
    the comment id (`echoesComment`, padding-insensitive) or a non-null value
    under that key. A bare `{"data":{"comment_delete":null}}` / `{}` is a
    silent no-op and must fail closed."""
    keys = out.get("dataKeys") or []
    if not any(str(k).startswith("comment_delete") for k in keys):
        return False
    if out.get("echoesComment"):
        return True
    # Fallback: non-null payload under comment_delete (body is truncated at
    # 2000 chars and may carry trailing braces, so raw_decode the first value).
    body = out.get("body") or ""
    marker = '"comment_delete":'
    i = body.find(marker)
    if i < 0:
        return False
    try:
        value, _ = json.JSONDecoder().raw_decode(body[i + len(marker):].lstrip())
        return value is not None
    except (TypeError, ValueError):
        return False


def _check_graphql(kind: str, out: dict) -> None:
    """Map transport/GraphQL failures onto Reach errors (shared by both kinds)."""
    classify_result(
        out,
        label="comment mutation",
        name=_DOC[kind][0],
        # Positive confirmation wins over non-fatal errors[] — otherwise a
        # successful create/delete reports UPSTREAM_BROKEN and retries duplicate.
        confirmed=(lambda: bool(out.get("commentId"))) if kind == "create" else (lambda: _deleted_comment(out)),
    )

    if kind == "delete" and not _deleted_comment(out):
        raise ReachError(
            UPSTREAM_BROKEN,
            "Comment delete returned no confirmation (comment_delete payload missing/null) — "
            "check facebook.com before retrying; the comment may not be yours or may not exist",
        )


def _comment_permalink(post_url: str, comment_id: str) -> str:
    """Build a comment permalink from the post URL + numeric comment id."""
    try:
        decoded = base64.urlsafe_b64decode(
            comment_id + "=" * (-len(comment_id) % 4)
        ).decode("utf-8", "ignore")
        numeric = decoded.rsplit("_", 1)[-1]
    except Exception:  # noqa: BLE001
        numeric = ""
    if not numeric.isdigit():
        return post_url
    sep = "&" if "?" in post_url else "?"
    return f"{post_url}{sep}comment_id={numeric}"


def facebook_comment(*, url: str, text: str, timeout: float) -> list:
    """Comment on a Facebook post with `text`."""
    _check_url(url)
    if not isinstance(text, str) or not text.strip():
        raise ReachError(BAD_INPUT, "text must be a non-empty string")
    deadline, left, tid = fb_write_target(timeout, target_fn=_fb_target, wait_fn=_wait_ready)

    _ensure_post_loaded(tid, url, left(25))
    fid = _resolve_feedback_id(tid, url, left(15))

    out = _send_mutation("create", tid, deadline=deadline, __FB_FID=fid, __FB_TEXT=text)
    _check_graphql("create", out)

    comment_id = out.get("commentId")
    if not comment_id:
        raise ReachError(UPSTREAM_BROKEN, "Comment mutation returned data but no comment id — check facebook.com before retrying")

    permalink = _comment_permalink(url, comment_id)
    body_text = out.get("bodyText") or text
    return [
        make_item(
            title=f"Facebook comment: {body_text[:80]}",
            url=permalink,
            text="Commented",
            raw={
                "status": out.get("status"),
                "url": permalink,
                "commentId": comment_id,
                "feedbackId": out.get("feedbackId"),
                "bodyText": body_text,
                "createdTime": out.get("createdTime"),
            },
        )
    ]


def facebook_uncomment(*, url: str, timeout: float) -> list:
    """Delete the current user's own comment, by permalink or raw comment id."""
    raw_id = _raw_comment_id(url)
    if raw_id is None:
        _check_url(url)
        cid_param = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("comment_id", [None])[0]
        if not cid_param:
            raise ReachError(BAD_INPUT, "uncomment needs a comment permalink with ?comment_id= or a raw comment id")
    deadline, left, tid = fb_write_target(timeout, target_fn=_fb_target, wait_fn=_wait_ready)

    if raw_id is not None:
        comment_id = raw_id
        # A raw id carries no post context; any logged-in facebook.com page has
        # the tokens the mutation needs. Navigate only when the current page is
        # not already on facebook.com (mirrors react's _ensure_post_loaded
        # pattern: poll location.href until the navigation lands).
        href = _eval(tid, "location.href", 8) or ""
        host = (urllib.parse.urlparse(href).hostname or "").lower()
        if not _FB_HOST_RE.search(host):
            from agent_reach.runtime import cdp

            target_url = "https://www.facebook.com/"
            cdp.call("Page.navigate", {"url": target_url}, target_id=tid)
            while time.monotonic() < deadline:
                try:
                    href = _eval(tid, "location.href", 8) or ""
                except ReachError:
                    href = ""
                host = (urllib.parse.urlparse(href).hostname or "").lower()
                if _FB_HOST_RE.search(host):
                    break
                time.sleep(0.5)
            _wait_ready(tid, left(20))
    else:
        _ensure_post_loaded(tid, url, left(25))
        # Load the comment thread so its Relay node id is present in the page.
        # A bare ?comment_id= permalink only highlights the comment once the
        # surrounding comments have streamed in; _resolve_comment_id polls
        # until the id appears (fails closed on ambiguity).
        cid_param = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["comment_id"][0]
        # $= / *&= so comment_id=56789 never prefix-matches comment_id=567890.
        focus_expr = (
            "(()=>{try{const a=document.querySelector("
            + json.dumps(f'a[href$="comment_id={cid_param}"],a[href*="comment_id={cid_param}&"]')
            + ");if(a)a.scrollIntoView({block:'center'});return!!a}catch(e){return false}})()"
        )
        _eval(tid, focus_expr, left(8))
        comment_id = _resolve_comment_id(tid, url, left(20))

    out = _send_mutation("delete", tid, deadline=deadline, __FB_CID=comment_id)
    _check_graphql("delete", out)

    return [
        make_item(
            title="Facebook comment deleted",
            url=url if raw_id is None else None,
            text="Comment deleted",
            raw={
                "status": out.get("status"),
                "url": url if raw_id is None else None,
                "commentId": comment_id,
                "dataKeys": out.get("dataKeys"),
                "echoesComment": out.get("echoesComment"),
                "removed": True,
            },
        )
    ]

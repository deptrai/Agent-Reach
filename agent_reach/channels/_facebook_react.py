# -*- coding: utf-8 -*-
"""Facebook react / unreact on posts and comments in the dedicated browser.

Replays Facebook's own GraphQL `CometUFIFeedbackReactMutation` — the SAME
mutation serves react and unreact (unreact = feedback_reaction_id "0") —
inside a logged-in facebook.com page, fire-and-poll under a per-call window
key, exactly like `_facebook_write.facebook_post`.

The mutation was captured once via scripts/capture_fb_mutation.py and kept in
`_fb_react_vars.json` (sanitized; the session-bound `tracking` blob was
dropped after a live replay proved it unnecessary). `doc_id` is a
persisted-query id that rotates with FB builds — re-capture if reactions
start failing with "document not found".

`feedback_id` (the Relay "feedback:pfbid..." node id) is resolved live from
the loaded post page: the first `"feedback_id"` occurrence in document order
is the post's own feedback (verified against a real capture). Comment
permalinks (`?comment_id=`) get a best-effort binding to the comment node's
feedback; when that fails the first post feedback_id is used, per plan.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse

from agent_reach.runtime import query_ids
from agent_reach.runtime.result import (
    BAD_INPUT,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
)

from ._facebook_write import (
    _eval,
    _fb_target,
    _result_key,
    _wait_ready,
    classify_result,
    fb_send_js,
    fb_write_target,
    load_skeleton,
    send_mutation,
)

# Fallback default — query-ids.json overrides it at send time (hot reload).
_DOC_ID = "27646120298312844"
_FRIENDLY = "CometUFIFeedbackReactMutation"

# Facebook reaction ids, verified live against the reaction picker's button
# labels (UI is Vietnamese: Thích/Yêu thích/Haha/Wow/Buồn/Phẫn nộ).
_REACTION_IDS = {
    "like": "1635855486666999",
    "love": "1678524932434102",
    "haha": "115940658764963",
    "wow": "478547315650144",
    "sad": "908563459236466",
    "angry": "444813342392137",
}

_VARS_SKELETON = load_skeleton("_fb_react_vars.json")[1]

_FB_HOST_RE = re.compile(r"(^|\.)(facebook\.com|fb\.com|fb\.watch)$", re.IGNORECASE)

# Ordered unique "feedback_id" values in document order + the comment-bound
# best-effort pick. __FB_CID is the comment_id from the URL (or null).
_LOCATE_JS = r"""
(()=>{try{
const h=document.documentElement.innerHTML;
// FB embeds page JSON both raw and HTML-escaped (&quot;) — match both forms
// without unescaping, so a literal &quot; in user text cannot forge a match.
const re=/"feedback_id":"([^"]+)"|&quot;feedback_id&quot;:&quot;([^&]+)&quot;/g;
const ids=[];let m;
while((m=re.exec(h))){const v=m[1]||m[2];if(ids.indexOf(v)<0){ids.push(v);if(ids.length>=16)break}}
let fid=ids[0]||null;
const cid=__FB_CID;
// Comment node ids decode to "comment:<post>_<cid>" — scan base64 blobs (same
// pattern as _facebook_comment._COMMENT_LOCATE_JS); btoa("comment:"+cid) can
// never match that form. cidBound=false when a comment id was requested but
// no matching node exists on the page — callers must not fall back to the
// post's fid (that would write to the wrong target).
let cidBound=!cid;
if(cid&&fid){
try{
const cre=/Y29tbWVud[A-Za-z0-9+/_=-]{6,200}/g;
let nm;
while((nm=cre.exec(h))){
const v=nm[0];
let d="";
try{d=atob(v.replace(/-/g,"+").replace(/_/g,"/"));}catch(e){continue}
const mm=/^comment:[0-9]+_([0-9]+)$/.exec(d);
if(mm&&mm[1]===cid){
const ni=nm.index;
const seg=h.slice(Math.max(0,ni-4000),ni+12000);
const cm=seg.match(/"feedback_id":"([^"]+)"|&quot;feedback_id&quot;:&quot;([^&]+)&quot;/)||seg.match(/"feedback":\{"id":"([^"]+)"\}|&quot;feedback&quot;:{&quot;id&quot;:&quot;([^&]+)&quot;}/);
if(cm){const cv=cm[1]||cm[2]||cm[3]||cm[4];if(cv&&cv!==fid&&ids.indexOf(cv)>=0){fid=cv;cidBound=true;}}
break;
}
}
}catch(e){}
}
return JSON.stringify({href:location.href,fid:fid,cidBound:cidBound,count:ids.length});
}catch(e){return JSON.stringify({error:String(e)})}})()
"""

# Build params from window.require + fire the reaction WITHOUT awaiting it —
# the result lands in window[<key>] under a per-call key so overlapping
# reactions cannot clobber each other's result (same fire-and-poll pattern as
# _facebook_write._POST_JS). __FB_* are substituted before eval; __FB_FID and
# __FB_RID are simple JSON strings, __FB_VARS is a JSON literal.
_SEND_JS = fb_send_js(
    """\
vars.input.feedback_id=__FB_FID;
vars.input.feedback_reaction_id=__FB_RID;
vars.input.actor_id=uid;
vars.input.client_mutation_id=String(Math.floor(Math.random()*900)+100);
vars.input.session_id=crypto.randomUUID();
vars.input.downstream_share_session_id=crypto.randomUUID();
vars.input.downstream_share_session_origin_uri=location.href;
vars.input.downstream_share_session_start_time=String(Date.now());""",
    """\
const fr=(j.data||{}).feedback_react||(j.data||{}).feedback_unreact||{};
const fb=fr.feedback||{};
out.reactionId=(fb.viewer_feedback_reaction_info||{}).id||null;
out.reactionCount=(fb.reaction_count||{}).count;""",
)


def _check_url(url: str) -> urllib.parse.ParseResult:
    # urlparse raises TypeError/AttributeError (or returns bytes parts) for
    # non-str input — reject before parsing so callers always get BAD_INPUT.
    if not isinstance(url, str):
        raise ReachError(BAD_INPUT, "url must be a facebook.com / fb.com / fb.watch post or comment URL")
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise ReachError(BAD_INPUT, f"Invalid URL: {exc}") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not _FB_HOST_RE.search(host):
        raise ReachError(BAD_INPUT, "url must be a facebook.com / fb.com / fb.watch post or comment URL")
    return parsed


def _ensure_post_loaded(target_id: str, url: str, timeout: float) -> None:
    """Navigate the facebook.com tab to `url` unless it already shows it."""
    from agent_reach.runtime import cdp

    deadline = time.monotonic() + timeout
    href = _eval(target_id, "location.href", 8) or ""
    if _same_post_path(href, url):
        return
    cdp.call("Page.navigate", {"url": url}, target_id=target_id)
    # Page.navigate is fire-and-forget: the OLD document stays evaluable until
    # the new one commits, so poll location.href until the target post/comment
    # is actually showing — otherwise _resolve_feedback_id could read the old
    # document's feedback_id and react the wrong post.
    while time.monotonic() < deadline:
        try:
            href = _eval(target_id, "location.href", 8) or ""
        except ReachError:
            href = ""  # execution context destroyed mid-navigation
        if _same_post_path(href, url):
            break
        time.sleep(0.5)
    else:
        raise ReachError(UPSTREAM_BROKEN, f"Navigation to {url} did not reach the target page")
    _wait_ready(target_id, max(1.0, deadline - time.monotonic()))


def _post_id_of(url: str) -> str | None:
    """Extract the numeric post id from a post URL — `/posts/<id>` path or
    `permalink.php?story_fbid=<id>` query. ASCII digits only."""
    try:
        parts = urllib.parse.urlparse(url or "")
    except (TypeError, ValueError):
        return None
    m = re.search(r"/posts/([0-9]+)", parts.path or "")
    if m:
        return m.group(1)
    fbid = (urllib.parse.parse_qs(parts.query).get("story_fbid") or [None])[0]
    if fbid and re.fullmatch(r"[0-9]+", str(fbid)):
        return str(fbid)
    return None


def _same_post_path(href: str, url: str) -> bool:
    """True when `href` already shows the same post AND the same comment.

    Path alone is not enough: a comment permalink shares the post's path, so
    a comment_id mismatch must trigger navigation (otherwise the comment is
    never loaded and the reaction would land on the post). The comparison
    uses the resolved post identity, not the raw path: Facebook 302-redirects
    `permalink.php?story_fbid=N` to the canonical `/<uid>/posts/N`, so a
    path-only compare would never match and every write action on a
    `facebook post`-style URL would loop to UPSTREAM_BROKEN."""
    a = urllib.parse.urlparse(href or "")
    b = urllib.parse.urlparse(url)
    cid_a = urllib.parse.parse_qs(a.query).get("comment_id", [None])[0]
    cid_b = urllib.parse.parse_qs(b.query).get("comment_id", [None])[0]
    if cid_a != cid_b:
        return False
    if a.netloc and a.path.rstrip("/") == b.path.rstrip("/"):
        return True
    pa, pb = _post_id_of(href), _post_id_of(url)
    return bool(pa and pb and pa == pb)


def _resolve_feedback_id(target_id: str, url: str, timeout: float) -> str:
    """First feedback_id on the page (post's own), comment-bound when the URL
    carries comment_id. Retries while the page streams in."""
    cid = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("comment_id", [None])[0]
    expr = _LOCATE_JS.replace("__FB_CID", json.dumps(cid))
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        raw = _eval(target_id, expr, 10)
        try:
            info = json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError):
            info = {}
        if info.get("fid"):
            if info.get("cidBound") is False:
                raise ReachError(
                    UPSTREAM_BROKEN,
                    "Comment node not found on the page — refusing to target the parent post",
                )
            return info["fid"]
        last = info
        time.sleep(0.8)
    raise ReachError(
        UPSTREAM_BROKEN,
        f"Could not resolve a feedback_id on the page ({last or 'no data'}) — is this a Facebook post or comment URL?",
    )


def _send_reaction(target_id: str, *, fid: str, rid: str, deadline: float) -> dict:
    """Fire-and-poll the reaction mutation; returns the parsed page result."""
    key = _result_key("__fbReactResult")
    expr = (
        _SEND_JS.replace("__FB_VARS", json.dumps(_VARS_SKELETON, ensure_ascii=False))
        .replace("__FB_FID", json.dumps(fid))
        .replace("__FB_RID", json.dumps(rid))
        .replace("__FB_FRIENDLY", json.dumps(_FRIENDLY))
        .replace("__FB_DOC", json.dumps(query_ids.get("facebook", "react", _DOC_ID)))
        .replace("__FB_KEY", json.dumps(key))
    )
    return send_mutation(target_id, expr, deadline=deadline, key=key, label="Reaction", eval_fn=_eval)


def _reaction_result(out: dict, *, url: str, reaction: str | None, rid: str) -> list:
    """Map the GraphQL page result onto Reach errors / a result item."""
    status = out.get("status")
    classify_result(
        out,
        label="reaction",
        name=_FRIENDLY,
        # Positive signal wins over non-fatal errors[]: react confirms via
        # reactionId, unreact via a cleared/missing reaction state.
        confirmed=lambda: bool(out.get("reactionId")) if reaction is not None else out.get("reactionId") is None,
    )

    # GraphQL returned data — verify the reaction state actually changed, so a
    # duplicate react / silent no-op cannot masquerade as ok:true.
    rid_out = out.get("reactionId")
    if reaction is None:
        if rid_out is not None and out.get("reactionCount") != 0:
            raise ReachError(
                UPSTREAM_BROKEN,
                f"Unreact did not take effect (reactionId={rid_out!r}, count={out.get('reactionCount')})",
            )
        verb = "Removed reaction"
    else:
        if not rid_out:
            raise ReachError(UPSTREAM_BROKEN, f"{reaction} reaction was not applied — no reactionId in GraphQL response")
        if str(rid_out) != rid:
            raise ReachError(UPSTREAM_BROKEN, f"Unexpected reaction id {rid_out!r} (expected {rid})")
        verb = f"Reacted {reaction}"

    return [
        make_item(
            title=f"Facebook {verb}",
            url=url,
            text=verb,
            raw={
                "status": status,
                "url": url,
                "reactionId": out.get("reactionId"),
                "reactionCount": out.get("reactionCount"),
            },
        )
    ]


def _react_or_unreact(*, url: str, reaction: str | None, timeout: float) -> list:
    _check_url(url)
    rid = "0" if reaction is None else _REACTION_IDS[reaction]
    deadline, left, tid = fb_write_target(timeout, target_fn=_fb_target, wait_fn=_wait_ready)

    _ensure_post_loaded(tid, url, left(25))
    fid = _resolve_feedback_id(tid, url, left(15))

    out = _send_reaction(tid, fid=fid, rid=rid, deadline=deadline)
    return _reaction_result(out, url=url, reaction=reaction, rid=rid)


def facebook_react(*, url: str, reaction: str, timeout: float) -> list:
    """React to a post/comment with `reaction` (like|love|haha|wow|sad|angry)."""
    if reaction not in _REACTION_IDS:
        raise ReachError(BAD_INPUT, "reaction must be one of: " + ", ".join(_REACTION_IDS))
    return _react_or_unreact(url=url, reaction=reaction, timeout=timeout)


def facebook_unreact(*, url: str, timeout: float) -> list:
    """Remove the current user's reaction from a post/comment."""
    return _react_or_unreact(url=url, reaction=None, timeout=timeout)

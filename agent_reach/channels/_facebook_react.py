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
import uuid
from pathlib import Path

from agent_reach.runtime.result import (
    BAD_INPUT,
    NEED_LOGIN,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
)

from ._facebook_write import _error_message, _eval, _fb_target, _wait_ready

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

_VARS_DOC = json.loads(
    (Path(__file__).with_name("_fb_react_vars.json")).read_text(encoding="utf-8")
)
if "params" in _VARS_DOC or "variables" in _VARS_DOC:
    _PARAMS_TEMPLATE = _VARS_DOC.get("params") or {}
    _VARS_SKELETON = _VARS_DOC.get("variables") or {}
else:
    _PARAMS_TEMPLATE, _VARS_SKELETON = {}, _VARS_DOC

_FB_HOST_RE = re.compile(r"(^|\.)(facebook\.com|fb\.com|fb\.watch)$", re.IGNORECASE)

# Ordered unique "feedback_id" values in document order + the comment-bound
# best-effort pick. __FB_CID is the comment_id from the URL (or null).
_LOCATE_JS = r"""
(()=>{try{
const h=document.documentElement.innerHTML;
const re=/"feedback_id":"([^"]+)"/g;
const ids=[];let m;
while((m=re.exec(h))){if(ids.indexOf(m[1])<0){ids.push(m[1]);if(ids.length>=16)break}}
let fid=ids[0]||null;
const cid=__FB_CID;
if(cid&&fid){
try{
const nodeId=btoa("comment:"+cid);
const ni=h.indexOf('"id":"'+nodeId+'"');
if(ni>=0){
const seg=h.slice(Math.max(0,ni-4000),ni+12000);
const cm=seg.match(/"feedback_id":"([^"]+)"/)||seg.match(/"feedback":\{"id":"([^"]+)"/);
if(cm&&cm[1]&&cm[1]!==fid&&ids.indexOf(cm[1])>=0)fid=cm[1];
}
}catch(e){}
}
return JSON.stringify({href:location.href,fid:fid,count:ids.length});
}catch(e){return JSON.stringify({error:String(e)})}})()
"""

# Build params from window.require + fire the reaction WITHOUT awaiting it —
# the result lands in window[<key>] under a per-call key so overlapping
# reactions cannot clobber each other's result (same fire-and-poll pattern as
# _facebook_write._POST_JS). __FB_* are substituted before eval; __FB_FID and
# __FB_RID are simple JSON strings, __FB_VARS/__FB_PARAMS are JSON literals.
_SEND_JS = r"""
(()=>{try{
const req=window.require;
if(!req)return{error:"no_require"};
const uid=(req("CurrentUserInitialData")||{}).USER_ID;
const dtsg=(req("DTSGInitialData")||{}).token;
const lsd=(req("LSD")||{}).token;
const site=req("SiteData")||{};
if(!uid||!dtsg||!lsd)return{error:"no_tokens",hasUid:!!uid,hasDtsg:!!dtsg,hasLsd:!!lsd};
const vars=__FB_VARS;
vars.input.feedback_id=__FB_FID;
vars.input.feedback_reaction_id=__FB_RID;
vars.input.actor_id=uid;
vars.input.client_mutation_id=String(Math.floor(Math.random()*900)+100);
vars.input.session_id=crypto.randomUUID();
vars.input.downstream_share_session_id=crypto.randomUUID();
vars.input.downstream_share_session_origin_uri=location.href;
vars.input.downstream_share_session_start_time=String(Date.now());
const p=new URLSearchParams();
const tmpl=__FB_PARAMS;
for(const k in tmpl)p.set(k,tmpl[k]);
p.set("av",uid);p.set("__user",uid);p.set("__a","1");
p.set("__comet_req",site.comet_req||"15");
p.set("fb_dtsg",dtsg);p.set("jazoest",site.jazoest||"");
p.set("lsd",lsd);
p.set("__spin_r",site.__spin_r||"");p.set("__spin_b",site.__spin_b||"");p.set("__spin_t",site.__spin_t||"");
p.set("__rev",site.client_revision||site.__spin_r||"");p.set("__hsi",site.hsi||"");p.set("__hs",site.hsp||"");
p.set("fb_api_req_friendly_name",__FB_FRIENDLY);
p.set("fb_api_caller_class","RelayModern");
p.set("doc_id",__FB_DOC);
p.set("server_timestamps","true");
p.set("variables",JSON.stringify(vars));
const KEY=__FB_KEY;
// Sent sentinel: a retried eval after a transport timeout must not re-fire
// the mutation — return early; the poll picks up the in-flight result.
if(window[KEY+"_sent"])return{sent:true,dup:true};
window[KEY+"_sent"]=true;
window[KEY]=null;
fetch("/api/graphql/",{method:"POST",credentials:"include",
headers:{"Content-Type":"application/x-www-form-urlencoded","X-FB-Friendly-Name":__FB_FRIENDLY},
body:p.toString()}).then(r=>r.text().then(t=>{
const out={status:r.status,actorId:uid,body:t.slice(0,2000)};
const clean=t.replace(/^for\s*\(\s*;\s*\)\s*;\s*/,"");
let j=null;
try{j=JSON.parse(clean)}catch(e){
for(const line of clean.split("\n")){
if(!line.trim())continue;
try{const o=JSON.parse(line);if(o&&(o.data||o.errors)){j=o;break}}catch(_){}
}
}
if(j){
out.parsed=true;
out.errors=Array.isArray(j.errors)?j.errors.slice(0,5):null;
const fr=(j.data||{}).feedback_react||(j.data||{}).feedback_unreact||{};
const fb=fr.feedback||{};
out.reactionId=(fb.viewer_feedback_reaction_info||{}).id||null;
out.reactionCount=(fb.reaction_count||{}).count;
}else{out.parsed=false}
window[KEY]=out;
})).catch(e=>{window[KEY]={error:String(e)}});
return{sent:true}}catch(e){return{error:String(e)}}})()
"""


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
            return info["fid"]
        last = info
        time.sleep(0.8)
    raise ReachError(
        UPSTREAM_BROKEN,
        f"Could not resolve a feedback_id on the page ({last or 'no data'}) — is this a Facebook post or comment URL?",
    )


def _send_reaction(target_id: str, *, fid: str, rid: str, deadline: float) -> dict:
    """Fire-and-poll the reaction mutation; returns the parsed page result."""

    def left(cap: float) -> float:
        return max(1.0, min(deadline - time.monotonic(), cap))

    key = f"__fbReactResult_{uuid.uuid4().hex}"
    expr = (
        _SEND_JS.replace("__FB_PARAMS", json.dumps(_PARAMS_TEMPLATE))
        .replace("__FB_VARS", json.dumps(_VARS_SKELETON, ensure_ascii=False))
        .replace("__FB_FID", json.dumps(fid))
        .replace("__FB_RID", json.dumps(rid))
        .replace("__FB_FRIENDLY", json.dumps(_FRIENDLY))
        .replace("__FB_DOC", json.dumps(_DOC_ID))
        .replace("__FB_KEY", json.dumps(key))
    )

    sent = None
    for _ in range(6):
        sent = _eval(target_id, expr, left(10))
        if isinstance(sent, dict) and sent.get("error") == "no_tokens" and sent.get("hasUid"):
            time.sleep(1.5)
            continue
        break
    if not isinstance(sent, dict) or not sent.get("sent"):
        err = (sent or {}).get("error") if isinstance(sent, dict) else sent
        if err == "no_tokens":
            raise ReachError(NEED_LOGIN, "facebook.com is not logged in — open https://www.facebook.com/login in the Agent Reach browser")
        raise ReachError(UPSTREAM_BROKEN, f"Reaction could not be sent: {err!r}")

    poll_expr = f"window[{json.dumps(key)}]||null"
    out = None
    while time.monotonic() < deadline:
        out = _eval(target_id, poll_expr, left(8))
        if out:
            break
        time.sleep(0.7)
    if not isinstance(out, dict):
        raise ReachError(
            UPSTREAM_BROKEN,
            "Reaction sent but no result returned — check facebook.com before retrying",
        )
    if out.get("error"):
        raise ReachError(UPSTREAM_BROKEN, f"Reaction failed in page: {out['error'][:200]}")
    return out


def _reaction_result(out: dict, *, url: str, reaction: str | None, rid: str) -> list:
    """Map the GraphQL page result onto Reach errors / a result item."""
    status = out.get("status")
    if status == 429:
        raise ReachError(RATE_LIMITED, "Facebook rate-limited the reaction — slow down before retrying")
    if isinstance(status, int) and status >= 500:
        raise ReachError(UPSTREAM_BROKEN, f"Facebook returned HTTP {status} for the reaction mutation")

    if not out.get("parsed"):
        head = (out.get("body") or "").lower()
        if any(w in head for w in ("login_form", "checkpoint", "not logged")):
            raise ReachError(NEED_LOGIN, "Facebook rejected the session — log in again in the Agent Reach browser")
        raise ReachError(UPSTREAM_BROKEN, f"Unparseable GraphQL response (HTTP {status}): {head[:200]}")

    errors = out.get("errors") or []
    if errors:
        message = _error_message(errors)
        if any(w in message.lower() for w in ("login", "checkpoint", "session")):
            raise ReachError(NEED_LOGIN, f"Facebook session rejected: {message}")
        raise ReachError(UPSTREAM_BROKEN, f"CometUFIFeedbackReactMutation failed: {message}")

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
    from agent_reach.runtime import session_reuse

    _check_url(url)
    rid = "0" if reaction is None else _REACTION_IDS[reaction]
    session_reuse.prepare("facebook")

    deadline = time.monotonic() + timeout

    def left(cap: float) -> float:
        return max(1.0, min(deadline - time.monotonic(), cap))

    target = _fb_target(create=True)
    if not target:
        raise ReachError(UPSTREAM_BROKEN, "No facebook.com tab could be opened in the dedicated browser")
    tid = target["id"]
    _wait_ready(tid, left(25))

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

# -*- coding: utf-8 -*-
"""Facebook share-to-own-timeline in the dedicated browser.

Shares a post to the caller's own timeline by replaying Facebook's
`ComposerStoryCreateMutation` with `composer_type: "share"` — the SAME
mutation the web composer fires for a normal post, but with
`reshare_original_post` plus an `attachments[0].link.share_scrape_data`
JSON blob `{"share_type":22,"share_params":[<numeric post id>]}` pointing at
the source post. Fire-and-poll under a per-call window key, exactly like
`_facebook_write.facebook_post` / `_facebook_react`.

The mutation was captured once via scripts/capture_fb_share.py (drive the
real "Chia sẻ"/Share dialog on a public post, confirm inside the dialog) and
kept sanitized in `_fb_share_vars.json` (`{params, variables}`; actor/uid ->
"0", uuids -> zero-uuid, epochs -> 0, the session-bound encrypted `tracking`
blob dropped -> [null]). `doc_id` is a persisted-query id that rotates with
FB builds — re-capture if shares start failing with "document not found".

The source post id comes from the URL (`/posts/<id>` or
`permalink.php?story_fbid=<id>`); when the URL carries no numeric id (pfbid
links) the first `"post_id":"<digits>"` in the loaded page's document order
is used — the same first-in-document-order heuristic as feedback_id
resolution in `_facebook_react`.
"""

from __future__ import annotations

import json
import time

from agent_reach.runtime.result import (
    BAD_INPUT,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
)

from ._facebook_react import _check_url, _ensure_post_loaded, _post_id_of
from ._facebook_write import (
    _eval,
    _fb_target,
    _result_key,
    _wait_ready,
    classify_result,
    load_skeleton,
    prepare_facebook,
    send_mutation,
)

_DOC_ID = "29304741702489334"
_FRIENDLY = "ComposerStoryCreateMutation"

_PARAMS_TEMPLATE, _VARS_SKELETON = load_skeleton("_fb_share_vars.json")

_AUDIENCE = {"only_me": "SELF", "friends": "FRIENDS", "public": "EVERYONE"}

# Ordered unique "post_id" values in document order — the first is the
# loaded post's own numeric id (fallback for URLs that carry no numeric id).
_LOCATE_POSTID_JS = r"""
(()=>{try{
const h=document.documentElement.innerHTML;
const re=/"post_id":"([0-9]{8,})"/g;
const ids=[];let m;
while((m=re.exec(h))){if(ids.indexOf(m[1])<0){ids.push(m[1]);if(ids.length>=16)break}}
return JSON.stringify({href:location.href,pid:ids[0]||null,count:ids.length});
}catch(e){return JSON.stringify({error:String(e)})}})()
"""

# Build params from window.require + fire the share WITHOUT awaiting it —
# the result lands in window[<key>] under a per-call key so overlapping
# shares cannot clobber each other's result (same fire-and-poll pattern as
# _facebook_write._POST_JS). __FB_* are substituted before eval.
_SEND_JS = r"""
(()=>{try{
const req=window.require;
if(!req)return{error:"no_require"};
const uid=(req("CurrentUserInitialData")||{}).USER_ID;
// getAsyncParams returns the whole live request envelope — __csr/__dyn/
// __hsdp/__sjsp checksums, __hsi, __s, __req, fb_dtsg/lsd/jazoest — all
// consistent with the CURRENT page build and session. Hand-assembling a
// stale template makes the share mutation fail with a field_exception.
const gp=req("getAsyncParams");
if(!uid||!gp)return{error:"no_tokens",hasUid:!!uid};
const base=gp("POST")||{};
if(!base.fb_dtsg||!base.lsd)return{error:"no_tokens",hasUid:true};
const vars=__FB_VARS;
const link=(vars.input.attachments[0]||{}).link;
// Post ids exceed 2^53 — Number() would round them and FB reports
// "can't load the post". Concatenate the digits verbatim instead.
link.share_scrape_data='{"share_type":22,"share_params":['+__FB_POST+']}';
vars.input.message={ranges:[],text:__FB_TEXT};
vars.input.audience={privacy:{allow:[],base_state:__FB_AUD,deny:[],tag_expansion_state:"UNSPECIFIED"}};
const sid=crypto.randomUUID();
vars.input.idempotence_token=sid+"_FEED";
vars.input.logging.composer_session_id=sid;
vars.input.actor_id=uid;
vars.input.client_mutation_id=String(Math.floor(Math.random()*900)+100);
const p=new URLSearchParams();
for(const k in base)p.set(k,base[k]);
p.set("av",uid);
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
const sc=(j.data||{}).story_create||{};
const story=sc.story||{};
out.storyUrl=story.url||null;
out.storyId=story.legacy_story_hideable_id||story.id||null;
out.postId=sc.post_id||null;
out.newPostId=sc.new_post_id||null;
}else{out.parsed=false}
window[KEY]=out;
})).catch(e=>{window[KEY]={error:String(e)}});
return{sent:true}}catch(e){return{error:String(e)}}})()
"""


def _resolve_post_id(target_id: str, url: str, timeout: float) -> str:
    """Numeric source-post id for share_params — from the URL when it
    carries one, else the first post_id on the loaded page (retries while
    the page streams in)."""
    pid = _post_id_of(url)
    if pid:
        return pid
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        raw = _eval(target_id, _LOCATE_POSTID_JS, 10)
        try:
            info = json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError):
            info = {}
        if info.get("pid"):
            return info["pid"]
        last = info
        time.sleep(0.8)
    raise ReachError(
        UPSTREAM_BROKEN,
        f"Could not resolve a numeric post id for the share ({last or 'no data'}) — is this a Facebook post URL?",
    )


def _send_share(target_id: str, *, post_id: str, text: str, audience: str, deadline: float) -> dict:
    """Fire-and-poll the share mutation; returns the parsed page result."""
    key = _result_key("__fbShareResult")
    expr = (
        _SEND_JS.replace("__FB_VARS", json.dumps(_VARS_SKELETON, ensure_ascii=False))
        # Splice raw — post_id is guaranteed [0-9]+ by _post_id_of, and the
        # captured shape is a numeric array element (["<id>"] quoted form is a
        # divergence from the real payload).
        .replace("__FB_POST", post_id)
        .replace("__FB_AUD", json.dumps(_AUDIENCE[audience]))
        .replace("__FB_FRIENDLY", json.dumps(_FRIENDLY))
        .replace("__FB_DOC", json.dumps(_DOC_ID))
        .replace("__FB_KEY", json.dumps(key))
        # __FB_TEXT last: user text could itself contain another sentinel.
        .replace("__FB_TEXT", json.dumps(text, ensure_ascii=False))
    )
    return send_mutation(target_id, expr, deadline=deadline, key=key, label="Share", dup_hint=True, eval_fn=_eval)


def _share_result(out: dict, *, post_id: str, text: str, audience: str) -> list:
    """Map the GraphQL page result onto Reach errors / a result item."""
    status = out.get("status")
    classify_result(
        out,
        label="share",
        name=_FRIENDLY,
        confirmed=lambda: bool(out.get("storyUrl") or out.get("storyId") or out.get("newPostId") or out.get("postId")),
    )

    # GraphQL returned data — a share that created no new story must not
    # report success (fail-closed, same as facebook_post).
    shared_url = out.get("storyUrl")
    if not shared_url:
        story_id = str(out.get("storyId") or "")
        if story_id.isdigit() and out.get("actorId"):
            shared_url = f"https://www.facebook.com/permalink.php?story_fbid={story_id}&id={out['actorId']}"
    if not shared_url:
        new_post_id = str(out.get("postId") or out.get("newPostId") or "")
        if new_post_id.isdigit() and out.get("actorId"):
            shared_url = f"https://www.facebook.com/{out['actorId']}/posts/{new_post_id}"
    if not shared_url and not (out.get("storyId") or out.get("newPostId") or out.get("postId")):
        raise ReachError(UPSTREAM_BROKEN, "Share mutation returned data but no new story — check facebook.com before retrying")

    return [
        make_item(
            title=(text or "Shared a post")[:120],
            url=shared_url,
            text=f"Shared ({audience})",
            raw={
                "status": status,
                "audience": audience,
                "url": shared_url,
                "sourcePostId": post_id,
                "storyId": out.get("storyId"),
                "newPostId": out.get("newPostId"),
            },
        )
    ]


def facebook_share(*, url: str, text: str = "", audience: str = "only_me", timeout: float) -> list:
    """Share a Facebook post to the caller's own timeline."""
    if audience not in _AUDIENCE:
        raise ReachError(BAD_INPUT, "audience must be one of: only_me, friends, public")
    _check_url(url)
    prepare_facebook()

    deadline = time.monotonic() + timeout

    def left(cap: float) -> float:
        return max(1.0, min(deadline - time.monotonic(), cap))

    target = _fb_target(create=True)
    if not target:
        raise ReachError(UPSTREAM_BROKEN, "No facebook.com tab could be opened in the dedicated browser")
    tid = target["id"]
    _wait_ready(tid, left(25))

    _ensure_post_loaded(tid, url, left(25))
    post_id = _resolve_post_id(tid, url, left(15))

    out = _send_share(tid, post_id=post_id, text=text or "", audience=audience, deadline=deadline)
    return _share_result(out, post_id=post_id, text=text or "", audience=audience)

# -*- coding: utf-8 -*-
"""Facebook write actions (post a status) in the shared Agent Reach browser.

Reads go through `opencli facebook`; writes replay Facebook's own GraphQL
`ComposerStoryCreateMutation` inside a logged-in facebook.com page of the
dedicated browser. All transport params (fb_dtsg, lsd, jazoest, spin tokens,
__user...) are read live from `window.require` on the page — no request
capture needed (verified: __dyn/__csr/etc. are NOT required by the endpoint).
The `variables` skeleton below (including the __relay_internal__pv__* provider
flags) is captured once from a real composer post — re-capture if posts start
failing with missing_required_variable_value.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from agent_reach.runtime.result import NEED_LOGIN, UPSTREAM_BROKEN, ReachError, make_item

# Persisted-query id for ComposerStoryCreateMutation (rotates with FB builds).
_DOC_ID = "29221801510819514"

_AUDIENCE = {"only_me": "SELF", "friends": "FRIENDS", "public": "EVERYONE"}

# `variables` skeleton captured verbatim from a real composer post
# (audience/text/tokens are overwritten at send time). Kept as a JSON resource
# so it can be re-captured without touching code.
_VARS_SKELETON = json.loads(
    (Path(__file__).with_name("_facebook_vars.json")).read_text(encoding="utf-8")
)

# Build params from window.require + fire the post WITHOUT awaiting it —
# result lands in window.__fbPostResult. The eval returns immediately, so a
# slow mutation can't hit the CDP socket timeout; retries would duplicate the
# post. __FB_VARS / __FB_TEXT / __FB_AUD / __FB_DOC are substituted before eval.
_POST_JS = r"""
(()=>{try{
const req=window.require;
if(!req)return{error:"no_require"};
const uid=(req("CurrentUserInitialData")||{}).USER_ID;
const dtsg=(req("DTSGInitialData")||{}).token;
const lsd=(req("LSD")||{}).token;
const site=req("SiteData")||{};
if(!uid||!dtsg||!lsd)return{error:"no_tokens",hasUid:!!uid,hasDtsg:!!dtsg,hasLsd:!!lsd};
const vars=__FB_VARS;
vars.input.message.text=__FB_TEXT;
vars.input.audience={privacy:{allow:[],base_state:__FB_AUD,deny:[],tag_expansion_state:"UNSPECIFIED"}};
vars.input.idempotence_token=crypto.randomUUID()+"_FEED";
vars.input.logging={...(vars.input.logging||{}),composer_session_id:crypto.randomUUID()};
vars.input.client_mutation_id="1";
vars.input.actor_id=uid;
const p=new URLSearchParams();
p.set("av",uid);p.set("__user",uid);p.set("__a","1");
p.set("__comet_req",site.comet_req||"15");
p.set("fb_dtsg",dtsg);p.set("jazoest",site.jazoest||"");
p.set("lsd",lsd);
p.set("__spin_r",site.__spin_r||"");p.set("__spin_b",site.__spin_b||"");p.set("__spin_t",site.__spin_t||"");
p.set("__rev",site.client_revision||site.__spin_r||"");p.set("__hsi",site.hsi||"");p.set("__hs",site.hsp||"");
p.set("fb_api_req_friendly_name","ComposerStoryCreateMutation");
p.set("fb_api_caller_class","RelayModern");
p.set("doc_id",__FB_DOC);
p.set("server_timestamps","true");
p.set("variables",JSON.stringify(vars));
window.__fbPostResult=null;
fetch("/api/graphql/",{method:"POST",credentials:"include",
headers:{"Content-Type":"application/x-www-form-urlencoded","X-FB-Friendly-Name":"ComposerStoryCreateMutation"},
body:p.toString()}).then(r=>r.text().then(t=>{window.__fbPostResult={status:r.status,body:t.slice(0,4000)}})).catch(e=>{window.__fbPostResult={error:String(e)}});
return{sent:true}}catch(e){return{error:String(e)}}})()
"""


def _fb_target(create: bool):
    """A facebook.com page target in the dedicated browser (create one if absent)."""
    from agent_reach.runtime import cdp

    targets = cdp._json("/json/list") or []
    for t in targets:
        if t.get("type") == "page" and "facebook.com" in t.get("url", ""):
            return t
    if not create:
        return None
    made = cdp.call("Target.createTarget", {"url": "https://www.facebook.com/"})
    tid = (made or {}).get("targetId")
    if not tid:
        return None
    for t in cdp._json("/json/list") or []:
        if t.get("id") == tid:
            return t
    return {"id": tid, "type": "page", "url": "https://www.facebook.com/"}


def _eval(target_id: str, expr: str, timeout: float = 30.0) -> object:
    """Runtime.evaluate on a page target; retries transient None results a busy
    (still-loading) page produces, raises on persistent failure."""
    from agent_reach.runtime import cdp

    result = None
    for _ in range(4):
        result = cdp.call(
            "Runtime.evaluate",
            {"expression": expr, "awaitPromise": True, "returnByValue": True, "timeout": int(timeout * 1000)},
            target_id=target_id,
        )
        if result is not None:
            break
        time.sleep(0.5)
    if result is None:
        raise ReachError(UPSTREAM_BROKEN, "Could not reach the dedicated browser page")
    res = (result or {}).get("result") or {}
    if res.get("subtype") == "error" or (result.get("exceptionDetails") or {}).get("text"):
        raise ReachError(UPSTREAM_BROKEN, str(res.get("description") or result.get("exceptionDetails", {}).get("text"))[:300])
    return res.get("value")


def _wait_ready(target_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _eval(target_id, "document.readyState", 8) in ("interactive", "complete"):
            if _eval(target_id, "!!window.require", 8):
                return
        time.sleep(0.8)
    raise ReachError(UPSTREAM_BROKEN, "facebook.com did not finish loading in the dedicated browser")


def facebook_post(*, text: str, audience: str, timeout: float) -> list:
    """Post `text` as a status update in the dedicated browser's FB session."""
    from agent_reach.runtime import session_reuse

    session_reuse.prepare("facebook")
    target = _fb_target(create=True)
    if not target:
        raise ReachError(UPSTREAM_BROKEN, "No facebook.com tab could be opened in the dedicated browser")
    _wait_ready(target["id"], min(timeout, 25))

    expr = (
        _POST_JS.replace("__FB_VARS", json.dumps(_VARS_SKELETON, ensure_ascii=False))
        .replace("__FB_TEXT", json.dumps(text, ensure_ascii=False))
        .replace("__FB_AUD", json.dumps(_AUDIENCE[audience]))
        .replace("__FB_DOC", json.dumps(_DOC_ID))
    )
    sent = _eval(target["id"], expr, 10)
    if not isinstance(sent, dict) or not sent.get("sent"):
        err = (sent or {}).get("error") if isinstance(sent, dict) else sent
        if err == "no_tokens":
            raise ReachError(NEED_LOGIN, "facebook.com is not logged in — open https://www.facebook.com/login in the Agent Reach browser")
        raise ReachError(UPSTREAM_BROKEN, f"Post could not be sent: {err!r}")

    out = None
    deadline = time.monotonic() + min(timeout, 30)
    while time.monotonic() < deadline:
        out = _eval(target["id"], "window.__fbPostResult||null", 8)
        if out:
            break
        time.sleep(0.7)
    if not isinstance(out, dict):
        raise ReachError(
            UPSTREAM_BROKEN,
            "Post sent but no result returned — check facebook.com before retrying to avoid duplicates",
        )
    if out.get("error"):
        raise ReachError(UPSTREAM_BROKEN, f"Post failed in page: {out['error'][:200]}")
    body = out.get("body") or ""
    low = body.lower()
    if any(w in low for w in ("login_form", "checkpoint", "not logged")):
        raise ReachError(NEED_LOGIN, "Facebook rejected the session — log in again in the Agent Reach browser")
    if '"errors"' in body or ("story_create" not in body and '"data"' not in body):
        raise ReachError(UPSTREAM_BROKEN, f"ComposerStoryCreateMutation failed: {body[:300]}")
    m = re.search(r'"url":"([^"]*facebook\.com[^"]*)"', body)
    url = m.group(1).replace("\\/", "/") if m else None
    return [
        make_item(
            title=text[:120],
            url=url or "https://www.facebook.com/me",
            text=f"Posted ({audience})",
            raw={"status": out.get("status"), "audience": audience, "url": url},
        )
    ]

# -*- coding: utf-8 -*-
"""X write actions (post a tweet) in the shared Agent Reach browser.

Reads go through `opencli twitter`; writes replay X's own `CreateTweet`
GraphQL mutation inside a logged-in x.com page of the dedicated browser.
X has no `window.require` — the request envelope is reassembled live in
page context: the bearer token comes from the webpack registry
(`__wr('973526').Rb()`, where `__wr` is captured via the
`webpackChunk_twitter_responsive_web` push-trick), `x-csrf-token` from the
`ct0` cookie, and `x-client-transaction-id` is minted by
`__wr('265390').kc(host, path, method)`. The request shape (variables,
features, static `x-twitter-*` headers) replays a skeleton captured once
from a real composer post via `scripts/capture_mutation.py --platform x`
and sanitized — re-capture it if posts start failing.

Send is the shared fire-and-poll pattern from `_facebook_write`: the eval
fires `fetch` without awaiting it and the result lands in a per-call
window key, so a slow mutation can't hit the CDP socket timeout and a
transport-layer retry can't double-post.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from agent_reach.runtime import query_ids
from agent_reach.runtime.result import (
    BAD_INPUT,
    NEED_LOGIN,
    PERSISTED_QUERY_DEAD,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
)

from ._facebook_write import _error_message, _eval, _result_key, send_mutation

_X_HOST_SUFFIXES = ("x.com", "twitter.com")

# Public web-client bearer — last-resort only; live extraction via
# __wr('973526').Rb() (and a module-source scan) runs first on every send.
_PUBLIC_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)


def load_x_skeleton(filename: str) -> dict:
    """Load an X mutation skeleton: {operation, method, query_id,
    path_template, headers_template, body_template}."""
    return json.loads((Path(__file__).with_name(filename)).read_text(encoding="utf-8"))


_SKELETON = load_x_skeleton("_x_create_tweet_vars.json")


def prepare_twitter() -> None:
    """Warm the dedicated browser session for x.com (all X write verbs)."""
    from agent_reach.runtime import session_reuse

    session_reuse.prepare("twitter")


def _is_x_url(url: str) -> bool:
    """True only when the URL's hostname is a real X domain — substring
    matching would accept ``notx.com.evil.example``."""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    return any(host == s or host.endswith("." + s) for s in _X_HOST_SUFFIXES)


def _x_target(create: bool):
    """An x.com page target in the dedicated browser (create one if absent)."""
    from agent_reach.runtime import cdp

    targets = cdp._json("/json/list") or []
    for t in targets:
        if t.get("type") == "page" and _is_x_url(t.get("url", "")):
            return t
    if not create:
        return None
    made = cdp.call("Target.createTarget", {"url": "https://x.com/home"})
    tid = (made or {}).get("targetId")
    if not tid:
        return None
    for t in cdp._json("/json/list") or []:
        if t.get("id") == tid:
            return t
    return {"id": tid, "type": "page", "url": "https://x.com/home"}


def _wait_x_ready(target_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _eval(target_id, "document.readyState", 8) in ("interactive", "complete"):
            if _eval(target_id, "!!window.webpackChunk_twitter_responsive_web", 8):
                return
        time.sleep(0.8)
    raise ReachError(UPSTREAM_BROKEN, "x.com did not finish loading in the dedicated browser")


def x_write_target(
    timeout: float,
    *,
    target_fn: Callable | None = None,
    wait_fn: Callable | None = None,
) -> tuple[float, Callable[[float], float], str]:
    """Warm the X session, arm one deadline, open an x.com tab and wait for
    page readiness — the X twin of ``fb_write_target``.

    Returns ``(deadline, left, target_id)``; ``target_fn``/``wait_fn``
    default to this module's ``_x_target``/``_wait_x_ready`` so tests stub
    per-module (same contract as ``send_mutation.eval_fn``)."""
    prepare_twitter()
    if target_fn is None:
        target_fn = _x_target
    if wait_fn is None:
        wait_fn = _wait_x_ready
    deadline = time.monotonic() + timeout

    def left(cap: float) -> float:
        return max(1.0, min(deadline - time.monotonic(), cap))

    target = target_fn(create=True)
    if not target:
        raise ReachError(UPSTREAM_BROKEN, "No x.com tab could be opened in the dedicated browser")
    wait_fn(target["id"], left(25))
    return deadline, left, target["id"]


# The async IIFE mints txid BEFORE firing, then fires WITHOUT awaiting the
# fetch — the result lands in window[<key>] under a per-call key. Token
# failures report {error:"no_tokens",hasUid:<ct0 seen>}: send_mutation
# retries them while webpack is still booting; no ct0 at all is a
# definitive logout. __X_* sentinels are substituted before eval —
# __X_TEXT always LAST, because the user's text could contain any of the
# other sentinel strings.
_X_SEND_TEMPLATE = r"""
(async()=>{try{
const ck=(n)=>{const m=document.cookie.match(new RegExp("(?:^|; )"+n+"=([^;]*)"));return m?decodeURIComponent(m[1]):null};
// ct0 is the JS-readable session marker; auth_token is HttpOnly and rides
// the cookie jar via credentials:"include" — checking it would false-fail.
const ct0=ck("ct0");
if(!ct0)return{error:"no_tokens",hasUid:false};
let wr=window.__wr;
if(!wr){try{webpackChunk_twitter_responsive_web.push([[Math.random()],{},(r)=>{window.__wr=r;wr=r}])}catch(e){}}
if(!wr||!wr.m)return{error:"no_tokens",hasUid:true};
let bearer=null;
try{const m=wr("973526");if(m&&typeof m.Rb==="function")bearer=m.Rb()}catch(e){}
if(!bearer||!/Bearer/.test(String(bearer))){try{
const ms=wr.m||{};
for(const id in ms){const s=Function.prototype.toString.call(ms[id]);
const mm=s.match(/Bearer AAAAAAAA[^"'\\\s]+/);if(mm){bearer=mm[0];break}}
}catch(e){}}
if(!bearer)bearer="Bearer " + __X_PUBLIC_BEARER;
const PATH=__X_PATH;
let txid=null,txErr=null;
try{const m=wr("265390");if(m&&typeof m.kc==="function")txid=await m.kc("x.com",PATH,"POST")}catch(e){txErr=String(e)}
if(!txid){try{
const ms=wr.m||{};
for(const id in ms){const s=Function.prototype.toString.call(ms[id]);
if(s.indexOf("x-client-transaction-id")<0)continue;
const m=wr(id);const f=m&&(m.kc||(m.default&&m.default.kc));
if(typeof f==="function"){try{txid=await f("x.com",PATH,"POST")}catch(e){txErr=String(e)}if(txid)break}}
}catch(e){if(!txErr)txErr=String(e)}}
if(!txid)return{error:"txid_unavailable:"+(txErr||"module missing")};
const headers=__X_HEADERS;
headers["authorization"]=String(bearer).indexOf("Bearer")===0?String(bearer):("Bearer "+bearer);
headers["x-csrf-token"]=ct0;
headers["x-client-transaction-id"]=txid;
for(const k in headers){if(/^<.*>$/.test(String(headers[k])))delete headers[k]}
const body=__X_BODY;
body.queryId=__X_QID;
body.variables=body.variables||{};
body.variables.tweet_text=__X_TEXT;
const KEY=__X_KEY;
if(window[KEY+"_sent"])return{sent:true,dup:true};
window[KEY+"_sent"]=true;
window[KEY]=null;
fetch(PATH,{method:"POST",credentials:"include",headers:headers,body:JSON.stringify(body)}).then(r=>r.text().then(t=>{
const out={status:r.status,body:t.slice(0,2000)};
let j=null;try{j=JSON.parse(t)}catch(e){}
if(j){out.parsed=true;out.errors=Array.isArray(j.errors)?j.errors.slice(0,5):null;
const r0=(((j.data||{}).create_tweet||{}).tweet_results||{}).result||{};
const tw=(r0.tweet&&(r0.__typename==="TweetWithVisibilityResults"||r0.__typename==="TweetWithVisibilityResult"))?r0.tweet:r0;
const leg=tw.legacy||{};
out.tweetId=leg.id_str||tw.rest_id||null;
out.echoText=leg.full_text||null;
const usr=((tw.core||{}).user_results||{}).result||{};
out.screenName=(usr.legacy||{}).screen_name||null;
out.tweetUrl=(out.tweetId&&out.screenName)?("https://x.com/"+out.screenName+"/status/"+out.tweetId):null;
}else{out.parsed=false}
window[KEY]=out;
})).catch(e=>{window[KEY]={error:String(e)}});
return{sent:true}}catch(e){return{error:String(e)}}})()
"""


def x_classify_result(
    out: dict,
    *,
    label: str,
    name: str,
    confirmed: Callable[[], bool] | None = None,
) -> bool:
    """Map transport/GraphQL failures onto Reach errors; returns True when
    the mutation was positively confirmed.

    ``label`` is the rate-limit/HTTP phrase stem ("post", "reply", ...);
    ``name`` is the GraphQL operation name for errors[] failures.
    ``confirmed`` is checked BEFORE errors[] — X can attach non-fatal
    errors to a payload that still created the tweet."""
    status = out.get("status")
    if status == 429:
        raise ReachError(RATE_LIMITED, f"X rate-limited the {label} — slow down before retrying")
    if isinstance(status, int) and status >= 500:
        raise ReachError(UPSTREAM_BROKEN, f"X returned HTTP {status} for the {label} mutation")
    if status == 404:
        # GraphQL ops 404 when the persisted queryId rotated — terminal for
        # this lane until re-capture (same signature classify_result maps).
        raise ReachError(PERSISTED_QUERY_DEAD, f"{name}: HTTP 404 — persisted queryId is dead on this build")

    if not out.get("parsed"):
        head = (out.get("body") or "").lower()
        if status in (401, 403) or any(w in head for w in ("login", "unauthorized")):
            raise ReachError(NEED_LOGIN, "X rejected the session — log in again in the Agent Reach browser")
        raise ReachError(UPSTREAM_BROKEN, f"Unparseable GraphQL response (HTTP {status}): {head[:200]}")

    if confirmed is not None and confirmed():
        return True

    errors = out.get("errors") or []
    if errors:
        message = _error_message(errors)
        lowered = message.lower()
        codes = {e.get("code") for e in errors if isinstance(e, dict)}
        if 88 in codes or "rate limit" in lowered:
            raise ReachError(RATE_LIMITED, f"X rate-limited the {label}: {message}")
        if codes & {32, 89, 401} or any(w in lowered for w in ("authenticat", "login", "session")):
            raise ReachError(NEED_LOGIN, f"X session rejected: {message}")
        if "not found" in lowered and ("query" in lowered or "document" in lowered):
            raise ReachError(PERSISTED_QUERY_DEAD, f"{name} failed: {message}")
        raise ReachError(UPSTREAM_BROKEN, f"{name} failed: {message}")
    return False


def twitter_post(*, text: str, timeout: float) -> list:
    """Post `text` as a tweet in the dedicated browser's X session."""
    if not text or not text.strip():
        raise ReachError(BAD_INPUT, "tweet text is empty")
    if len(text) > 280:
        # Raw char count: every counting scheme makes >280 chars invalid.
        raise ReachError(BAD_INPUT, "tweet text is over 280 characters")
    # One shared deadline for every phase — a fresh budget per phase could
    # overrun the dispatch timeout and strand the in-flight post.
    deadline, left, tid = x_write_target(timeout, target_fn=_x_target, wait_fn=_wait_x_ready)

    key = _result_key("__xPostResult")
    qid = query_ids.get("x", "CreateTweet", _SKELETON.get("query_id") or "")
    path = (_SKELETON.get("path_template") or "/i/api/graphql/{query_id}/CreateTweet").replace("{query_id}", qid)
    expr = (
        _X_SEND_TEMPLATE.replace("__X_PUBLIC_BEARER", json.dumps(_PUBLIC_BEARER))
        .replace("__X_PATH", json.dumps(path))
        .replace("__X_HEADERS", json.dumps(_SKELETON.get("headers_template") or {}, ensure_ascii=False))
        .replace("__X_BODY", json.dumps(_SKELETON.get("body_template") or {}, ensure_ascii=False))
        .replace("__X_QID", json.dumps(qid))
        .replace("__X_KEY", json.dumps(key))
        .replace("__X_TEXT", json.dumps(text, ensure_ascii=False))
    )
    try:
        out = send_mutation(tid, expr, deadline=deadline, key=key, label="Post", eval_fn=_eval, dup_hint=True)
        x_classify_result(out, label="post", name="CreateTweet", confirmed=lambda: bool(out.get("tweetId")))
    except ReachError as exc:
        # send_mutation's NEED_LOGIN names facebook.com — reword for this lane.
        if exc.code == NEED_LOGIN and "facebook.com" in (exc.message or ""):
            raise ReachError(
                NEED_LOGIN,
                "x.com is not logged in — open https://x.com/i/flow/login in the Agent Reach browser",
            ) from exc
        raise
    url = out.get("tweetUrl")
    tweet_id = out.get("tweetId")
    if not url and tweet_id:
        url = f"https://x.com/i/web/status/{tweet_id}"
    if not url:
        raise ReachError(
            UPSTREAM_BROKEN,
            "Mutation returned data but no tweet — check x.com before retrying to avoid duplicates",
        )
    return [
        make_item(
            title=text[:120],
            url=url,
            text="Posted",
            raw={
                "status": out.get("status"),
                "tweetId": tweet_id,
                "url": url,
                "screenName": out.get("screenName"),
                "echo": out.get("echoText"),
            },
        )
    ]

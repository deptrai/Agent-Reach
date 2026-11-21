# -*- coding: utf-8 -*-
"""Facebook write actions (post a status) in the shared Agent Reach browser.

Reads go through `opencli facebook`; writes replay Facebook's own GraphQL
`ComposerStoryCreateMutation` inside a logged-in facebook.com page of the
dedicated browser. Token params (fb_dtsg, lsd, jazoest, spin tokens, __user)
are read live from `window.require`; the remaining transport params
(__dyn, __csr, __hsdp, __crn, __req, dpr...) are replayed from a captured
`getAsyncParams("POST")` envelope — they ARE required on some builds: without
them FB answers "The GraphQL document with ID … was not found" even when
doc_id is still valid. The whole file ({params, variables}, including the
__relay_internal__pv__* provider flags) is captured once from a real composer
post via scripts/capture_fb_mutation.py, sanitized, then re-captured if posts
start failing.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import uuid
from collections.abc import Callable
from pathlib import Path

from agent_reach.runtime import query_ids
from agent_reach.runtime.result import (
    NEED_LOGIN,
    PERSISTED_QUERY_DEAD,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
)

# Persisted-query id for ComposerStoryCreateMutation (rotates with FB builds).
# Fallback default — query-ids.json overrides it at send time (hot reload).
_DOC_ID = "29221801510819514"

_AUDIENCE = {"only_me": "SELF", "friends": "FRIENDS", "public": "EVERYONE"}


def load_skeleton(filename: str) -> tuple[dict, dict]:
    """Load a single-mutation vars skeleton -> (params, variables).

    Accepts the {"params": ..., "variables": ...} envelope and a flat
    variables-only file (legacy captures)."""
    doc = json.loads((Path(__file__).with_name(filename)).read_text(encoding="utf-8"))
    if "params" in doc or "variables" in doc:
        return doc.get("params") or {}, doc.get("variables") or {}
    return {}, doc


def load_skeleton_sections(filename: str) -> dict:
    """Load a multi-mutation vars skeleton -> {"params": {...}, "sections": {kind: entry}}.

    comment/post-edit captures keep one entry per mutation kind beside a shared
    (or absent) top-level "params" template."""
    doc = json.loads((Path(__file__).with_name(filename)).read_text(encoding="utf-8"))
    return {"params": doc.get("params") or {}, "sections": {k: v for k, v in doc.items() if k != "params"}}


def prepare_facebook() -> None:
    """Warm the dedicated browser session for facebook.com (all write verbs)."""
    from agent_reach.runtime import session_reuse

    session_reuse.prepare("facebook")


def _result_key(prefix: str) -> str:
    """Per-call window key — isolates concurrent mutation results."""
    return f"{prefix}_{uuid.uuid4().hex}"


_FB_SEND_TEMPLATE = r"""
(()=>{try{
const req=window.require;
if(!req)return{error:"no_require"};
const uid=(req("CurrentUserInitialData")||{}).USER_ID;
// getAsyncParams returns the whole live request envelope — transport
// checksums (__csr/__dyn/__hsdp/__sjsp), __hsi, __s, __req, fb_dtsg/lsd/
// jazoest — consistent with the CURRENT page build and session. A
// hand-assembled template goes stale on every FB deploy.
const gp=req("getAsyncParams");
if(!uid||!gp)return{error:"no_tokens",hasUid:!!uid};
const base=gp("POST")||{};
if(!base.fb_dtsg||!base.lsd)return{error:"no_tokens",hasUid:true};
const vars=__FB_VARS;
__FB_VARS_MUT
const p=new URLSearchParams();
for(const k in base)p.set(k,base[k]);
p.set("av",uid);
p.set("fb_api_req_friendly_name",__FB_FRIENDLY);
p.set("fb_api_caller_class","RelayModern");
p.set("doc_id",__FB_DOC);
p.set("server_timestamps","true");
p.set("variables",JSON.stringify(vars));
const KEY=__FB_KEY;
// Sent sentinel: if a previous eval of this expression timed out at the
// transport layer but actually executed, the retry must not fire the
// mutation a second time — it returns early and the poll picks up the
// in-flight result instead.
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
__FB_RESULT_MUT
}else{out.parsed=false}
window[KEY]=out;
})).catch(e=>{window[KEY]={error:String(e)}});
return{sent:true}}catch(e){return{error:String(e)}}})()
"""


def fb_send_js(vars_mut: str, result_mut: str, *, body_slice: int = 2000) -> str:
    """Compose a page-side GraphQL send expression from the shared template.

    ``vars_mut`` is JS executed after ``const vars=__FB_VARS;`` — the
    per-verb mutations (message text, feedback_id, attachments, audience
    ...). ``result_mut`` is JS run inside ``if(j){...}`` that extracts
    response fields into ``out`` — per-verb (storyUrl, commentId,
    reactionId, deletedStoryId, ...). Everything else — token/envelope
    handling, transport params, the sent sentinel, fetch, for(;;); strip,
    NDJSON fallback, window-key store — is shared here.

    ``body_slice`` overrides the ``out.body`` preview length — post_edit
    keeps 4000 because its payload is huge and the echo check reads the
    full body ``t`` (the slice only feeds the unparseable-response head).
    """
    expr = (_FB_SEND_TEMPLATE
            .replace("__FB_VARS_MUT", vars_mut)
            .replace("__FB_RESULT_MUT", result_mut))
    if body_slice != 2000:
        expr = expr.replace("body:t.slice(0,2000)", f"body:t.slice(0,{body_slice})")
    return expr


def fb_write_target(
    timeout: float,
    *,
    target_fn: Callable | None = None,
    wait_fn: Callable | None = None,
) -> tuple[float, Callable[[float], float], str]:
    """Shared write-verb scaffold: warm the facebook session, arm one
    deadline, open a facebook.com tab and wait for page readiness.

    Returns ``(deadline, left, target_id)`` where ``left(cap)`` is the
    shared deadline helper — pass ``deadline`` to ``send_mutation`` and
    ``left`` to ``_eval``/``_ensure_post_loaded``/resolvers.
    ``target_fn``/``wait_fn`` default to this module's
    ``_fb_target``/``_wait_ready`` — sibling callers pass their own so
    tests can stub per-module (same contract as ``send_mutation.eval_fn``)."""
    prepare_facebook()
    if target_fn is None:
        target_fn = _fb_target
    if wait_fn is None:
        wait_fn = _wait_ready
    deadline = time.monotonic() + timeout

    def left(cap: float) -> float:
        return max(1.0, min(deadline - time.monotonic(), cap))

    target = target_fn(create=True)
    if not target:
        raise ReachError(UPSTREAM_BROKEN, "No facebook.com tab could be opened in the dedicated browser")
    wait_fn(target["id"], left(25))
    return deadline, left, target["id"]


def send_mutation(
    target_id: str,
    expr: str,
    *,
    deadline: float,
    key: str | None = None,
    label: str = "Mutation",
    eval_fn: Callable | None = None,
    poll_expr: str | None = None,
    dup_hint: bool = False,
) -> dict:
    """Fire-and-poll a GraphQL mutation whose page-side result lands in
    ``window[key]``; shared by every facebook write verb.

    ``expr`` must already carry the substituted key — ``__FB_KEY`` is replaced
    BEFORE user-controlled fields so a literal "__FB_KEY" inside post text
    survives. ``label`` prefixes the send-phase error messages ("Post",
    "Comment create", ...); ``dup_hint`` appends the duplicate-warning suffix.
    ``eval_fn`` defaults to this module's ``_eval`` — callers in sibling
    modules pass their own so tests can stub per-module.
    """
    if eval_fn is None:
        eval_fn = _eval
    if key is None:
        key = _result_key("__fbResult")
    if poll_expr is None:
        poll_expr = f"window[{json.dumps(key)}]||null"

    def left(cap: float) -> float:
        return max(1.0, min(deadline - time.monotonic(), cap))

    # window.require can exist before DTSGInitialData/LSD register on slow
    # loads — retry `no_tokens` while a user id is present; no uid at all is a
    # definitive logout.
    sent = None
    for _ in range(6):
        sent = eval_fn(target_id, expr, left(10))
        if isinstance(sent, dict) and sent.get("error") == "no_tokens" and sent.get("hasUid"):
            time.sleep(1.5)
            continue
        break
    if not isinstance(sent, dict) or not sent.get("sent"):
        err = (sent or {}).get("error") if isinstance(sent, dict) else sent
        if err == "no_tokens":
            raise ReachError(NEED_LOGIN, "facebook.com is not logged in — open https://www.facebook.com/login in the Agent Reach browser")
        raise ReachError(UPSTREAM_BROKEN, f"{label} could not be sent: {err!r}")

    out = None
    while time.monotonic() < deadline:
        out = eval_fn(target_id, poll_expr, left(8))
        if out:
            break
        time.sleep(0.7)
    if not isinstance(out, dict):
        raise ReachError(
            UPSTREAM_BROKEN,
            f"{label} sent but no result returned — check facebook.com before retrying"
            + (" to avoid duplicates" if dup_hint else ""),
        )
    if out.get("error"):
        raise ReachError(UPSTREAM_BROKEN, f"{label} failed in page: {out['error'][:200]}")
    return out


def classify_result(out: dict, *, label: str, name: str, confirmed: Callable[[], bool] | None = None) -> bool:
    """Map transport/GraphQL failures onto Reach errors; returns True when the
    mutation was positively confirmed.

    ``label`` is the rate-limit/HTTP phrase stem ("post", "comment mutation",
    "reaction", "share", "post mutation"); ``name`` is the mutation's friendly
    name for errors[] failures. ``confirmed`` (post edit/delete only) is
    checked BEFORE errors[] because FB attaches non-fatal errors to payloads
    that still confirm the mutation.
    """
    status = out.get("status")
    if status == 429:
        raise ReachError(RATE_LIMITED, f"Facebook rate-limited the {label} — slow down before retrying")
    http_label = label if label.endswith("mutation") else f"{label} mutation"
    if isinstance(status, int) and status >= 500:
        raise ReachError(UPSTREAM_BROKEN, f"Facebook returned HTTP {status} for the {http_label}")

    if not out.get("parsed"):
        head = (out.get("body") or "").lower()
        if any(w in head for w in ("login_form", "checkpoint", "not logged")):
            raise ReachError(NEED_LOGIN, "Facebook rejected the session — log in again in the Agent Reach browser")
        raise ReachError(UPSTREAM_BROKEN, f"Unparseable GraphQL response (HTTP {status}): {head[:200]}")

    if confirmed is not None and confirmed():
        return True

    errors = out.get("errors") or []
    if errors:
        message = _error_message(errors)
        lowered = message.lower()
        if any(w in lowered for w in ("login", "checkpoint", "session")):
            raise ReachError(NEED_LOGIN, f"Facebook session rejected: {message}")
        # A rotated doc_id / dead persisted query is terminal for this lane —
        # retrying the identical replay cannot help (farm-account-contract §4).
        # Match the specific persisted-query signature, not any "not found".
        if "graphql document" in lowered and "not found" in lowered:
            raise ReachError(PERSISTED_QUERY_DEAD, f"{name} failed: {message}")
        raise ReachError(UPSTREAM_BROKEN, f"{name} failed: {message}")
    return False


# Skeleton captured from a real composer post: {"params": form params replayed
# verbatim, "variables": Relay variables with placeholders overwritten at send
# time}. A flat (params-less) file still loads as variables-only.
_VARS_SKELETON = load_skeleton("_fb_post_create_vars.json")[1]

# Build params from window.require + fire the post WITHOUT awaiting it — the
# result lands in window[<key>] under a per-call key so overlapping posts
# cannot clobber each other's result. The eval returns immediately, so a slow
# mutation can't hit the CDP socket timeout; retrying a sent post would
# duplicate it. __FB_VARS / __FB_AUD / __FB_DOC / __FB_KEY / __FB_TEXT are
# substituted before eval — __FB_TEXT always LAST, because the user's text
# could itself contain any of the other sentinel strings.
_POST_JS = (
    fb_send_js(
        """\
vars.input.message.text=__FB_TEXT;
vars.input.audience={privacy:{allow:[],base_state:__FB_AUD,deny:[],tag_expansion_state:"UNSPECIFIED"}};
vars.input.idempotence_token=crypto.randomUUID()+"_FEED";
vars.input.logging={...(vars.input.logging||{}),composer_session_id:crypto.randomUUID()};
vars.input.client_mutation_id=String(Math.floor(Math.random()*900)+100);
vars.input.actor_id=uid;""",
        """\
const sc=(j.data||{}).story_create||{};
const st=sc.story||{};
out.storyUrl=st.url||null;
out.storyId=st.legacy_story_hideable_id||st.id||null;
out.newPostId=sc.new_post_id||null;
out.postId=sc.post_id||null;""",
    )
    .replace("__FB_FRIENDLY", json.dumps("ComposerStoryCreateMutation"))
)


_FB_HOST_SUFFIXES = ("facebook.com", "fb.com", "fb.watch")


def _is_facebook_url(url: str) -> bool:
    """True only when the URL's hostname is a real Facebook domain —
    substring matching would accept ``notfacebook.com.evil.example``."""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    return any(host == s or host.endswith("." + s) for s in _FB_HOST_SUFFIXES)


def _fb_target(create: bool):
    """A facebook.com page target in the dedicated browser (create one if absent)."""
    from agent_reach.runtime import cdp

    targets = cdp._json("/json/list") or []
    for t in targets:
        if t.get("type") == "page" and _is_facebook_url(t.get("url", "")):
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


def _error_message(errors: list) -> str:
    parts = []
    for e in errors[:3]:
        if isinstance(e, dict):
            parts.append(str(e.get("message") or e.get("summary") or e)[:150])
        else:
            parts.append(str(e)[:150])
    return "; ".join(p for p in parts if p)


def facebook_post(*, text: str, audience: str, timeout: float) -> list:
    """Post `text` as a status update in the dedicated browser's FB session."""
    # One shared deadline for every phase — a fresh budget per phase could
    # overrun the dispatch timeout and strand the in-flight post.
    deadline, left, tid = fb_write_target(timeout, target_fn=_fb_target, wait_fn=_wait_ready)

    key = _result_key("__fbPostResult")
    expr = (
        _POST_JS.replace("__FB_VARS", json.dumps(_VARS_SKELETON, ensure_ascii=False))
        .replace("__FB_AUD", json.dumps(_AUDIENCE[audience]))
        .replace("__FB_DOC", json.dumps(query_ids.get("facebook", "post_create", _DOC_ID)))
        .replace("__FB_KEY", json.dumps(key))
        .replace("__FB_TEXT", json.dumps(text, ensure_ascii=False))
    )
    out = send_mutation(tid, expr, deadline=deadline, key=key, label="Post", dup_hint=True)
    classify_result(
        out,
        label="post",
        name="ComposerStoryCreateMutation",
        # FB attaches non-fatal errors to payloads that still confirm the
        # mutation — positive story refs must win over errors[] (same contract
        # post-edit uses) or a successful post reports failure and retries
        # duplicate it.
        confirmed=lambda: bool(out.get("storyUrl") or out.get("storyId") or out.get("newPostId") or out.get("postId")),
    )

    status = out.get("status")

    # Prefer the story permalink; fall back to building one from the numeric
    # story id — never to the actor's profile URL.
    url = out.get("storyUrl")
    if not url:
        story_id = str(out.get("storyId") or "")
        if story_id.isdigit() and out.get("actorId"):
            url = f"https://www.facebook.com/permalink.php?story_fbid={story_id}&id={out['actorId']}"
    if not url:
        post_id = str(out.get("postId") or "")
        if post_id.isdigit() and out.get("actorId"):
            url = f"https://www.facebook.com/{out['actorId']}/posts/{post_id}"
    if not url and not (out.get("storyId") or out.get("newPostId") or out.get("postId")):
        raise ReachError(UPSTREAM_BROKEN, "Mutation returned data but no story — check facebook.com before retrying")

    return [
        make_item(
            title=text[:120],
            url=url,
            text=f"Posted ({audience})",
            raw={"status": status, "audience": audience, "url": url, "newPostId": out.get("newPostId")},
        )
    ]

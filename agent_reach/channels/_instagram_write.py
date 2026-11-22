# -*- coding: utf-8 -*-
"""Instagram write actions (comment / like / unlike) in the Agent Reach browser.

Reads go through `opencli instagram`; writes replay Instagram's own GraphQL
mutations inside a logged-in instagram.com page — `POST /api/graphql` with
`usePolarisCommentCreateMutation` / `usePolarisLikeMediaXIG{Like,Unlike}Mutation`.
Instagram moved off the old `/api/v1/web/comments/<id>/add/` REST surface; those
paths now 404 with a `not-logged-in` shell page even on a live session.

Token params (fb_dtsg, lsd, jazoest, __dyn, __csr, __hsdp, __sjsp...) are minted
live by `window.require("getAsyncParams")("POST")` — the same envelope trick as
Facebook (Meta shares the transport stack). `av` (the actor/viewer id) is read
from `window.require("PolarisViewer").data.fbid`. A numeric `media_id` or a
`/p/`, `/reel/` or `/tv/` URL (or bare shortcode) resolves through the
deterministic shortcode↔media_id base64 alphabet — no lookup request needed.

`instagram post` is DESCOPED: a post is `PUT /rupload_igphoto/` then
`POST /api/v1/media/configure/` — a binary upload pipeline, not a single
replayable mutation (same class as X thread media, deferred).

Send is the fire-and-poll shape from `_facebook_write`: the eval fires
`fetch` without awaiting it and the result lands in a per-call window key,
so a slow response can't hit the CDP socket timeout and a transport retry
can't double-post.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections.abc import Callable

from agent_reach.runtime import query_ids
from agent_reach.runtime.result import (
    BAD_INPUT,
    NEED_LOGIN,
    PERSISTED_QUERY_DEAD,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
)

from ._facebook_write import _eval, _result_key

_IG_HOSTS = ("instagram.com", "instagr.am")
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,20}$")
_MEDIA_ID_RE = re.compile(r"^\d{8,25}$")

# Persisted-query ids (rotate with IG builds; query-ids.json overrides at send).
_DOC_COMMENT = "39617891021192323"
_DOC_LIKE = "27182485238052618"
_DOC_UNLIKE = "27345296031770102"

# IG JSON `message`/`feedback`/`error_summary` text that is the caller's problem.
_RATE_WORDS = ("please wait", "few minutes", "try again later", "rate")
# checkpoint/challenge/spam walls are terminal until the operator resolves them —
# retrying digs the hole deeper, so they are PLATFORM_BLOCKED, not UPSTREAM_BROKEN.
_BLOCK_WORDS = ("checkpoint_required", "challenge_required", "feedback_required", "spam")


def shortcode_to_media_id(shortcode: str) -> int:
    """Base64-ish decode of an IG shortcode to the numeric media id."""
    m = 0
    for ch in shortcode:
        i = _ALPHABET.find(ch)
        if i < 0:
            raise ReachError(BAD_INPUT, f"not a valid Instagram shortcode: {shortcode!r}")
        m = m * 64 + i
    return m


def media_id_to_shortcode(media_id: int) -> str:
    media_id = int(media_id)
    if media_id <= 0:
        raise ReachError(BAD_INPUT, f"not a valid Instagram media id: {media_id!r}")
    out = []
    while media_id:
        media_id, r = divmod(media_id, 64)
        out.append(_ALPHABET[r])
    return "".join(reversed(out))


def resolve_media_id(target: str) -> tuple[str, str]:
    """`media_id`, `/p/…|/reel/…|/tv/…` URL, or bare shortcode → (media_id, shortcode)."""
    t = (target or "").strip()
    if not t:
        raise ReachError(BAD_INPUT, "target must be a post/reel URL, shortcode, or numeric media id")
    if _MEDIA_ID_RE.match(t):
        return t, media_id_to_shortcode(t)
    code = None
    if "://" in t or t.startswith("www."):
        host = (urllib.parse.urlparse(t).hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in _IG_HOSTS):
            raise ReachError(BAD_INPUT, f"not an Instagram URL: {t!r}")
        m = re.search(r"/(p|reel|reels|tv)/([A-Za-z0-9_-]+)", urllib.parse.urlparse(t).path)
        if m:
            code = m.group(2)
    elif _SHORTCODE_RE.match(t):
        code = t
    if not code:
        raise ReachError(BAD_INPUT, f"no post/reel shortcode in: {t!r}")
    return str(shortcode_to_media_id(code)), code


# GraphQL send — same fire-and-poll shape as _facebook_write's _FB_SEND_TEMPLATE,
# with the actor id coming from PolarisViewer.data.fbid instead of
# CurrentUserInitialData.USER_ID (which is "0" under Instagram's web bundle).
_SEND_JS = r"""
(()=>{try{
const req=window.require;
if(!req)return{error:"no_require"};
const viewer=req("PolarisViewer");
const av=viewer?.data?.fbid;
const gp=req("getAsyncParams");
if(!av||!gp)return{error:"no_tokens",hasViewer:!!av};
const base=gp("POST")||{};
if(!base.fb_dtsg||!base.lsd)return{error:"no_tokens",hasViewer:true};
const vars=__IG_VARS;
const p=new URLSearchParams();
for(const k in base)p.set(k,base[k]);
p.set("av",av);
p.set("fb_api_req_friendly_name",__IG_FRIENDLY);
p.set("fb_api_caller_class","RelayModern");
p.set("doc_id",__IG_DOC);
p.set("server_timestamps","true");
p.set("variables",JSON.stringify(vars));
const KEY=__IG_KEY;
if(window[KEY+"_sent"])return{sent:true,dup:true};
window[KEY+"_sent"]=true;
window[KEY]=null;
fetch("/api/graphql",{method:"POST",credentials:"include",
headers:{"Content-Type":"application/x-www-form-urlencoded"},
body:p.toString()}).then(r=>r.text().then(t=>{
const out={status:r.status,body:t.slice(0,3000)};
let j=null;try{j=JSON.parse(t)}catch(e){}
if(j&&typeof j==="object"){
out.parsed=true;
out.ok=!!(j.data&&!j.errors);
out.errors=(j.errors||[]).map(e=>e.message||String(e)).slice(0,3);
out.extensions=j.extensions?Object.keys(j.extensions):null;
const cc=j.data?.xig_comment_create;
if(cc)out.commentId=cc.comment?.pk?String(cc.comment.pk):null;
const ml=j.data?.xig_media_like;
if(ml)out.liked=ml.media?.has_liked===true;
const ul=j.data?.xig_media_unlike;
if(ul)out.unliked=ul.media?.has_liked===false;
}else{out.parsed=false}
window[KEY]=out;
})).catch(e=>{window[KEY]={error:String(e)}});
return{sent:true}}catch(e){return{error:String(e)}}})()
"""


def prepare_instagram() -> None:
    """Warm the dedicated browser session for instagram.com (all write verbs)."""
    from agent_reach.runtime import session_reuse

    session_reuse.prepare("instagram")


def _is_ig_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    return any(host == h or host.endswith("." + h) for h in _IG_HOSTS)


def _ig_target(create: bool):
    """An instagram.com page target in the dedicated browser (create one if absent)."""
    from agent_reach.runtime import cdp

    for t in cdp._json("/json/list") or []:
        if t.get("type") == "page" and _is_ig_url(t.get("url", "")):
            return t
    if not create:
        return None
    made = cdp.call("Target.createTarget", {"url": "https://www.instagram.com/"})
    tid = (made or {}).get("targetId")
    if not tid:
        return None
    for t in cdp._json("/json/list") or []:
        if t.get("id") == tid:
            return t
    return {"id": tid, "type": "page", "url": "https://www.instagram.com/"}


def _wait_ready(target_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    expr = (
        "(document.readyState==='interactive'||document.readyState==='complete')"
        "&&/(^|\\.)instagram\\.com$/.test(location.hostname)"
    )
    while time.monotonic() < deadline:
        if _eval(target_id, expr, 8):
            return
        time.sleep(0.8)
    raise ReachError(UPSTREAM_BROKEN, "instagram.com did not finish loading in the dedicated browser")


def require_session(cookies: list[dict]) -> tuple[str, str]:
    """`(csrf, ds_user_id)` or NEED_LOGIN when the jar has no `sessionid`."""
    jar = {c.get("name"): str(c.get("value") or "") for c in cookies or []}
    if not jar.get("sessionid") or not jar.get("ds_user_id"):
        raise ReachError(
            NEED_LOGIN,
            "instagram.com is not logged in — run `agent-reach login instagram` "
            "and let the operator finish the account picker/password in the Agent Reach browser",
        )
    csrf = jar.get("csrftoken")
    if not csrf:
        raise ReachError(NEED_LOGIN, "instagram.com session has no csrftoken — log in again")
    return csrf, jar["ds_user_id"]


def _session(target_id: str, deadline: float) -> tuple[str, str]:
    from agent_reach.runtime import cdp

    return require_session(cdp.cookies("instagram.com"))


def classify_ig(out: dict, *, confirmed: Callable[[], bool]) -> None:
    """Map a page-side `/api/graphql` result onto Reach errors."""
    status = out.get("status")
    if status == 429:
        raise ReachError(RATE_LIMITED, "Instagram rate-limited the request — slow down before retrying")
    if status == 401 or status == 403:
        raise ReachError(NEED_LOGIN, "Instagram rejected the session — log in again in the Agent Reach browser")
    if isinstance(status, int) and status >= 500:
        raise ReachError(UPSTREAM_BROKEN, f"Instagram returned HTTP {status}")
    if not out.get("parsed"):
        head = (out.get("body") or "").lower()
        if "not-logged-in" in head or "login" in head or "log in" in head:
            raise ReachError(NEED_LOGIN, "Instagram rejected the session — log in again in the Agent Reach browser")
        raise ReachError(UPSTREAM_BROKEN, f"Unparseable Instagram response (HTTP {status}): {head[:200]}")
    if confirmed():
        return
    errs = out.get("errors") or []
    code = str(out.get("code") or "")
    msg = str(out.get("message") or "")
    text = " ".join(errs + [code, msg] + ([out.get("body", "")][:400] if out.get("body") else []))
    lowered = str(text).lower()
    if status == 404 or "doesn't exist" in lowered or "media not found" in lowered:
        raise ReachError(BAD_INPUT, f"Instagram media not found or target invalid: {text[:200]}")
    if "not found" in lowered and ("document" in lowered or "query" in lowered):
        raise ReachError(
            PERSISTED_QUERY_DEAD,
            f"Instagram persisted query id is dead — re-capture doc_id: {errs[0] if errs else text[:120]}",
        )
    if "require_login" in lowered or "user not logged in" in lowered:
        raise ReachError(NEED_LOGIN, "Instagram rejected the session — log in again in the Agent Reach browser")
    if any(w in lowered for w in _BLOCK_WORDS):
        raise ReachError(
            PLATFORM_BLOCKED,
            f"Instagram checkpoint/verification wall: {text[:200]} — resolve it in the browser before retrying",
        )
    if any(w in lowered for w in _RATE_WORDS):
        raise ReachError(RATE_LIMITED, f"Instagram rate-limited the request: {text[:200]}")
    raise ReachError(UPSTREAM_BROKEN, f"Instagram rejected the request: {text[:300]}")


def _arm(timeout: float) -> tuple[float, str]:
    prepare_instagram()
    deadline = time.monotonic() + timeout
    target = _ig_target(create=True)
    if not target:
        raise ReachError(UPSTREAM_BROKEN, "No instagram.com tab could be opened in the dedicated browser")
    _wait_ready(target["id"], max(1.0, min(deadline - time.monotonic(), 25)))
    return deadline, target["id"]


def _left(deadline: float, cap: float) -> float:
    return max(1.0, min(deadline - time.monotonic(), cap))


def _send_js(vars_obj: dict, friendly_name: str, doc_id: str, key: str) -> str:
    return (
        _SEND_JS.replace("__IG_KEY", json.dumps(key))
        .replace("__IG_VARS", json.dumps(vars_obj))
        .replace("__IG_FRIENDLY", json.dumps(friendly_name))
        .replace("__IG_DOC", json.dumps(doc_id))
    )


def _send(target_id: str, expr: str, *, deadline: float, key: str, label: str) -> dict:
    sent = _eval(target_id, expr, _left(deadline, 10))
    if not isinstance(sent, dict) or not sent.get("sent"):
        err = (sent or {}).get("error") if isinstance(sent, dict) else sent
        raise ReachError(UPSTREAM_BROKEN, f"{label} could not be sent: {err!r}")
    poll = f"window[{json.dumps(key)}]||null"
    out = None
    while time.monotonic() < deadline:
        out = _eval(target_id, poll, _left(deadline, 8))
        if out:
            break
        time.sleep(0.7)
    if not isinstance(out, dict):
        raise ReachError(
            UPSTREAM_BROKEN,
            f"{label} sent but no result returned — check instagram.com before retrying to avoid duplicates",
        )
    if out.get("error"):
        raise ReachError(UPSTREAM_BROKEN, f"{label} failed in page: {str(out['error'])[:200]}")
    return out


def _doc_id(kind: str) -> str:
    """Persisted-query id for `kind` — query-ids.json overrides the baked default."""
    defaults = {"comment": _DOC_COMMENT, "like": _DOC_LIKE, "unlike": _DOC_UNLIKE}
    return query_ids.get("instagram", kind, defaults[kind])


def ig_comment(*, target: str, text: str, timeout: float) -> list:
    """Comment on a post/reel (`usePolarisCommentCreateMutation`)."""
    mid, code = resolve_media_id(target)
    body_text = (text or "").strip()
    if not body_text:
        raise ReachError(BAD_INPUT, "comment text is empty")
    deadline, tid = _arm(timeout)
    _, user_id = _session(tid, deadline)
    vars_obj = {
        "data": {"comment_text": body_text, "media_id": mid},
        "root_connections": [
            f'client:POLARIS_{mid}:__PolarisPostComments__comments_connection_connection(input:{{"sort_order":"popular"}})'
        ],
    }
    key = _result_key("__igCommentResult")
    out = _send(
        tid,
        _send_js(vars_obj, "usePolarisCommentCreateMutation", _doc_id("comment"), key),
        deadline=deadline,
        key=key,
        label="Comment",
    )
    classify_ig(out, confirmed=lambda: out.get("ok") and bool(out.get("commentId")))
    return [
        make_item(
            title=body_text[:120],
            url=(
                f"https://www.instagram.com/p/{code}/#c{out.get('commentId')}"
                if out.get("commentId")
                else f"https://www.instagram.com/p/{code}/"
            ),
            author=user_id,
            text="Commented",
            raw={
                "status": out.get("status"),
                "media_id": mid,
                "shortcode": code,
                "comment_id": out.get("commentId"),
            },
        )
    ]


def ig_like(*, target: str, unlike: bool, timeout: float) -> list:
    """Like (`usePolarisLikeMediaXIGLikeMutation`) or unlike the same target."""
    mid, code = resolve_media_id(target)
    deadline, tid = _arm(timeout)
    _, user_id = _session(tid, deadline)
    vars_obj = {
        "input": {
            "actor_id": "__AV__",  # filled by PolarisViewer.data.fbid inside the send JS
            "client_mutation_id": "1",
            "media_id": mid,
        }
    }
    key = _result_key("__igLikeResult")
    send_expr = (
        _SEND_JS.replace("__IG_KEY", json.dumps(key))
        .replace("__IG_VARS", json.dumps(vars_obj).replace('"__AV__"', '" + av + "'))
        .replace("__IG_FRIENDLY", json.dumps("usePolarisLikeMediaXIGUnlikeMutation" if unlike else "usePolarisLikeMediaXIGLikeMutation"))
        .replace("__IG_DOC", json.dumps(_doc_id("unlike" if unlike else "like")))
    )
    out = _send(tid, send_expr, deadline=deadline, key=key, label="Unlike" if unlike else "Like")
    want = not unlike
    classify_ig(out, confirmed=lambda: out.get("ok") and out.get("liked" if want else "unliked"))
    return [
        make_item(
            title=f"https://www.instagram.com/p/{code}/",
            url=f"https://www.instagram.com/p/{code}/",
            author=user_id,
            text="Unliked" if unlike else "Liked",
            raw={"status": out.get("status"), "media_id": mid, "shortcode": code},
        )
    ]

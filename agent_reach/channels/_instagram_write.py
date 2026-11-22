# -*- coding: utf-8 -*-
"""Instagram write actions (comment / like / unlike) in the Agent Reach browser.

Reads go through `opencli instagram`; writes replay Instagram's web form API
inside a logged-in instagram.com page: `POST /api/v1/web/comments/<media_id>/add/`
and `POST /api/v1/web/likes/<media_id>/like|unlike/`. Auth is the session cookie
(`credentials:'include'`); CSRF is the `csrftoken` cookie echoed in
`X-CSRFToken`. A numeric `media_id` or a `/p/`, `/reel/` or `/tv/` URL (or bare
shortcode) all resolve through the deterministic shortcode↔media_id base64
alphabet — no lookup request needed.

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

from agent_reach.runtime.result import (
    BAD_INPUT,
    NEED_LOGIN,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
)

from ._facebook_write import _eval, _result_key

_IG_HOSTS = ("instagram.com", "instagr.am")
_APP_ID = "936619743392459"  # web app id IG itself sends on every XHR
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,20}$")
_MEDIA_ID_RE = re.compile(r"^\d{8,25}$")

# IG JSON `message`/`feedback` text that is the caller's problem vs a soft block.
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


_SEND_JS = r"""
(()=>{try{
const KEY=__IG_KEY;
if(window[KEY+"_sent"])return{sent:true,dup:true};
window[KEY+"_sent"]=true;
window[KEY]=null;
fetch(__IG_PATH,{method:"POST",credentials:"include",
headers:{"Content-Type":"application/x-www-form-urlencoded","X-CSRFToken":__IG_CSRF,
"X-Requested-With":"XMLHttpRequest","X-IG-App-ID":__IG_APPID},
body:__IG_BODY}).then(r=>r.text().then(t=>{
const out={status:r.status,body:t.slice(0,2000)};
let j=null;try{j=JSON.parse(t)}catch(e){}
if(j&&typeof j==="object"){
out.parsed=true;
out.ok=j.status==="ok";
const c=j.comment||{};
out.commentId=c.pk?String(c.pk):null;
out.message=j.message||null;
out.feedback=(j.feedback_title||j.feedback_message||"")+"";
out.code=(j.error_type||j.checkpoint_error_type||"")+""+
["feedback_required","spam","checkpoint_required","challenge_required","require_login"]
.filter(k=>j[k]===true||j[k]===k).map(k=>" "+k).join("");
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
    """Map a page-side `/api/v1/web/*` result onto Reach errors."""
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
    text = " ".join(
        str(x or "")
        for x in (
            out.get("message"),
            out.get("feedback"),
            out.get("code"),
            out.get("body")[:400] if out.get("body") else "",
        )
    )
    lowered = text.lower()
    if "require_login" in lowered or "user not logged in" in lowered:
        raise ReachError(NEED_LOGIN, "Instagram rejected the session — log in again in the Agent Reach browser")
    if any(w in lowered for w in _BLOCK_WORDS):
        raise ReachError(
            PLATFORM_BLOCKED,
            f"Instagram checkpoint/verification wall: {text[:200]} — resolve it in the browser before retrying",
        )
    if any(w in lowered for w in _RATE_WORDS):
        raise ReachError(RATE_LIMITED, f"Instagram rate-limited the request: {text[:200]}")
    if status == 404 or "not found" in lowered or "doesn't exist" in lowered:
        raise ReachError(BAD_INPUT, f"Instagram media not found or target invalid: {text[:200]}")
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


def _send_js(path: str, csrf: str, body: str, key: str) -> str:
    return (
        _SEND_JS.replace("__IG_KEY", json.dumps(key))
        .replace("__IG_PATH", json.dumps(path))
        .replace("__IG_CSRF", json.dumps(csrf))
        .replace("__IG_APPID", json.dumps(_APP_ID))
        .replace("__IG_BODY", json.dumps(body))
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


def ig_comment(*, target: str, text: str, timeout: float) -> list:
    """Comment on a post/reel (`/api/v1/web/comments/<media_id>/add/`)."""
    mid, code = resolve_media_id(target)
    body_text = (text or "").strip()
    if not body_text:
        raise ReachError(BAD_INPUT, "comment text is empty")
    deadline, tid = _arm(timeout)
    csrf, user_id = _session(tid, deadline)
    body = urllib.parse.urlencode({"comment_text": body_text})
    key = _result_key("__igCommentResult")
    out = _send(
        tid,
        _send_js(f"/api/v1/web/comments/{mid}/add/", csrf, body, key),
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
    """Like (`/api/v1/web/likes/<media_id>/like/`) or unlike the same path's `/unlike/`."""
    mid, code = resolve_media_id(target)
    deadline, tid = _arm(timeout)
    csrf, user_id = _session(tid, deadline)
    verb = "unlike" if unlike else "like"
    key = _result_key("__igLikeResult")
    out = _send(
        tid,
        _send_js(f"/api/v1/web/likes/{mid}/{verb}/", csrf, "", key),
        deadline=deadline,
        key=key,
        label=verb.capitalize(),
    )
    classify_ig(out, confirmed=lambda: out.get("ok"))
    return [
        make_item(
            title=f"https://www.instagram.com/p/{code}/",
            url=f"https://www.instagram.com/p/{code}/",
            author=user_id,
            text="Unliked" if unlike else "Liked",
            raw={"status": out.get("status"), "media_id": mid, "shortcode": code},
        )
    ]

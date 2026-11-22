# -*- coding: utf-8 -*-
"""Threads write lane — REST replay in the dedicated browser.

Story 15.10 (spike 15.9 verdict REPLAYABLE): a Threads text post is
``POST /api/v1/media/configure_text_only_post/`` (urlencoded, session
cookies) — not GraphQL, so there is no persisted-query id and no
PERSISTED_QUERY_DEAD mapping for this verb. The Relay mutation surface
(useBarcelona* doc-ids) is only needed for delete/reply metadata; kept
descoped here.

Envelope mint: ``window.require("getAsyncParams")("POST")`` mints the full
base param set fresh each call (``av`` falls back to
``require("RelayAPIConfig").actorID`` — NOT ``CurrentUserInitialData``,
which is "0" on Meta surfaces). ``web_session_id`` accepts a fresh ``__s``
value verbatim (same ``prefix:session:nonce`` format, live-verified) and is
also sent as ``x-web-session-id``. ``x-bloks-version-id`` is NOT required —
a bare param-complete replay posts successfully without it.

Session: threads.com has its own sessionid/ds_user_id/csrftoken (Meta
"continue as" flow, not shared with instagram.com cookies).
"""

from __future__ import annotations

import json
import time
import urllib.parse
import uuid

from agent_reach.runtime.result import (
    BAD_INPUT,
    NEED_LOGIN,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
)
from ._facebook_write import _eval, _result_key

_HOME = "https://www.threads.com/"
_POST_PATH = "/api/v1/media/configure_text_only_post/"
_ASBD_ID = "129477"
_IG_APP_ID = "238260118697367"
_MAX_CAPTION = 500


def prepare_threads() -> None:
    """Warm the dedicated browser session for threads.com."""
    from agent_reach.runtime import session_reuse

    session_reuse.prepare("threads")


def _is_threads_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    return host == "threads.com" or host.endswith(".threads.com") or host == "threads.net" or host.endswith(".threads.net")


def _threads_target(create: bool):
    from agent_reach.runtime import cdp

    targets = cdp._json("/json/list") or []
    for t in targets:
        if t.get("type") == "page" and _is_threads_url(t.get("url", "")):
            return t
    if not create:
        return None
    made = cdp.call("Target.createTarget", {"url": _HOME})
    tid = (made or {}).get("targetId")
    if not tid:
        return None
    for t in cdp._json("/json/list") or []:
        if t.get("id") == tid:
            return t
    return {"id": tid, "type": "page", "url": _HOME}


def _wait_ready(target_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    expr = (
        "(document.readyState==='interactive'||document.readyState==='complete')"
        "&&/(^|\\.)threads\\.(com|net)$/.test(location.hostname)"
    )
    while time.monotonic() < deadline:
        if _eval(target_id, expr, 8):
            return
        time.sleep(0.8)
    raise ReachError(UPSTREAM_BROKEN, "threads.com did not finish loading in the dedicated browser")


def _arm(timeout: float) -> tuple[float, str]:
    prepare_threads()
    deadline = time.monotonic() + timeout
    target = _threads_target(create=True)
    if not target:
        raise ReachError(UPSTREAM_BROKEN, "No threads.com tab could be opened in the dedicated browser")
    _wait_ready(target["id"], max(1.0, min(deadline - time.monotonic(), 25)))
    from agent_reach.runtime import behavior

    behavior.humanize("threads", target["id"])
    # Sim time must not eat the send's deadline (see _facebook_write).
    return time.monotonic() + timeout, target["id"]


def _left(deadline: float, cap: float) -> float:
    return max(1.0, min(deadline - time.monotonic(), cap))


def _session(target_id: str, deadline: float) -> str:
    """csrftoken for the threads.com session; NEED_LOGIN when absent."""
    from agent_reach.runtime import cdp

    raw = cdp.cookies("threads.com") or cdp.cookies("threads.net")
    cookies = {c.get("name"): c.get("value") for c in (raw or [])}
    if not cookies.get("sessionid") or not cookies.get("ds_user_id"):
        raise ReachError(
            NEED_LOGIN,
            "Not logged into threads.com in the dedicated browser — run `agent-reach login threads`",
        )
    return cookies.get("csrftoken") or ""


# Mint the Relay envelope + actor id fresh from the page. getAsyncParams is
# the only reliable source — a static params template goes stale on deploy.
_MINT_JS = """(()=>{const req=window.require; if(!req) return {error:"no_require"};
let actor=null;
try{const c=req('RelayAPIConfig');actor=c.actorID||c.actorId||null}catch(e){}
if(!actor){try{const u=req('CurrentUser');actor=u.getID?u.getID():null}catch(e){}}
try{return {params:req('getAsyncParams')('POST'),actor}}catch(e){return {error:String(e)}}})()"""


# Fire-and-poll: register the fetch, return sent:true, the result lands in
# window[key]. The __sent flag stops an eval retry from refiring the POST.
_SEND_JS = """(()=>{const K=__T_KEY,S=K+'_sent';
if(window[S])return {sent:true,dup:true};
window[S]=1; window[K]=null;
try{
fetch(__T_PATH,{method:'POST',credentials:'include',headers:__T_HEADERS,
body:new URLSearchParams(__T_BODY).toString()})
.then(async r=>{let j=null;try{j=await r.json()}catch(e){}
window[K]={sent:true,status:r.status,body:j}})
.catch(e=>{window[K]={sent:true,error:String(e)}});
return {sent:true};
}catch(e){window[K]={sent:true,error:String(e)};return {sent:true,error:String(e)}}})()"""


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
            f"{label} sent but no result returned — check threads.com before retrying to avoid duplicates",
        )
    if out.get("error"):
        raise ReachError(UPSTREAM_BROKEN, f"{label} failed in page: {str(out['error'])[:200]}")
    return out


_RATE_WORDS = ("rate limit", "please wait", "too many", "try again later", "temporarily")
_LOGIN_WORDS = ("not logged", "login", "unauthorized", "auth_required", "logged out")
_BLOCK_WORDS = ("checkpoint", "suspended", "action_blocked", "action blocked", "spam")


def classify_threads(out: dict, *, confirmed) -> dict:
    """Map the configure_text_only_post result. REST errors — no
    PERSISTED_QUERY_DEAD here (spike 15.9: create is REST, not persisted)."""
    if not isinstance(out, dict):
        raise ReachError(UPSTREAM_BROKEN, "threads post returned no parseable result")
    if confirmed():
        return out
    status = out.get("status")
    body = out.get("body")
    text = " ".join(
        str(x).lower()
        for x in (
            out.get("error"),
            body.get("message") if isinstance(body, dict) else body,
            body.get("error") if isinstance(body, dict) else None,
            body.get("checkpoint_url") if isinstance(body, dict) else None,
        )
        if x is not None
    )
    if any(w in text for w in _BLOCK_WORDS):
        raise ReachError(
            PLATFORM_BLOCKED,
            "Threads account is checkpointed/limited — resolve the notice on the account, then retry",
        )
    if status in (401, 403) or any(w in text for w in _LOGIN_WORDS):
        raise ReachError(NEED_LOGIN, "Threads session rejected the write — login may have expired")
    if status == 429 or any(w in text for w in _RATE_WORDS):
        raise ReachError(RATE_LIMITED, "Threads rate-limited the post — slow down")
    if status == 404 or "not found" in text:
        raise ReachError(BAD_INPUT, f"Threads post rejected ({status}): {text[:150]}")
    if status and status >= 400:
        raise ReachError(UPSTREAM_BROKEN, f"Threads post failed with HTTP {status}: {text[:150]}")
    raise ReachError(UPSTREAM_BROKEN, f"Threads post failed: {text[:180] or 'no error detail'}")


def _confirmed(body) -> bool:
    if not isinstance(body, dict):
        return False
    media = body.get("media") or {}
    return bool(
        media.get("id") or media.get("pk") or body.get("media_id") or body.get("id")
    )


def _permalink(body) -> str | None:
    if not isinstance(body, dict):
        return None
    media = body.get("media") or {}
    for cand in (media.get("permalink"), body.get("permalink")):
        if cand:
            return cand
    code = media.get("code") or body.get("code")
    return f"https://www.threads.com/_/post/{code}" if code else None


def threads_post(*, text: str, timeout: float) -> list:
    """Publish a text-only Threads post via configure_text_only_post replay."""
    caption = (text or "").strip()
    if not caption:
        raise ReachError(BAD_INPUT, "post text is empty")
    if len(caption) > _MAX_CAPTION:
        raise ReachError(BAD_INPUT, f"post text is over {_MAX_CAPTION} characters")
    deadline, tid = _arm(timeout)
    csrf = _session(tid, deadline)

    minted = _eval(tid, _MINT_JS, _left(deadline, 10))
    if not isinstance(minted, dict) or "params" not in minted:
        raise ReachError(
            NEED_LOGIN if (minted or {}).get("error") in ("no_require", None) else UPSTREAM_BROKEN,
            f"Could not mint the Threads envelope (no session?): {(minted or {}).get('error')!r}",
        )
    params = dict(minted["params"])
    if minted.get("actor") and not params.get("av"):
        params["av"] = str(minted["actor"])
    web_session = params.get("__s") or ""

    body = dict(params)
    body.update(
        {
            # Param set live-captured from the real composer (story 15.10).
            "audience": "default",
            "caption": caption,
            "container_module": "ig_text_feed_timeline",
            "creator_geo_gating_info": '{"whitelist_country_codes":[]}',
            "is_upload_type_override_allowed": "1",
            "music_failures_supported": "1",
            "nav_chain": "BarcelonaHomeRoot:ig_text_feed_timeline:2:unexpected",
            "publish_mode": "text_post",
            "should_include_permalink": "true",
            "upload_id": str(int(time.time() * 1000)),
            "web_session_id": web_session,
            "text_post_app_info": json.dumps(
                {
                    "community_flair_id": None,
                    "composer_session_id": str(uuid.uuid4()),
                    "entry_point": "main_tab_bar",
                    "excluded_inline_media_ids": "[]",
                    "fediverse_composer_enabled": True,
                    "is_genai_invocation_post": False,
                    "is_reply_approval_enabled": False,
                    "is_spoiler_media": False,
                    "link_attachment_url": None,
                    "link_preview_default_render_style": None,
                    "quoted_post_id": None,
                    "reply_control": 0,
                    "self_thread_context_id": str(uuid.uuid4()),
                    "snippet_attachment": None,
                    "special_effects_enabled_str": None,
                    "tag_header": None,
                    "text_with_entities": {"entities": [], "text": caption},
                    "threads_draft_id": None,
                },
                separators=(",", ":"),
            ),
        }
    )

    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "x-ig-app-id": _IG_APP_ID,
        "x-csrftoken": csrf,
        "x-web-session-id": web_session,
        "x-asbd-id": _ASBD_ID,
        "x-instagram-ajax": "0",
    }
    key = _result_key("__threadsPost")
    expr = (
        _SEND_JS.replace("__T_KEY", json.dumps(key))
        .replace("__T_PATH", json.dumps(_POST_PATH))
        .replace("__T_HEADERS", json.dumps(headers))
        .replace("__T_BODY", json.dumps(body))
    )
    out = _send(tid, expr, deadline=deadline, key=key, label="threads post")
    out = classify_threads(out, confirmed=lambda: _confirmed(out.get("body")))
    media = out.get("body") or {}
    mid = (media.get("media") or {}).get("id") or media.get("media_id") or media.get("id")
    return [
        {
            "title": caption[:80],
            "url": _permalink(media),
            "author": None,
            "published_at": None,
            "text": "Posted",
            "raw": {"status": out.get("status"), "media_id": mid, "echo": caption},
        }
    ]

# -*- coding: utf-8 -*-
"""Reddit write actions (post / comment) in the shared Agent Reach browser.

Reads go through `opencli reddit`; writes replay Reddit's legacy form API
inside a logged-in reddit.com page. The CSRF token is the session modhash
from `GET /api/me.json` (fresh per call). Cookies ride `credentials:'include'`
— no OAuth bearer. `POST /api/submit` creates a self or link post;
`POST /api/comment` replies to a thing fullname (`t3_` / `t1_`).

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
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
)

from ._facebook_write import _eval, _result_key

_THING_RE = re.compile(r"^t[1-6]_[A-Za-z0-9]+$")
_REDDIT_HOSTS = ("reddit.com",)

# Reddit form error codes that are the caller's input, not a dead session
# or a broken upstream. RATELIMIT is handled separately.
_BAD_INPUT_CODES = frozenset({
    "SUBREDDIT_NOEXIST", "NO_TEXT", "NO_URL", "NO_TITLE", "BAD_URL",
    "TOO_LONG", "TOO_SHORT", "SUBREDDIT_REQUIRED", "BAD_SR_NAME",
    "INVALID_OPTION", "NO_SELFS", "NO_LINKS", "SUBREDDIT_NOTALLOWED",
})
_RATE_CODES = frozenset({"RATELIMIT", "RATELIMIT_MESSAGE"})
_LOGIN_CODES = frozenset({"USER_REQUIRED"})

_ME_JS = r"""
(async()=>{try{
const r=await fetch("/api/me.json",{credentials:"include",headers:{"Accept":"application/json"}});
const t=await r.text();
let j=null;try{j=JSON.parse(t)}catch(e){}
const d=(j&&j.data)||{};
return{status:r.status,name:d.name||null,modhash:d.modhash||""};
}catch(e){return{error:String(e)}}})()
"""

# __RD_BODY is substituted LAST — the form body is caller text and could
# itself contain one of the other sentinels.
_SEND_JS = r"""
(()=>{try{
const KEY=__RD_KEY;
if(window[KEY+"_sent"])return{sent:true,dup:true};
window[KEY+"_sent"]=true;
window[KEY]=null;
fetch(__RD_PATH,{method:"POST",credentials:"include",
headers:{"Content-Type":"application/x-www-form-urlencoded","X-Modhash":__RD_UH,"X-Requested-With":"XMLHttpRequest"},
body:__RD_BODY}).then(r=>r.text().then(t=>{
const out={status:r.status,body:t.slice(0,2000)};
let j=null;try{j=JSON.parse(t)}catch(e){}
if(j&&j.json){
out.parsed=true;
const blob=j.json;
out.errors=Array.isArray(blob.errors)?blob.errors.slice(0,5):[];
const data=blob.data||{};
out.url=data.url||null;
out.name=data.name||null;
out.id=data.id||null;
const thing=((data.things||[])[0]||{}).data||{};
out.commentName=thing.name||null;
out.commentId=thing.id||null;
out.permalink=thing.permalink||null;
}else{out.parsed=false}
window[KEY]=out;
})).catch(e=>{window[KEY]={error:String(e)}});
return{sent:true}}catch(e){return{error:String(e)}}})()
"""


def prepare_reddit() -> None:
    """Warm the dedicated browser session for reddit.com (all write verbs)."""
    from agent_reach.runtime import session_reuse

    session_reuse.prepare("reddit")


def normalize_subreddit(raw: str) -> str:
    """`r/test` → `test`, `u/name` → `u_name` (the sr Reddit expects)."""
    s = (raw or "").strip().strip("/")
    low = s.lower()
    if low.startswith("r/"):
        s = s[2:]
    elif low.startswith("u/"):
        s = "u_" + s[2:]
    if not s or any(c in s for c in " /"):
        raise ReachError(BAD_INPUT, "subreddit must be a name like test or u_username")
    return s


def build_submit_fields(*, subreddit: str, title: str, text: str | None, url: str | None, uh: str) -> dict:
    """Form fields for `POST /api/submit`. A url makes a link post; otherwise self."""
    sr = normalize_subreddit(subreddit)
    title = (title or "").strip()
    if not title:
        raise ReachError(BAD_INPUT, "title is required")
    link = (url or "").strip()
    body = "" if text is None else text
    # shortcut: url wins as a link post and text is dropped, upgrade if a
    # link-with-body post is ever required.
    if link:
        kind, extra = "link", {"url": link}
    else:
        if not str(body).strip():
            raise ReachError(BAD_INPUT, "text or url is required")
        kind, extra = "self", {"text": body}
    return {
        "api_type": "json",
        "uh": uh,
        "sr": sr,
        "kind": kind,
        "title": title,
        "resubmit": "true",
        **extra,
    }


def build_comment_fields(*, thing_id: str, text: str, uh: str) -> dict:
    """Form fields for `POST /api/comment`."""
    tid = (thing_id or "").strip()
    if not _THING_RE.match(tid):
        raise ReachError(BAD_INPUT, "thing_id must be a fullname like t3_abc")
    body = (text or "").strip()
    if not body:
        raise ReachError(BAD_INPUT, "text is required")
    return {"api_type": "json", "uh": uh, "thing_id": tid, "text": body}


def encode_form(fields: dict) -> str:
    return urllib.parse.urlencode(fields)


def require_session(me: object) -> tuple[str, str]:
    """`(username, modhash)` or NEED_LOGIN when `/api/me.json` has no session."""
    if not isinstance(me, dict) or me.get("error"):
        raise ReachError(
            NEED_LOGIN,
            "reddit.com is not logged in — open https://www.reddit.com/login in the Agent Reach browser",
        )
    name = str(me.get("name") or "").strip()
    uh = str(me.get("modhash") or "").strip()
    if not name or not uh:
        raise ReachError(
            NEED_LOGIN,
            "reddit.com is not logged in (no modhash) — open https://www.reddit.com/login in the Agent Reach browser",
        )
    return name, uh


def _error_parts(errors) -> tuple[set[str], str]:
    codes: list[str] = []
    parts: list[str] = []
    for entry in errors or []:
        if isinstance(entry, (list, tuple)) and entry:
            codes.append(str(entry[0]))
            parts.append(" ".join(str(x) for x in entry[:3]))
        elif isinstance(entry, dict):
            codes.append(str(entry.get("reason") or entry.get("code") or ""))
            parts.append(str(entry.get("message") or entry)[:150])
        else:
            parts.append(str(entry)[:150])
    return {c for c in codes if c}, "; ".join(parts)[:400]


def classify_reddit(out: dict, *, confirmed: Callable[[], bool]) -> None:
    """Map a page-side `/api/*` result onto Reach errors.

    A confirmed signal (post url, comment id) wins over `errors`, matching
    the facebook write contract — Reddit can attach a non-fatal error to a
    payload that still created the thing.
    """
    status = out.get("status")
    if status == 429:
        raise ReachError(RATE_LIMITED, "Reddit rate-limited the request — slow down before retrying")
    if status in (401, 403):
        raise ReachError(NEED_LOGIN, "Reddit rejected the session — log in again in the Agent Reach browser")
    if isinstance(status, int) and status >= 500:
        raise ReachError(UPSTREAM_BROKEN, f"Reddit returned HTTP {status}")
    if not out.get("parsed"):
        head = (out.get("body") or "").lower()
        if any(w in head for w in ("log in", "login", "sign up")):
            raise ReachError(NEED_LOGIN, "Reddit rejected the session — log in again in the Agent Reach browser")
        raise ReachError(UPSTREAM_BROKEN, f"Unparseable Reddit response (HTTP {status}): {head[:200]}")
    if confirmed():
        return
    codes, message = _error_parts(out.get("errors"))
    lowered = message.lower()
    if codes & _RATE_CODES or "you are doing that too much" in lowered:
        raise ReachError(RATE_LIMITED, f"Reddit rate-limited the request: {message}")
    if codes & _LOGIN_CODES or "please log in" in lowered:
        raise ReachError(NEED_LOGIN, f"Reddit session rejected: {message}")
    if codes & _BAD_INPUT_CODES:
        raise ReachError(BAD_INPUT, message or "Reddit rejected the input")
    if out.get("errors"):
        raise ReachError(UPSTREAM_BROKEN, f"Reddit rejected the request: {message}")


def _is_reddit_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    return any(host == s or host.endswith("." + s) for s in _REDDIT_HOSTS)


def _reddit_target(create: bool):
    """A reddit.com page target in the dedicated browser (create one if absent)."""
    from agent_reach.runtime import cdp

    targets = cdp._json("/json/list") or []
    for t in targets:
        if t.get("type") == "page" and _is_reddit_url(t.get("url", "")):
            return t
    if not create:
        return None
    made = cdp.call("Target.createTarget", {"url": "https://www.reddit.com/"})
    tid = (made or {}).get("targetId")
    if not tid:
        return None
    for t in cdp._json("/json/list") or []:
        if t.get("id") == tid:
            return t
    return {"id": tid, "type": "page", "url": "https://www.reddit.com/"}


def _wait_ready(target_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    expr = (
        "(document.readyState==='interactive'||document.readyState==='complete')"
        "&&/(^|\\.)reddit\\.com$/.test(location.hostname)"
    )
    while time.monotonic() < deadline:
        if _eval(target_id, expr, 8):
            return
        time.sleep(0.8)
    raise ReachError(UPSTREAM_BROKEN, "reddit.com did not finish loading in the dedicated browser")


def _send_js(path: str, uh: str, body: str, key: str) -> str:
    return (
        _SEND_JS.replace("__RD_KEY", json.dumps(key))
        .replace("__RD_PATH", json.dumps(path))
        .replace("__RD_UH", json.dumps(uh))
        .replace("__RD_BODY", json.dumps(body))
    )


def _arm(timeout: float) -> tuple[float, str]:
    prepare_reddit()
    deadline = time.monotonic() + timeout
    target = _reddit_target(create=True)
    if not target:
        raise ReachError(UPSTREAM_BROKEN, "No reddit.com tab could be opened in the dedicated browser")
    _wait_ready(target["id"], max(1.0, min(deadline - time.monotonic(), 25)))
    return deadline, target["id"]


def _left(deadline: float, cap: float) -> float:
    return max(1.0, min(deadline - time.monotonic(), cap))


def _session(target_id: str, deadline: float) -> tuple[str, str]:
    me = _eval(target_id, _ME_JS, _left(deadline, 15))
    return require_session(me)


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
            f"{label} sent but no result returned — check reddit.com before retrying to avoid duplicates",
        )
    if out.get("error"):
        raise ReachError(UPSTREAM_BROKEN, f"{label} failed in page: {str(out['error'])[:200]}")
    return out


def _clean_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.endswith(".json"):
        url = url[:-5]
    if url.startswith("/"):
        return "https://www.reddit.com" + url
    return url


def reddit_post(*, subreddit: str, title: str, text: str | None = None, url: str | None = None, timeout: float) -> list:
    """Post a text or link submission. `subreddit` may be `test`, `r/test`, or `u/name`."""
    fields = build_submit_fields(subreddit=subreddit, title=title, text=text, url=url, uh="pending")
    deadline, tid = _arm(timeout)
    name, uh = _session(tid, deadline)
    fields["uh"] = uh
    key = _result_key("__rdPostResult")
    out = _send(
        tid,
        _send_js("/api/submit", uh, encode_form(fields), key),
        deadline=deadline,
        key=key,
        label="Post",
    )
    classify_reddit(out, confirmed=lambda: bool(out.get("url")))
    post_url = _clean_url(out.get("url"))
    if not post_url:
        raise ReachError(UPSTREAM_BROKEN, "Submit returned no url — check reddit.com before retrying")
    return [
        make_item(
            title=fields["title"][:120],
            url=post_url,
            author=name,
            text="Posted",
            raw={
                "status": out.get("status"),
                "name": out.get("name"),
                "id": out.get("id"),
                "subreddit": fields["sr"],
                "kind": fields["kind"],
                "url": post_url,
            },
        )
    ]


def reddit_comment(*, thing_id: str, text: str, timeout: float) -> list:
    """Comment on a thing fullname (`t3_` post or `t1_` comment)."""
    fields = build_comment_fields(thing_id=thing_id, text=text, uh="pending")
    deadline, tid = _arm(timeout)
    name, uh = _session(tid, deadline)
    fields["uh"] = uh
    key = _result_key("__rdCommentResult")
    out = _send(
        tid,
        _send_js("/api/comment", uh, encode_form(fields), key),
        deadline=deadline,
        key=key,
        label="Comment",
    )
    classify_reddit(out, confirmed=lambda: bool(out.get("commentName") or out.get("commentId")))
    fullname = out.get("commentName") or (f"t1_{out['commentId']}" if out.get("commentId") else None)
    if not fullname:
        raise ReachError(UPSTREAM_BROKEN, "Comment returned no id — check reddit.com before retrying")
    return [
        make_item(
            title=fields["text"][:120],
            url=_clean_url(out.get("permalink")),
            author=name,
            text="Commented",
            raw={
                "status": out.get("status"),
                "name": fullname,
                "id": out.get("commentId"),
                "thing_id": fields["thing_id"],
                "permalink": out.get("permalink"),
            },
        )
    ]


def reddit_delete(*, thing_id: str, timeout: float) -> list:
    """Delete a thing the session owns (`POST /api/del`). Cleanup helper, not a channel action."""
    tid_name = (thing_id or "").strip()
    if not _THING_RE.match(tid_name):
        raise ReachError(BAD_INPUT, "thing_id must be a fullname like t3_abc")
    deadline, tid = _arm(timeout)
    _name, uh = _session(tid, deadline)
    fields = {"api_type": "json", "uh": uh, "id": tid_name}
    key = _result_key("__rdDeleteResult")
    out = _send(
        tid,
        _send_js("/api/del", uh, encode_form(fields), key),
        deadline=deadline,
        key=key,
        label="Delete",
    )
    # `/api/del` sometimes answers `{}` with no json envelope. That is success.
    body = (out.get("body") or "").strip()
    if out.get("status") == 200 and body in ("{}", ""):
        return [make_item(title=tid_name, text="Deleted", raw={"id": tid_name, "status": 200})]
    classify_reddit(out, confirmed=lambda: out.get("parsed") and not out.get("errors"))
    if out.get("errors"):
        raise ReachError(UPSTREAM_BROKEN, "Delete returned errors — check reddit.com")
    return [make_item(title=tid_name, text="Deleted", raw={"id": tid_name, "status": out.get("status")})]

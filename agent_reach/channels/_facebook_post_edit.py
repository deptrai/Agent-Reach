# -*- coding: utf-8 -*-
"""Facebook edit / delete of the caller's OWN posts in the dedicated browser.

Replays Facebook's own GraphQL `ComposerStoryEditMutation` and
`useCometFeedStoryDeleteMutation` inside a logged-in facebook.com page —
the same fire-and-poll pipeline as `_facebook_comment.py` / `_facebook_write.py`.

The mutations were captured once from the real composer UI via
scripts/capture_fb_post_edit.py and kept sanitized in `_fb_post_edit_vars.json`
(per-kind `params` + `variables` + `doc_id`; actor/uid -> "0", uuids ->
zero-uuid, epochs -> 0). `doc_id` is a persisted-query id that rotates with FB
builds — re-capture if edits/deletes start failing with "document not found".

The target is a post permalink (`/<uid>/posts/<id>` or `permalink.php?story_fbid=`)
or a raw story id (the base64 Relay id). The story id is resolved LIVE from the
loaded page (`"storyID"` in the Relay payload, filtered by the permalink's post
id) so ids are never constructed arithmetically; the resolved owner uid must
match the logged-in user or the action fails UPSTREAM_BROKEN (never edit/delete
someone else's post). Edit additionally resolves the post's CURRENT audience
privacy object ({allow, base_state, deny, tag_expansion_state}) from the page
so the edit never silently changes who can see the post — including
exceptions like "Friends except X".
"""

from __future__ import annotations

import base64
import json
import re
import time

from agent_reach.runtime import query_ids
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
    fb_send_js,
    fb_write_target,
    load_skeleton_sections,
    send_mutation,
)

# Persisted-query ids for the two mutations (rotates with FB builds).
# Fallback defaults — query-ids.json overrides them at send time (hot reload).
_DOC = {
    "edit": ("ComposerStoryEditMutation", "30241673288768290"),
    "delete": ("useCometFeedStoryDeleteMutation", "29557927263819997"),
}

_VARS_DOC = load_skeleton_sections("_fb_post_edit_vars.json")
_VARS = {}
for _kind in ("edit", "delete"):
    _entry = _VARS_DOC["sections"].get(_kind) or {}
    _VARS[_kind] = _entry.get("variables") or {}

# base64 OR base64url alphabet (+ optional '=' padding), like comment ids.
_STORY_ID_RE = re.compile(r"^[A-Za-z0-9+/=_-]{20,400}$")

# Live-resolve the target post's Relay story id. __FB_PID is the numeric post id
# from the permalink. The page embeds story ids as base64 "storyID" strings whose
# decoded form is "S:_I<uid>:<fbid>:<story_id>"; we keep only those whose decoded
# segments contain the requested post id, then require exactly one distinct id.
_STORY_LOCATE_JS = r"""
(()=>{try{
const pid=__FB_PID;
const h=document.documentElement.innerHTML;
const re=/"storyID":"([A-Za-z0-9+/=_-]{20,400})"/g;
const found={};
let m;
while((m=re.exec(h))){
const v=m[1];
const raw=v.replace(/=+$/,"");
let d="";
try{d=atob((raw+"=".repeat((4-raw.length%4)%4)).replace(/-/g,"+").replace(/_/g,"/"));}catch(e){continue}
const parts=d.split(":");
if(parts.indexOf(pid)<0)continue;
// Dedupe on the DECODED id — padded and unpadded base64 of the same story
// decode identically and must count as one match, not two.
if(found[d]!==undefined)continue;
let uid=null;
for(const p of parts){const mm=/^_?I([0-9]{6,})$/.exec(p);if(mm){uid=mm[1];break}}
found[d]={v:v,uid:uid||""};
}
const vals=Object.values(found);
const ids=vals.map(o=>o.v);
const uids=[...new Set(vals.map(o=>o.uid).filter(Boolean))];
return JSON.stringify({href:location.href,ids:ids,uid:uids.length===1?uids[0]:null,count:ids.length});
}catch(e){return JSON.stringify({error:String(e)})}})()
"""

# The post's FULL current audience privacy object, correlated to the TARGET
# post: each PrivacyScopeEditableRenderer carries a `privacy_write_id` that
# base64-decodes to `privacy_scope_renderer:{"id":<post_id>}`, plus a
# `privacy_row_input` ({allow, base_state, deny, privacy_targeting,
# tag_expansion_state}) — the exact state the composer would send. We extract
# {allow, base_state, deny, tag_expansion_state} (the mutation's
# input.audience.privacy shape; `privacy_targeting` is composer-internal) so
# editing a "Friends except X" post keeps its exceptions instead of silently
# broadening to plain FRIENDS. __FB_PID is the permalink post id.
_PRIVACY_LOCATE_JS = r"""
(()=>{try{
const pid=__FB_PID;
const h=document.documentElement.innerHTML;
const grab=(s,key)=>{
const ki=s.indexOf('"'+key+'"');
if(ki<0)return null;
let i=s.indexOf(':',ki);if(i<0)return null;i++;
while(i<s.length&&/\s/.test(s[i]))i++;
if(s[i]!=='{')return null;
let depth=0,inStr=false,esc=false,start=i;
for(;i<s.length;i++){
const c=s[i];
if(inStr){if(esc)esc=false;else if(c==='\\')esc=true;else if(c==='"')inStr=false;continue}
if(c==='"'){inStr=true;continue}
if(c==='{')depth++;
else if(c==='}'){depth--;if(depth===0){i++;break}}
}
try{return JSON.parse(s.slice(start,i))}catch(e){return null}
};
const wre=/"privacy_write_id"\s*:\s*"([A-Za-z0-9+/=_-]{20,400})"/g;
const found={};
let m;
while((m=wre.exec(h))){
const raw=m[1].replace(/=+$/,"");
let dec="";
try{dec=atob((raw+"=".repeat((4-raw.length%4)%4)).replace(/-/g,"+").replace(/_/g,"/"));}catch(e){continue}
const mm=/"id":([0-9]+)/.exec(dec);
if(!mm||mm[1]!==pid)continue;
// privacy_row_input precedes privacy_write_id inside the same renderer.
const seg=h.slice(Math.max(0,m.index-12000),m.index);
const o=grab(seg,"privacy_row_input");
if(!o||typeof o.base_state!=="string")continue;
const privacy={allow:o.allow||[],base_state:o.base_state,deny:o.deny||[],tag_expansion_state:o.tag_expansion_state||"UNSPECIFIED"};
if(["SELF","FRIENDS","EVERYONE"].indexOf(privacy.base_state)<0)continue;
const k=JSON.stringify(privacy);
if(found[k]!==undefined)continue;
found[k]=privacy;
}
const vals=Object.values(found);
return JSON.stringify({href:location.href,privacy:vals.length===1?vals[0]:null,count:vals.length});
}catch(e){return JSON.stringify({error:String(e)})}})()
"""

_UID_JS = (
    '(()=>{try{const r=window.require;'
    'return r?((r("CurrentUserInitialData")||{}).USER_ID||null):null'
    "}catch(e){return null}})()"
)

# Fire the post mutation WITHOUT awaiting it — the result lands in
# window[<key>] under a per-call key so overlapping edits cannot clobber each
# other's result (same fire-and-poll pattern as _facebook_comment). The __FB_*
# placeholders are substituted before eval; __FB_STORY/__FB_TEXT/__FB_PRIVACY
# are JSON values, __FB_VARS is a JS object literal. The
# KEY+"_sent" sentinel is set synchronously BEFORE fetch fires: if _eval's
# transport times out but the expression actually executed, the retry sees the
# sentinel and returns early instead of firing the mutation a second time.
# body_slice stays 4000 — the story_edit payload is huge.
_EDIT_JS = fb_send_js(
    """\
vars.input.story_id=__FB_STORY;
vars.input.message.text=__FB_TEXT;
vars.input.message.ranges=[];
vars.input.actor_id=uid;
vars.input.client_mutation_id=String(Math.floor(Math.random()*900)+100);
if(vars.input.logging&&vars.input.logging.composer_session_id){vars.input.logging.composer_session_id=crypto.randomUUID()}
if(vars.input.audience){vars.input.audience.privacy=__FB_PRIVACY}""",
    r"""\
out.dataKeys=j.data?Object.keys(j.data):[];
const se=(j.data||{}).story_edit||{};
out.storyId=((se.story||{}).id)||null;
// Echo check runs against the FULL body `t` (not the 4000-char slice): the
// story_edit payload is huge and the new text can sit past the slice.
// Facebook JSON-escapes non-ASCII as \uXXXX, so check BOTH the raw needle and
// its \u-escaped form — otherwise every Vietnamese/emoji edit reports a false
// negative even though the mutation landed.
const _txt=JSON.stringify(vars.input.message.text);
const _txtEsc=_txt.replace(/[\u0080-￿]/g,c=>"\\u"+c.charCodeAt(0).toString(16).padStart(4,"0"));
out.textEchoed=t.indexOf(_txt)>=0||t.indexOf(_txtEsc)>=0;""",
    body_slice=4000,
)

_DELETE_JS = fb_send_js(
    """\
vars.input.story_id=__FB_STORY;
vars.input.actor_id=uid;
vars.input.client_mutation_id=String(Math.floor(Math.random()*900)+100);""",
    """\
out.dataKeys=j.data?Object.keys(j.data):[];
out.deletedStoryId=(((j.data||{}).story_delete||{}).deleted_story_id)||null;""",
    body_slice=4000,
)

_JS = {"edit": _EDIT_JS, "delete": _DELETE_JS}


def _raw_story_id(target: object) -> str | None:
    """Return the caller's raw story id unchanged when it is a Relay story id.

    Kept verbatim (caller's own padding) because the delete response echoes it
    for the positive-signal check; FB accepts unpadded base64."""
    if not isinstance(target, str) or not _STORY_ID_RE.match(target):
        return None
    try:
        decoded = base64.urlsafe_b64decode(
            target + "=" * (-len(target) % 4)
        ).decode("utf-8", "ignore")
    except Exception:
        return None
    if ":" not in decoded or not re.search(r"[0-9]{6,}", decoded):
        return None
    return target


def _story_fields(story_id: str) -> tuple[str, str] | None:
    """Decode a story id into (owner uid, post fbid) for permalink construction."""
    try:
        decoded = base64.urlsafe_b64decode(
            story_id + "=" * (-len(story_id) % 4)
        ).decode("utf-8", "ignore")
    except Exception:
        return None
    parts = decoded.split(":")
    uid = None
    fbid = None
    for part in parts:
        m = re.fullmatch(r"_?I([0-9]{6,})", part)
        if m and uid is None:
            uid = m.group(1)
    for part in parts:
        # ASCII digits only — str.isdigit() also accepts non-ASCII digits
        # (e.g. Arabic-Indic) which would build a bogus permalink.
        if part.isascii() and part.isdigit() and len(part) >= 6:
            fbid = part
            break
    if not uid or not fbid:
        return None
    return uid, fbid


def _post_target(target: object) -> tuple[str, str]:
    """Resolve user input to (permalink, post_id).

    Accepts a facebook.com post permalink or a raw story id; a raw story id is
    decoded to build the owner's post permalink (never an arithmetic id)."""
    raw = _raw_story_id(target)
    if raw is not None:
        fields = _story_fields(raw)
        if fields is None:
            raise ReachError(BAD_INPUT, "story id is not a recognizable Facebook story id")
        uid, fbid = fields
        return f"https://www.facebook.com/{uid}/posts/{fbid}", fbid
    if not isinstance(target, str):
        raise ReachError(BAD_INPUT, "url must be a facebook.com post permalink or a raw story id")
    _check_url(target)
    post_id = _post_id_of(target)
    if not post_id:
        raise ReachError(
            BAD_INPUT,
            "post edit/delete need a facebook.com post permalink (/posts/<id> or "
            "permalink.php?story_fbid=...) or a raw story id",
        )
    return target, post_id


def _resolve_story(target_id: str, post_id: str, timeout: float) -> tuple[str, str | None]:
    """Poll the page until exactly one story id matches the post id; fails closed."""
    expr = _STORY_LOCATE_JS.replace("__FB_PID", json.dumps(post_id))
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        # Bound each eval by the REMAINING budget — a hardcoded 10s eval inside
        # a 20s loop could burn the whole mutation phase on one slow call.
        raw = _eval(target_id, expr, max(1.0, min(10.0, deadline - time.monotonic())))
        try:
            info = json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError):
            info = {}
        if info.get("count") == 1:
            return info["ids"][0], info.get("uid")
        if (info.get("count") or 0) > 1:
            raise ReachError(
                UPSTREAM_BROKEN,
                f"Ambiguous post permalink — {info['count']} story nodes match post id {post_id}; refusing to proceed",
            )
        last = info
        time.sleep(0.8)
    raise ReachError(
        UPSTREAM_BROKEN,
        f"Could not resolve the post story id on the page ({last or 'no data'}) — is this a post permalink?",
    )


def _resolve_privacy(target_id: str, post_id: str, timeout: float) -> dict:
    """Poll the page until exactly one privacy object correlates to the target
    post; returns the FULL {allow, base_state, deny, tag_expansion_state} so an
    edit preserves exceptions ("Friends except X"), not just base_state.

    The page must still be showing the target post (href post id must match) —
    a privacy node correlated by id but rendered on a different page is not
    trusted."""
    expr = _PRIVACY_LOCATE_JS.replace("__FB_PID", json.dumps(post_id))
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        raw = _eval(target_id, expr, max(1.0, min(10.0, deadline - time.monotonic())))
        try:
            info = json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError):
            info = {}
        if (info.get("count") or 0) > 1:
            raise ReachError(
                UPSTREAM_BROKEN,
                f"Ambiguous post privacy — {info['count']} privacy nodes match post id {post_id}; refusing to proceed",
            )
        if info.get("count") == 1 and isinstance(info.get("privacy"), dict):
            if _post_id_of(info.get("href") or "") == post_id:
                return info["privacy"]
            # Page navigated away from the target post — keep polling.
        last = info
        time.sleep(0.8)
    raise ReachError(
        UPSTREAM_BROKEN,
        f"Could not determine the post's current audience ({last or 'no data'}) — refusing to edit (the edit must preserve the audience)",
    )


def _session_uid(target_id: str, timeout: float = 10.0) -> str | None:
    return _eval(target_id, _UID_JS, timeout)


def _check_ownership(story_uid: str | None, session_uid: str | None) -> None:
    if not story_uid or not session_uid:
        raise ReachError(UPSTREAM_BROKEN, "Could not verify the post owner — refusing to edit/delete")
    if str(story_uid) != str(session_uid):
        raise ReachError(
            UPSTREAM_BROKEN,
            "Target post is not yours (story owner differs from the logged-in account) — refusing to edit/delete",
        )


def _send_mutation(kind: str, target_id: str, *, deadline: float, **fields) -> dict:
    """Fire-and-poll one post mutation; returns the parsed page result."""
    friendly, doc_id = _DOC[kind]
    doc_id = query_ids.get("facebook", f"post_{kind}", doc_id)
    key = _result_key("__fbPostResult")
    expr = (
        _JS[kind]
        .replace("__FB_VARS", json.dumps(_VARS[kind], ensure_ascii=False))
        .replace("__FB_FRIENDLY", json.dumps(friendly))
        .replace("__FB_DOC", json.dumps(doc_id))
        .replace("__FB_KEY", json.dumps(key))
    )
    # Single-pass substitution: user-controlled values (the post text can
    # literally contain "__FB_PRIVACY") are inserted by ONE re.sub scan, so a
    # substituted value is never re-scanned for further placeholders.
    if fields:
        pattern = re.compile("|".join(re.escape(p) for p in fields))
        expr = pattern.sub(lambda m: json.dumps(fields[m.group(0)]), expr)
    return send_mutation(target_id, expr, deadline=deadline, key=key, label=f"Post {kind}", eval_fn=_eval)


def _strip_pad(value: object) -> str:
    """Padding-insensitive id compare — FB echoes ids with '=' padding that
    can differ from the sent spelling."""
    return "".join(str(value or "").split("="))


def _edited_post(out: dict, text: str, story_id: str) -> bool:
    """Positive edit signal: story_edit payload key AND the response's parsed
    story id echoing the target AND the new text echoed.

    The text echo prefers the JS-side `textEchoed` flag (computed against the
    FULL response body — `out["body"]` is sliced to 4000 chars and the
    story_edit payload can push the echo past the slice); the body substring
    check remains as a fallback for older page results."""
    keys = out.get("dataKeys") or []
    if not any(str(k).startswith("story_edit") for k in keys):
        return False
    sid = out.get("storyId")
    if not sid or _strip_pad(sid) != _strip_pad(story_id):
        return False
    if out.get("textEchoed") is not None:
        return bool(out.get("textEchoed"))
    body = out.get("body") or ""
    # JSON.stringify and json.dumps(ensure_ascii=False) escape identically, so
    # the quoted needle matches the echoed `"text":"..."` in the response.
    return json.dumps(text, ensure_ascii=False) in body


def _deleted_post(out: dict, story_id: str) -> bool:
    """Positive deletion signal: story_delete payload key AND the parsed
    deleted_story_id echoing the target story id (padding-insensitive).
    A bare {"data":{"story_delete":null}} / {} is a silent no-op -> fail closed."""
    keys = out.get("dataKeys") or []
    if not any(str(k).startswith("story_delete") for k in keys):
        return False
    value = out.get("deletedStoryId")
    if not isinstance(value, str) or not value:
        return False
    return _strip_pad(value) == _strip_pad(story_id)


def _check_graphql(kind: str, out: dict, *, story_id: str | None = None, text: str | None = None) -> None:
    """Map transport/GraphQL failures onto Reach errors (shared by both kinds)."""

    # Positive signal beats partial errors: FB can attach non-fatal errors to
    # a payload that still confirms the mutation — check the confirmation
    # BEFORE errors[] so a confirmed delete/edit is not misreported as a failure.
    def confirmed() -> bool:
        if kind == "delete":
            return _deleted_post(out, story_id or "")
        return (
            kind == "edit"
            and text is not None
            and bool(story_id)
            and _edited_post(out, text, story_id or "")
        )

    if classify_result(out, label="post mutation", name=_DOC[kind][0], confirmed=confirmed):
        return

    if kind == "delete":
        raise ReachError(
            UPSTREAM_BROKEN,
            "Post delete returned no confirmation (story_delete payload missing/null) — "
            "check facebook.com before retrying; the post may not be yours or may not exist",
        )
    if kind == "edit" and text is not None:
        raise ReachError(
            UPSTREAM_BROKEN,
            "Post edit returned no confirmation (edited text not echoed in story_edit) — "
            "check facebook.com before retrying",
        )


def facebook_post_edit(*, url: str, text: str, timeout: float) -> list:
    """Edit the caller's own Facebook post text, preserving its audience."""
    if not isinstance(text, str) or not text.strip():
        raise ReachError(BAD_INPUT, "text must be a non-empty string")
    permalink, post_id = _post_target(url)

    deadline, left, tid = fb_write_target(timeout, target_fn=_fb_target, wait_fn=_wait_ready)
    _ensure_post_loaded(tid, permalink, left(25))
    story_id, story_uid = _resolve_story(tid, post_id, left(20))
    _check_ownership(story_uid, _session_uid(tid, left(10)))
    privacy = _resolve_privacy(tid, post_id, left(20))

    out = _send_mutation(
        "edit", tid, deadline=deadline,
        __FB_STORY=story_id, __FB_TEXT=text, __FB_PRIVACY=privacy,
    )
    _check_graphql("edit", out, story_id=story_id, text=text)

    return [
        make_item(
            title=text[:120],
            url=permalink,
            text="Edited",
            raw={
                "status": out.get("status"),
                "url": permalink,
                "storyId": out.get("storyId") or story_id,
                "audience": privacy.get("base_state"),
                "privacy": privacy,
            },
        )
    ]


def facebook_post_delete(*, url: str, confirm: bool, timeout: float) -> list:
    """Delete the caller's own Facebook post. Requires confirm=True."""
    # Strict identity check: a truthy string like "false" must never delete.
    if confirm is not True:
        raise ReachError(
            BAD_INPUT,
            "post-delete requires explicit confirmation — pass --confirm (this permanently deletes the post and its comments/reactions)",
        )
    permalink, post_id = _post_target(url)

    deadline, left, tid = fb_write_target(timeout, target_fn=_fb_target, wait_fn=_wait_ready)
    _ensure_post_loaded(tid, permalink, left(25))
    story_id, story_uid = _resolve_story(tid, post_id, left(20))
    _check_ownership(story_uid, _session_uid(tid, left(10)))

    out = _send_mutation("delete", tid, deadline=deadline, __FB_STORY=story_id)
    _check_graphql("delete", out, story_id=story_id)

    return [
        make_item(
            title="Facebook post deleted",
            url=permalink,
            text="Deleted",
            raw={
                "status": out.get("status"),
                "url": permalink,
                "storyId": story_id,
                "deletedStoryId": out.get("deletedStoryId"),
                "removed": True,
            },
        )
    ]

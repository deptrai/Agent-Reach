#!/usr/bin/env python3
"""Capture real GraphQL mutation/request bodies on Facebook or X and turn
them into sanitized skeletons that the write lanes replay with live tokens.

Facebook (--platform fb, default) — identical to the old
scripts/capture_fb_mutation.py: requires `opencli` and a HEADED profile
already logged in to facebook.com. Arms `window.__fbCap`, filters
/api/graphql POSTs by `fb_api_req_friendly_name`, sanitizes (actor_id/uid
-> "0", uuids -> zero-uuid, epochs -> 0, attribution_id_v2 -> template),
writes {"params": ..., "variables": ...} — the same shape as
_fb_post_create_vars.json.

X/Twitter (--platform x) — uses the DEDICATED browser (CloakBrowser, CDP
127.0.0.1:9222) which already holds a logged-in x.com session
(auth_token + ct0). Arms `window.__cap` via Runtime.evaluate (spike 12.1:
Page.addScriptToEvaluateOnNewDocument does NOT run in the main world on
CloakBrowser), captures /i/api/graphql/<queryId>/<Operation> requests —
POST body AND request headers, plus GET ops whose variables/features ride
in the query string — then writes
{platform, operation, method, query_id, path_template, headers_template,
body_template} to agent_reach/channels/_x_<op>_vars.json. Auth headers
become placeholders (Bearer <bearer> / <ct0> / <fresh-txid>), cookie
headers are dropped, and X ids (rest_id/tweet_id/...) scrub to "0" —
real values NEVER touch disk.

FB usage (unchanged):
    uv run python scripts/capture_mutation.py --platform fb \
        --profile acrtv927 --name UFILikeMutation --seconds 90

X usage (do the action — or just scroll the timeline — while it watches):
    uv run python scripts/capture_mutation.py --platform x \
        --name HomeTimeline --seconds 60

Both: `--name` substring-filters the captured op names, `--list-only`
only lists names, `--out` overrides the skeleton path.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Shared sanitize rules (FB and X). Defaults keep FB output byte-identical.
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)
_EPOCH_RE = re.compile(r"^1[0-9]{11,12}$")  # epoch seconds/ms, 2020s-2030s
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"
_ID_KEYS = {"actor_id", "__user", "user_id", "uid", "av"}

# X-only keys scrubbed to "0" — replay injects the live target ids. Kept out
# of _ID_KEYS so FB skeletons stay byte-identical.
_X_ID_KEYS = {
    "rest_id", "tweet_id", "tweet_ids", "tweetId", "userId", "restId",
    "in_reply_to_tweet_id", "quoted_tweet_id", "conversation_id",
    "dm_conversation_id", "community_id", "list_id", "media_id",
    "focal_tweet_id", "promoted_tweet_id",
}
# X keys whose VALUE is a credential — redacted wherever they appear.
_X_SECRET_KEYS = {
    "auth_token", "ct0", "bearer", "access_token", "api_key", "api_secret",
    "guest_token", "x_csrf_token", "x-csrf-token", "password",
}


def sanitize(value, key: str = "", id_keys=None):
    """Strip identity/session-specific values from a captured `variables`
    object, keeping structure (incl. __relay_internal__pv__* flags).

    `id_keys` defaults to the FB set; X callers pass `_ID_KEYS | _X_ID_KEYS`
    so the FB output stays byte-identical."""
    ids = _ID_KEYS if id_keys is None else id_keys
    if isinstance(value, dict):
        return {k: sanitize(v, k, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, key, ids) for v in value]
    if isinstance(value, str):
        if key in ids:
            return "0"
        if key == "attribution_id_v2":
            return "CometHomeRoot.react,comet.home,via_cold_start,0,0,0,,"
        if key == "idempotence_token":
            return _ZERO_UUID + ("_FEED" if value.endswith("_FEED") else "")
        if key in _X_SECRET_KEYS:
            return "<redacted>"
        if key.endswith("session_id"):
            return _ZERO_UUID
        if _UUID_RE.search(value):
            return _UUID_RE.sub(_ZERO_UUID, value)
        if _EPOCH_RE.match(value):
            return "0"
        return value
    if isinstance(value, int) and 10**11 <= value < 10**13:
        return 0  # epoch ms
    return value


# ---------------------------------------------------------------------------
# opencli driver (FB capture — headed profile).
# ---------------------------------------------------------------------------


def oc(profile: str, session: str, *args: str, timeout: int = 60) -> str:
    """Run `opencli --profile <p> browser <sess> ...` and return stdout."""
    r = subprocess.run(
        ["opencli", "--profile", profile, "browser", session, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"opencli {' '.join(args)} failed ({r.returncode}): "
            f"{(r.stderr or r.stdout).strip()[:300]}"
        )
    return r.stdout


def oc_eval(profile: str, session: str, expr: str, timeout: int = 30):
    """Eval JS in the session tab; parse the last non-empty line as JSON."""
    out = oc(profile, session, "eval", expr, timeout=timeout)
    last = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
    last = last.strip()
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return last


# ---------------------------------------------------------------------------
# Facebook capture (unchanged behavior from capture_fb_mutation.py).
# ---------------------------------------------------------------------------

_ARM_JS_FB = (
    "(()=>{window.__fbCap=[];"
    "const _o=XMLHttpRequest.prototype.open,_s=XMLHttpRequest.prototype.send;"
    "XMLHttpRequest.prototype.open=function(m,u){this.__u=u;return _o.apply(this,arguments)};"
    "XMLHttpRequest.prototype.send=function(b){try{"
    "if((this.__u||'').includes('/api/graphql')&&typeof b=='string'){"
    "const n=new URLSearchParams(b).get('fb_api_req_friendly_name')||'?';"
    "window.__fbCap.push({n:n,b:b});}}catch(e){}"
    "return _s.apply(this,arguments)};"
    "const _f=window.fetch;"
    "window.fetch=function(u,i){try{"
    "const b=(i&&i.body)||'';"
    "if(String(u).includes('/api/graphql')&&typeof b=='string'){"
    "const n=new URLSearchParams(b).get('fb_api_req_friendly_name')||'?';"
    "window.__fbCap.push({n:n,b:b});}}catch(e){}"
    "return _f.apply(this,arguments)};"
    "return 'armed'})()"
)
_NAMES_JS_FB = "JSON.stringify((window.__fbCap||[]).map(x=>x.n))"


def parse_body(body: str) -> dict:
    """Parse a captured form-urlencoded request body."""
    params = dict(urllib.parse.parse_qsl(body))
    out = {
        "friendly_name": params.get("fb_api_req_friendly_name"),
        "doc_id": params.get("doc_id"),
        "variables": json.loads(params.get("variables") or "{}"),
        "params": {k: v for k, v in params.items() if k != "variables"},
    }
    return out


def default_out_name(friendly: str) -> str:
    # friendly_name comes from the page (a form field the tab pushes into the
    # capture buffer) — strip to a safe slug so "../x" can't escape the dir.
    name = re.sub(r"[^A-Za-z0-9]", "", friendly or "") or "mutation"
    # UFILikeMutation -> UFI_Like_Mutation -> ufi_like_mutation
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    slug = re.sub(r"_mutation$", "", name.lower())
    return f"agent_reach/channels/_fb_{slug}_vars.json"


# ---------------------------------------------------------------------------
# X (Twitter) capture — dedicated browser over CDP.
# ---------------------------------------------------------------------------

_X_HOSTS = ("x.com", "twitter.com")
_X_MARKER = "/i/api/graphql/"

# Header names whose live values must never persist (replay re-mints them).
_X_HEADER_PLACEHOLDERS = {
    "authorization": "Bearer <bearer>",
    "x-csrf-token": "<ct0>",
    "x-client-transaction-id": "<fresh-txid>",
    "x-guest-token": "<guest-token>",
}
_X_HEADER_DROP = {"cookie", "set-cookie"}

# Interceptor: capture {n: op, u: url, q: queryId, m: method, h: headers,
# b: body} for every /i/api/graphql/ call — fetch AND XHR, GET AND POST
# (read-side ops like HomeTimeline are GETs; mutations are POSTs).
_ARM_JS_X = r"""
(()=>{window.__cap=[];
const M="/i/api/graphql/";
const rec=(o)=>{try{window.__cap.push(o)}catch(e){}};
const hdrs=(h)=>{try{const o={};new Headers(h||{}).forEach((v,k)=>{o[k]=v});return o}catch(e){return{}}};
const opof=(u)=>{try{const p=String(u).split(M)[1].split("?")[0].split("/");
return{q:p[p.length-2]||"",n:p[p.length-1]||"?"}}catch(e){return{q:"",n:"?"}}};
const X=window.XMLHttpRequest;
const origOpen=X.prototype.open,origSend=X.prototype.send,origH=X.prototype.setRequestHeader;
X.prototype.open=function(m,u,...r){this.__m=m;this.__u=u;this.__h={};return origOpen.call(this,m,u,...r)};
X.prototype.setRequestHeader=function(k,v){try{this.__h[k]=v}catch(e){}return origH.apply(this,arguments)};
X.prototype.send=function(b){try{
if(String(this.__u||"").includes(M)){const o=opof(this.__u);
rec({n:o.n,u:String(this.__u),q:o.q,m:String(this.__m||"GET").toUpperCase(),h:this.__h||{},b:(typeof b==="string"?b:"")});
}}catch(e){}
return origSend.apply(this,arguments)};
const F=window.fetch;
window.fetch=function(u,o){try{
const url=typeof u==="string"?u:(u&&u.url)||"";
const m=String((o&&o.method)||(u&&u.method)||"GET").toUpperCase();
if(url.includes(M)){const op=opof(url);const b=(o&&typeof o.body==="string")?o.body:"";
rec({n:op.n,u:url,q:op.q,m:m,h:hdrs((o&&o.headers)||(u&&u.headers)),b:b});
}}catch(e){}
return F.apply(this,arguments)};
return "armed"})()
"""
_NAMES_JS_X = "JSON.stringify((window.__cap||[]).map(x=>x.m+':'+x.n))"
_DUMP_JS_X = "JSON.stringify(window.__cap||[])"


def _is_x_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    return any(host == s or host.endswith("." + s) for s in _X_HOSTS)


def _x_target(create: bool, url: str):
    """An x.com page target in the dedicated browser (create one if absent)."""
    from agent_reach.runtime import cdp

    targets = cdp._json("/json/list") or []
    for t in targets:
        if t.get("type") == "page" and _is_x_url(t.get("url", "")):
            return t
    if not create:
        return None
    made = cdp.call("Target.createTarget", {"url": url})
    tid = (made or {}).get("targetId")
    if not tid:
        return None
    for t in cdp._json("/json/list") or []:
        if t.get("id") == tid:
            return t
    return {"id": tid, "type": "page", "url": url}


def _cdp_eval(target_id: str, expr: str, timeout: float = 30.0):
    """Runtime.evaluate on a page target; retries transient None results."""
    from agent_reach.runtime import cdp

    result = None
    for _ in range(4):
        result = cdp.call(
            "Runtime.evaluate",
            {"expression": expr, "awaitPromise": True, "returnByValue": True,
             "timeout": int(timeout * 1000)},
            target_id=target_id,
        )
        if result is not None:
            break
        time.sleep(0.5)
    if result is None:
        raise RuntimeError("could not reach the dedicated browser page")
    res = (result or {}).get("result") or {}
    if res.get("subtype") == "error" or (result.get("exceptionDetails") or {}).get("text"):
        raise RuntimeError(
            str(res.get("description") or result.get("exceptionDetails", {}).get("text"))[:300]
        )
    return res.get("value")


def _wait_x_ready(target_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _cdp_eval(target_id, "document.readyState", 8) in ("interactive", "complete"):
            return
        time.sleep(0.8)
    raise RuntimeError("x.com did not finish loading in the dedicated browser")


def x_op_from_url(url: str) -> tuple:
    """'https://x.com/i/api/graphql/<queryId>/<Op>?...' -> (queryId, Op)."""
    if _X_MARKER not in url:
        return "", "?"
    after = url.split(_X_MARKER, 1)[-1].split("?", 1)[0].strip("/")
    parts = [p for p in after.split("/") if p]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return "", parts[-1] if parts else "?"


def parse_x_body(rec: dict) -> dict:
    """Normalize one captured X request to {variables, features, queryId}.

    POST ops carry a JSON body; GET ops (HomeTimeline & friends) carry the
    same fields as URL query params."""
    body = rec.get("b") or ""
    if body:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {"_raw": body}
    else:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(rec.get("u") or "").query)
        data = {}
        for k in ("variables", "features"):
            raw = (qs.get(k) or [""])[0]
            if raw:
                try:
                    data[k] = json.loads(raw)
                except json.JSONDecodeError:
                    data[k] = raw
    data.setdefault("queryId", rec.get("q") or "")
    return data


def sanitize_x_headers(headers: dict) -> dict:
    """Drop cookie headers, replace auth headers with placeholders, keep
    the x-twitter-* constants verbatim. Real token values never persist."""
    out = {}
    for k, v in (headers or {}).items():
        lk = str(k).lower()
        if lk in _X_HEADER_DROP:
            continue
        if lk in _X_HEADER_PLACEHOLDERS:
            out[k] = _X_HEADER_PLACEHOLDERS[lk]
            continue
        # x-twitter-* names are static client constants replay needs verbatim
        # (auth-type/active-user/client-language carry no credentials).
        if lk.startswith("x-twitter-"):
            out[k] = sanitize(str(v))
            continue
        if any(s in lk for s in ("token", "auth", "secret", "credential")):
            out[k] = "<redacted>"
            continue
        out[k] = sanitize(str(v))
    return out


def default_out_name_x(op: str) -> str:
    name = op or "op"
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return f"agent_reach/channels/_x_{name.lower()}_vars.json"


def _skeleton_x(rec: dict) -> dict:
    body = parse_x_body(rec)
    return {
        "platform": "x",
        "operation": rec.get("n") or "?",
        "method": (rec.get("m") or "POST").upper(),
        "query_id": rec.get("q") or "",
        "path_template": "/i/api/graphql/{query_id}/" + (rec.get("n") or "{operation}"),
        "headers_template": sanitize_x_headers(rec.get("h") or {}),
        "body_template": sanitize(body, id_keys=_ID_KEYS | _X_ID_KEYS),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _run_fb(args) -> int:
    print(f"[*] opening {args.url} (profile={args.profile}, session={args.session})")
    # `tab new` creates a dedicated tab — avoids `open`'s attach failure when
    # the profile's active tab is a chrome-extension:// page.
    oc(args.profile, args.session, "tab", "new", args.url, timeout=90)
    time.sleep(2.0)
    print(f"[*] arming interceptor: {oc_eval(args.profile, args.session, _ARM_JS_FB)}")
    print(f"[*] watching {args.seconds}s — perform the action in the browser now")
    deadline = time.time() + args.seconds
    seen: set[str] = set()
    while time.time() < deadline:
        names = oc_eval(args.profile, args.session, _NAMES_JS_FB)
        if isinstance(names, list):
            for n in names:
                if n not in seen:
                    seen.add(n)
                    print(f"    captured: {n}")
        time.sleep(args.poll)

    names = oc_eval(args.profile, args.session, _NAMES_JS_FB)
    if not isinstance(names, list) or not names:
        print("[!] nothing captured — interceptor died (page navigated?) or no /api/graphql POST fired", file=sys.stderr)
        return 1
    print(f"[*] {len(names)} requests captured")

    hits = [(i, n) for i, n in enumerate(names) if not args.name or args.name.lower() in str(n).lower()]
    if args.list_only:
        return 0
    if not hits:
        print(f"[!] no mutation matched --name {args.name!r}; captured: {sorted(set(map(str, names)))}", file=sys.stderr)
        return 1

    wrote = []
    for i, n in hits:
        body = oc_eval(args.profile, args.session, f"window.__fbCap[{i}].b")
        if not isinstance(body, str) or "variables" not in body:
            print(f"    skip {n}: body not readable", file=sys.stderr)
            continue
        parsed = parse_body(body)
        # Keep the transport params template too — a variables-only skeleton
        # would leave _PARAMS_TEMPLATE empty and replay without __dyn/__csr/...
        skeleton = {
            "params": sanitize(parsed["params"]),
            "variables": sanitize(parsed["variables"]),
        }
        out_path = args.out or default_out_name(str(parsed["friendly_name"]))
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(skeleton, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"    {parsed['friendly_name']}  doc_id={parsed['doc_id']}  -> {out_path}")
        wrote.append(out_path)
    return 0 if wrote else 1


def _run_x(args) -> int:
    from agent_reach.runtime import browser

    url = args.url if _is_x_url(args.url) else "https://x.com/home"
    browser.ensure_running("capture")
    target = _x_target(create=True, url=url)
    if not target:
        print("[!] no x.com tab in the dedicated browser", file=sys.stderr)
        return 1
    tid = target.get("id")
    print(f"[*] x.com tab: {tid} ({target.get('url', '')[:80]})")
    _wait_x_ready(tid, 30)
    time.sleep(1.0)
    print(f"[*] arming interceptor: {_cdp_eval(tid, _ARM_JS_X, 15)}")
    print(f"[*] watching {args.seconds}s — scroll/navigate or act in the tab now")
    deadline = time.time() + args.seconds
    seen: set[str] = set()
    while time.time() < deadline:
        names = _cdp_eval(tid, _NAMES_JS_X, 15)
        if isinstance(names, list):
            for n in names:
                if n not in seen:
                    seen.add(n)
                    print(f"    captured: {n}")
        time.sleep(args.poll)

    raw = _cdp_eval(tid, _DUMP_JS_X, 15)
    try:
        records = json.loads(raw) if isinstance(raw, str) else []
    except json.JSONDecodeError:
        records = []
    if not records:
        print("[!] nothing captured — no /i/api/graphql/ request fired (interceptor died on navigation?)", file=sys.stderr)
        return 1
    print(f"[*] {len(records)} requests captured")

    hits = [r for r in records
            if not args.name or args.name.lower() in str(r.get("n") or "").lower()]
    if args.list_only:
        return 0
    if not hits:
        print(f"[!] no op matched --name {args.name!r}; captured: "
              f"{sorted({str(r.get('n')) for r in records})}", file=sys.stderr)
        return 1

    wrote = []
    seen_ops: set[str] = set()
    for rec in hits:
        op = str(rec.get("n") or "?")
        if op in seen_ops:  # keep the FIRST capture of each op (closest to arm time)
            continue
        seen_ops.add(op)
        skeleton = _skeleton_x(rec)
        out_path = args.out or default_out_name_x(op)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(skeleton, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"    {skeleton['method']} {op}  query_id={skeleton['query_id']}  -> {out_path}")
        wrote.append(out_path)
    return 0 if wrote else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform", choices=["fb", "x"], default="fb",
                    help="fb: opencli headed profile (default) | x: dedicated browser CDP")
    ap.add_argument("--profile", help="[fb] opencli profile logged in to facebook.com (headed)")
    ap.add_argument("--session", default="fb", help="[fb] opencli browser session name (default: fb)")
    ap.add_argument("--url", default="https://www.facebook.com/",
                    help="[fb] URL to open before arming | [x] x.com URL for a new tab (default: fb home)")
    ap.add_argument("--name", default="", help="only keep ops whose name contains this")
    ap.add_argument("--seconds", type=int, default=90, help="watch duration (default: 90)")
    ap.add_argument("--poll", type=float, default=1.5, help="poll interval seconds (default: 1.5)")
    ap.add_argument("--out", default="", help="output skeleton path (default: _<platform>_<name>_vars.json)")
    ap.add_argument("--list-only", action="store_true", help="only list captured op names")
    args = ap.parse_args(argv)

    if args.platform == "x":
        return _run_x(args)
    if not args.profile:
        ap.error("--profile is required for --platform fb")
    return _run_fb(args)


if __name__ == "__main__":
    raise SystemExit(main())

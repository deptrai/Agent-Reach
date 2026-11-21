#!/usr/bin/env python3
"""Capture real Facebook /api/graphql mutation bodies and turn them into
sanitized `variables` skeletons that `_facebook_write.py` (and future write
actions) replay with live tokens.

Requires `opencli` CLI and a browser profile already logged in to
facebook.com. The profile must be HEADED — the whole point is a human (or
another tool) performs the action while this script watches.

Usage:
    uv run python scripts/capture_fb_mutation.py \
        --profile acrtv927 --name UFILikeMutation --seconds 90 \
        --out agent_reach/channels/_fb_like_vars.json

Then do the action once in that browser (click Like, post a comment, ...).
The script dumps every captured mutation name; matching bodies are parsed,
sanitized (actor_id -> "0", UUIDs -> zero, epoch timestamps -> 0,
attribution_id_v2 -> template) and written as {"params": ..., "variables": ...}
— the same shape as `_fb_post_create_vars.json`, so the variables skeleton survives a
re-capture instead of silently degrading to variables-only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse

_ARM_JS = (
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
_NAMES_JS = "JSON.stringify((window.__fbCap||[]).map(x=>x.n))"

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)
_EPOCH_RE = re.compile(r"^1[0-9]{11,12}$")  # epoch seconds/ms, 2020s-2030s
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"
_ID_KEYS = {"actor_id", "__user", "user_id", "uid", "av"}


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


def sanitize(value, key: str = ""):
    """Strip identity/session-specific values from a captured `variables`
    object, keeping structure (incl. __relay_internal__pv__* flags)."""
    if isinstance(value, dict):
        return {k: sanitize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, key) for v in value]
    if isinstance(value, str):
        if key in _ID_KEYS:
            return "0"
        if key == "attribution_id_v2":
            return "CometHomeRoot.react,comet.home,via_cold_start,0,0,0,,"
        if key == "idempotence_token":
            return _ZERO_UUID + ("_FEED" if value.endswith("_FEED") else "")
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
    name = friendly or "mutation"
    # UFILikeMutation -> UFI_Like_Mutation -> ufi_like_mutation
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    slug = re.sub(r"_mutation$", "", name.lower())
    return f"agent_reach/channels/_fb_{slug}_vars.json"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True, help="opencli profile logged in to facebook.com (headed)")
    ap.add_argument("--session", default="fb", help="opencli browser session name (default: fb)")
    ap.add_argument("--url", default="https://www.facebook.com/", help="URL to open before arming")
    ap.add_argument("--name", default="", help="only keep mutations whose friendly name contains this")
    ap.add_argument("--seconds", type=int, default=90, help="watch duration (default: 90)")
    ap.add_argument("--poll", type=float, default=1.5, help="poll interval seconds (default: 1.5)")
    ap.add_argument("--out", default="", help="output skeleton path (default: _fb_<name>_vars.json)")
    ap.add_argument("--list-only", action="store_true", help="only list captured mutation names")
    args = ap.parse_args(argv)

    print(f"[*] opening {args.url} (profile={args.profile}, session={args.session})")
    # `tab new` creates a dedicated tab — avoids `open`'s attach failure when
    # the profile's active tab is a chrome-extension:// page.
    oc(args.profile, args.session, "tab", "new", args.url, timeout=90)
    time.sleep(2.0)
    print(f"[*] arming interceptor: {oc_eval(args.profile, args.session, _ARM_JS)}")
    print(f"[*] watching {args.seconds}s — perform the action in the browser now")
    deadline = time.time() + args.seconds
    seen: set[str] = set()
    while time.time() < deadline:
        names = oc_eval(args.profile, args.session, _NAMES_JS)
        if isinstance(names, list):
            for n in names:
                if n not in seen:
                    seen.add(n)
                    print(f"    captured: {n}")
        time.sleep(args.poll)

    names = oc_eval(args.profile, args.session, _NAMES_JS)
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


if __name__ == "__main__":
    raise SystemExit(main())

"""Hot-reloadable persisted-query ids (FB doc_ids, X queryIds).

`state.root()/query-ids.json` maps channel → query name → id:

    {"facebook": {"post_create": "29221…", "react": "27646…"}, "x": {}}

`get()` reads the file fresh on EVERY call — no module-level cache — so an
operator edits the file and the next action send picks the new id up without a
release. The config only ever overlays module constants: a missing file, bad
JSON or an empty entry resolves to the caller-supplied default, so a broken
config can never break a send. Under a farm env window `state.root()` follows
the account's `HOME`, so each account can carry its own overrides when a
rotation turns out per-account rather than global.

`drift_probe`/`probe_facebook` implement the doctor drift check: send one
POST with the resolved doc_id and empty variables — a live persisted query
answers a variable-validation error, a dead one answers "The GraphQL document
with ID … was not found" / HTTP 404 (same signature `classify_result` maps to
PERSISTED_QUERY_DEAD). `drift_verdict` correlates ≥2 probe results: all dead →
"global" (platform-wide rotation), mixed → "per-account" (one variant is
stale). A single probe yields "single-account" — it CANNOT distinguish a
global rotation from a per-account variant; that distinction needs ≥2 targets.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import state

CONFIG_NAME = "query-ids.json"

#: Ship-shape documentation of the ids baked into the code; the runtime never
#: reads it (fallbacks are the module constants) — it exists so operators can
#: copy it to ``config_path()`` and edit ids without a release.
SEEDED_DEFAULTS = Path(__file__).resolve().parent.parent / "channels" / CONFIG_NAME

_DEAD_DOC_NEEDLE = "graphql document"
_DEAD_DOC_TAIL = "not found"


def config_path() -> Path:
    """The query-id config file for the current identity (per state.root())."""
    return state.root() / CONFIG_NAME


def load() -> dict:
    """Parse the config file fresh; {} on missing/unreadable/malformed input."""
    try:
        doc = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def get(channel: str, name: str, default: str = "") -> str:
    """Resolve one persisted-query id; config wins, `default` is the fallback.

    Read at action-send time on every call — this is the hot reload path."""
    value = (load().get(channel) or {}).get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int):
        return str(value)
    return default


def dead_document(probe: Mapping[str, Any]) -> bool:
    """True when a probe result carries the persisted-query-dead signature:
    "graphql document … not found" in the body/errors, or an HTTP 404."""
    status = probe.get("status")
    if status == 404:
        return True
    text = json.dumps({k: probe.get(k) for k in ("error", "body", "errors")}, ensure_ascii=False).lower()
    return _DEAD_DOC_NEEDLE in text and _DEAD_DOC_TAIL in text


def drift_verdict(results: Sequence[Mapping[str, Any]]) -> str:
    """Correlate probe results into global|per-account|single-account.

    ≥2 results: all stale → "global" (the platform rotated the id); some
    stale, some live → "per-account" (variants differ — config must be
    per-account). Exactly one result → "single-account": it proves THAT target
    is dead but cannot separate a platform-wide rotation from a variant-bound
    one — the epic's Done-when needs ≥2 accounts to say "global"."""
    dead = sum(1 for r in results if r.get("stale"))
    if len(results) < 2:
        return "single-account"
    return "global" if dead == len(results) else "per-account" if dead else "live"


def drift_probe(
    channel: str,
    name: str,
    default: str,
    probe_fn: Callable[[str], Mapping[str, Any]],
) -> dict:
    """Probe one persisted query through `probe_fn(doc_id)` and classify drift."""
    doc_id = get(channel, name, default)
    result = dict(probe_fn(doc_id) or {})
    result.setdefault("stale", dead_document(result))
    return {
        "channel": channel,
        "name": name,
        "doc_id": doc_id,
        "results": [result],
        "verdict": drift_verdict([result]),
    }


# ── live Facebook probe (doctor --probe-docids) ──

_FB_PROBE_JS = """
(()=>{try{
const req=window.require;
if(!req)return{error:"no_require"};
const uid=(req("CurrentUserInitialData")||{}).USER_ID;
const gp=req("getAsyncParams");
if(!uid||!gp)return{error:"no_tokens",hasUid:!!uid};
const base=gp("POST")||{};
if(!base.fb_dtsg||!base.lsd)return{error:"no_tokens",hasUid:true};
const p=new URLSearchParams();
for(const k in base)p.set(k,base[k]);
p.set("av",uid);
p.set("fb_api_req_friendly_name",__FB_FRIENDLY);
p.set("fb_api_caller_class","RelayModern");
p.set("doc_id",__FB_DOC);
p.set("variables","{}");
// Empty variables: a live doc fails validation (query alive), a dead doc_id
// fails with "document not found" — the probe never reaches a real mutation.
return fetch("/api/graphql/",{method:"POST",credentials:"include",
headers:{"Content-Type":"application/x-www-form-urlencoded","X-FB-Friendly-Name":__FB_FRIENDLY},
body:p.toString()}).then(r=>r.text().then(t=>({status:r.status,body:t.slice(0,2000)})))
}catch(e){return{error:String(e)}}})()
"""


def probe_facebook(target_id: str, eval_fn: Callable, *, timeout: float = 20.0) -> dict:
    """Probe the resolved facebook post_create doc_id on a live FB page.

    `eval_fn` is injected so tests (and future per-account probes running each
    account's own session) can stub it — signature matches the channel
    modules' ``_eval``."""
    from agent_reach.channels import _facebook_write as fw

    expr = (
        _FB_PROBE_JS.replace("__FB_FRIENDLY", json.dumps("ComposerStoryCreateMutation"))
        .replace("__FB_DOC", json.dumps(get("facebook", "post_create", fw._DOC_ID)))
    )
    out = eval_fn(target_id, expr, timeout)
    return dict(out) if isinstance(out, Mapping) else {"error": repr(out)}

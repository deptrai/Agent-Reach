"""Fallback write lane: XActions HTTP lane via a one-shot node subprocess (story 16.2).

Triggered from dispatch only after a twitter write backend dies with a
persisted-query/schema error (see dispatch.FALLBACK_TRIGGERS). The lane
reuses the XActions account pool provisioned by the identity bridge
(xactions_bridge): the linked pool entry already holds the session cookies
AND the account proxy (`accounts.proxy`), and `TwitterHttpClient` routes
every request through that proxy (`client.js` ProxyAgent). No proxy env var
is needed — egress is the pool column, which is the authoritative per-account
proxy store.

Invocation surface (verified against XActions @ main-clean): there is no CLI
or MCP tool that both selects a pool entry AND posts through the HTTP lane
(`xactions client post` uses the legacy Scraper + cookies.json; the MCP
`x_post_tweet` text path uses a browser). The real lane is the node module
`src/scrapers/twitter/http` (`createAccountPool` + `postTweet`/`postThread`).
This module calls it with a tiny inline script; the repos stay decoupled —
no dependency on XActions internals beyond that public module surface.

The script's JSON result is the only thing trusted back. Anything else the
child prints is discarded.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from agent_reach.utils.paths import home_dir

from . import xactions_bridge as bridge
from .result import (
    BAD_INPUT,
    NEED_LOGIN,
    NEED_SETUP,
    PERSISTED_QUERY_DEAD,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    ReachError,
    make_item,
)

#: Verbs the lane can replay. Mirrors the twitter entries of WRITE_ACTIONS.
FALLBACK_VERBS = ("post", "reply", "quote", "thread")

_DEFAULT_TIMEOUT = 60.0
_MAX_OUTPUT = 64 * 1024

#: One-shot runner. Args arrive as plain strings on argv (no shell).
#: `acquire()` has no name pinning, so every other pool name goes to
#: `exclude` — deterministic routing to the linked entry, not LRU luck.
#: `maxWaitMs: 0` makes a rate-limited entry fail fast instead of waiting
#: out a reset inside a dispatch timeout. The entry's own `proxy` column is
#: what the client dials — verified in accountPool.js clientFor():
#: `proxy: row.proxy || clientOptions.proxy`.
_RUNNER = r"""
const [repo, poolPath, linked, verb, payloadPath] = process.argv.slice(2);
const http = await import(repo + '/src/scrapers/twitter/http/index.js');
const payload = JSON.parse(await (await import('node:fs')).readFileSync(payloadPath, 'utf8'));
const pool = http.createAccountPool({ storePath: poolPath });
let out;
try {
  const others = pool.list().map((a) => a.name).filter((n) => n !== linked);
  const lease = await pool.acquire('CreateTweet', { exclude: others, maxWaitMs: 0 });
  try {
    const client = lease.client;
    let result;
    if (verb === 'post') result = await http.postTweet(client, payload.text);
    else if (verb === 'reply') result = await http.replyToTweet(client, payload.tweetId, payload.text);
    else if (verb === 'quote') result = await http.quoteTweet(client, payload.tweetId, payload.text);
    else if (verb === 'thread') result = await http.postThread(client, payload.texts.map((t) => ({ text: t })));
    else throw new Error('unknown verb ' + verb);
    const one = (r) => ({ id: r?.rest_id ?? r?.legacy?.id_str ?? r?.tweet?.rest_id ?? null });
    out = { ok: true, tweets: Array.isArray(result) ? result.map(one) : [one(result)] };
  } finally {
    lease.release();
  }
} catch (err) {
  out = { ok: false, name: err?.name || 'Error', message: String(err?.message || err).slice(0, 300), status: err?.status ?? null };
} finally {
  pool.close();
}
process.stdout.write(JSON.stringify(out));
"""


def xactions_repo() -> Optional[Path]:
    """Repo root holding the HTTP lane, or None when unresolvable.

    `XACTIONS_REPO` wins; else the checkout next to this repo
    (`../XActions`), which is where the bridge contract locates it.
    """
    override = os.environ.get("XACTIONS_REPO")
    if override:
        root = Path(override)
    else:
        root = Path(__file__).resolve().parents[2].parent / "XActions"
    if (root / "src" / "scrapers" / "twitter" / "http" / "index.js").is_file():
        return root
    return None


def _node() -> str:
    node = shutil.which("node")
    if not node:
        raise ReachError(NEED_SETUP, "xactions fallback lane needs node on PATH")
    return node


def subprocess_run(argv, **kwargs):
    """The subprocess seam — tests stub this; production calls subprocess.run."""
    return subprocess.run(argv, **kwargs)


def _tweet_id(target: str) -> str:
    """Numeric id from an X status URL or a raw id — BAD_INPUT otherwise."""
    target = (target or "").strip()
    if target.isdigit():
        return target
    marker = "/status/"
    at = target.find(marker)
    if at != -1:
        tail = target[at + len(marker):].split("/", 1)[0].split("?", 1)[0]
        if tail.isdigit():
            return tail
    raise ReachError(BAD_INPUT, f"not an X tweet URL or id: {target[:120]}")


def _payload(verb: str, params: dict) -> dict:
    if verb == "thread":
        texts = [str(t) for t in (params.get("texts") or [])]
        if len(texts) < 2:
            raise ReachError(BAD_INPUT, "thread needs at least 2 tweet texts")
        return {"texts": texts}
    text = str(params.get("text") or "")
    if not text.strip():
        raise ReachError(BAD_INPUT, "tweet text is empty")
    if verb == "post":
        return {"text": text}
    return {"text": text, "tweetId": _tweet_id(str(params.get("url") or ""))}


def _classify_lane_error(raw: dict) -> ReachError:
    """Map the child's error JSON to a ReachError.

    Kept off RATE_LIMITED/PLATFORM_BLOCKED semantics only where the lane
    itself says so; a dead doc-id on the fallback lane stays
    PERSISTED_QUERY_DEAD so it is never retried or paused.
    """
    message = str(raw.get("message") or "xactions lane failed")
    # The child echoes proxy URLs (e.g. "proxy ... http://user:pass@host") —
    # strip userinfo before it reaches the JSON envelope.
    message = re.sub(r"://[^/\s:]+:[^@\s]+@", "://", message)
    lowered = message.lower()
    status = raw.get("status")
    if status == 429 or "rate limit" in lowered or "rate-limited" in lowered:
        return ReachError(RATE_LIMITED, f"xactions lane: {message}")
    if status in (401, 403) or any(w in lowered for w in ("auth", "login", "csrf", "suspend")):
        return ReachError(NEED_LOGIN, f"xactions lane rejected the session: {message}")
    if "query" in lowered and ("not found" in lowered or "404" in lowered):
        return ReachError(PERSISTED_QUERY_DEAD, f"xactions lane: {message}")
    if "proxy" in lowered or "econnrefused" in lowered:
        return ReachError(PLATFORM_BLOCKED, f"xactions lane egress failed: {message}")
    return ReachError(UPSTREAM_BROKEN, f"xactions lane: {message}")


def _operator_home() -> Path:
    """HOME as the operator sees it. Inside a dispatch env window HOME is the
    account's private home; REACH_FARM_ROOT (<home>/.agent-reach/farm) is the
    one pin that still points at the operator tree."""
    farm_root = os.environ.get("REACH_FARM_ROOT")
    if farm_root:
        return Path(farm_root).parent.parent
    return home_dir()


def _pool_db() -> Path:
    """The operator-side pool store — `XACTIONS_HOME` wins, else
    `<operator home>/.xactions/accounts.db` (bridge.pool_path() under the
    operator HOME). Bridge calls that resolve pool_path() directly would read
    the ACCOUNT home inside a dispatch window, so the lane resolves its own."""
    override = os.environ.get("XACTIONS_HOME")
    return (Path(override) if override else _operator_home() / ".xactions") / "accounts.db"


def _pool_entry(pool_db: Path, xactions_id: str) -> Optional[dict]:
    """Read-only peek at the pool row. Returns None on any failure."""
    try:
        conn = sqlite3.connect(f"file:{pool_db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT locked, cookies, proxy FROM accounts WHERE name = ?",
                (xactions_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {"locked": bool(row[0]), "has_session": bool(row[1]), "proxy": row[2]}


def run(verb: str, params: dict, *, timeout: float = _DEFAULT_TIMEOUT,
        runner: str = _RUNNER, invoke=None) -> list:
    """Replay one twitter write verb through the XActions HTTP lane.

    `invoke(argv, env, timeout)` replaces the node subprocess (tests stub it);
    it must return an object with returncode/stdout/stderr like subprocess.run.
    """
    if verb not in FALLBACK_VERBS:
        raise ReachError(BAD_INPUT, f"xactions lane has no verb {verb!r}")
    account = os.environ.get("REACH_ACCOUNT")
    if not account:
        raise ReachError(NEED_SETUP, "xactions fallback requires REACH_ACCOUNT")
    xactions_id = bridge.resolve(account)
    if not xactions_id:
        raise ReachError(NEED_SETUP, f"account {account} has no xactions link")
    pool_db = _pool_db()
    entry = _pool_entry(pool_db, xactions_id)
    if entry is None or entry["locked"] or not entry["has_session"]:
        raise ReachError(NEED_LOGIN, f"xactions pool entry {xactions_id} has no usable session")
    repo = xactions_repo()
    if repo is None:
        raise ReachError(NEED_SETUP, "XActions repo not found (set XACTIONS_REPO)")
    payload = _payload(verb, params)

    # The dispatch window swapped HOME to the account home; the lane's child
    # gets the operator home so XActions resolves XACTIONS_HOME/caches there.
    env = dict(os.environ)
    env["HOME"] = str(_operator_home())

    with tempfile.TemporaryDirectory(prefix="reach-xactions-") as work:
        payload_path = Path(work) / "payload.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        argv = [_node(), "--input-type=module", "-e", runner,
                str(repo), str(pool_db), xactions_id, verb, str(payload_path)]
        call = invoke or subprocess_run
        try:
            proc = call(argv, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            raise ReachError(UPSTREAM_BROKEN, f"xactions lane timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise ReachError(NEED_SETUP, f"cannot run node for the xactions lane: {exc}") from exc
    if proc.returncode != 0 and not proc.stdout.strip():
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
        raise ReachError(UPSTREAM_BROKEN, f"xactions lane exited {proc.returncode}: {tail[0][:300]}")
    try:
        parsed = json.loads(proc.stdout.strip()[:_MAX_OUTPUT])
    except ValueError as exc:
        raise ReachError(UPSTREAM_BROKEN, "xactions lane returned no JSON result") from exc
    if not isinstance(parsed, dict) or not parsed.get("ok"):
        raise _classify_lane_error(parsed if isinstance(parsed, dict) else {})

    items = []
    for tweet in parsed.get("tweets") or []:
        tid = str((tweet or {}).get("id") or "")
        if not tid:
            raise ReachError(UPSTREAM_BROKEN, "xactions lane posted but returned no tweet id")
        items.append(make_item(
            title=str(payload.get("text") or (payload.get("texts") or [""])[0])[:120],
            url=f"https://x.com/i/web/status/{tid}",
            text="Posted via xactions",
            raw={"tweetId": tid, "via": "xactions-fallback"},
        ))
    if not items:
        raise ReachError(UPSTREAM_BROKEN, "xactions lane returned no tweets")
    return items

"""Doc-id sync: Reach `query-ids.json` → XActions query-id cache (story 16.2).

Single source of truth: Reach. `query_ids` is what the write lanes replay and
what `doctor --probe-docids` probes, so a rotation is detected and edited
there first. XActions resolves every GraphQL call as
cache (`$XACTIONS_HOME/query-ids.json`) THEN hardcoded table
(`queryIds.js` `resolveOperation`): its cache wins over the bundled ids,
which is exactly the "endpoint.url" store the ticket names — XActions has no
separate endpoint.url file; `endpoints.js` builds the URL from the query id
`resolveOperation` returns.

Sync is one direction only (Reach → XActions), per operation name, and only
for the `x` section. Facebook doc-ids are Reach-only and never copied.
Existing XActions cache entries for operations Reach does not list are kept,
so a sync never shrinks what XActions discovered on its own.

Dry-run is the default: `plan()` only reports the diff. `apply()` writes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import query_ids, xactions_bridge as bridge

_CACHE_FILENAME = "query-ids.json"


def xactions_cache_path() -> Path:
    override = os.environ.get("XACTIONS_HOME")
    root = Path(override) if override else bridge.xactions_home()
    return root / _CACHE_FILENAME


def _reach_x_ids() -> dict:
    """The `x` section of Reach's query-ids.json: {operation: id}."""
    section = query_ids.load().get("x") or {}
    return {
        name: str(value).strip()
        for name, value in section.items()
        if isinstance(name, str) and not name.startswith("_")
        and isinstance(value, (str, int)) and str(value).strip()
    }


def _read_cache(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def plan() -> dict:
    """Diff Reach's X doc-ids against the XActions cache. Nothing is written."""
    wanted = _reach_x_ids()
    path = xactions_cache_path()
    cache = _read_cache(path)
    operations = cache.get("operations") if isinstance(cache.get("operations"), dict) else {}
    changes = []
    for name, doc_id in sorted(wanted.items()):
        current = (operations.get(name) or {}).get("queryId") if isinstance(operations.get(name), dict) else None
        if current != doc_id:
            changes.append({"operation": name, "from": current, "to": doc_id})
    return {"cache": str(path), "changes": changes, "unchanged": sorted(set(wanted) - {c["operation"] for c in changes})}


def apply() -> dict:
    """Write Reach's X doc-ids into the XActions cache. Returns the plan applied."""
    diff = plan()
    if not diff["changes"]:
        return diff
    path = xactions_cache_path()
    cache = _read_cache(path)
    operations = cache.get("operations") if isinstance(cache.get("operations"), dict) else {}
    for change in diff["changes"]:
        entry = operations.get(change["operation"])
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry["queryId"] = change["to"]
        entry.setdefault("operationType", "mutation")
        operations[change["operation"]] = entry
    cache["operations"] = operations
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return diff

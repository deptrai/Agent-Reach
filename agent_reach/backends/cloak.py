"""Install/locate the shared browser runtime. Website extraction belongs upstream."""
from __future__ import annotations

import json
import os
from pathlib import Path

from agent_reach.runtime.result import NEED_SETUP, UPSTREAM_BROKEN, ReachError


def setup() -> dict:
    from agent_reach.runtime import state, tools
    from agent_reach.runtime.run import run_cmd

    state.remember_path(state.root() / "cloak-binaries", "dedicated browser binaries")
    tools.ensure("cloakbrowser")
    exe = tools.resolve("cloakbrowser")
    if not exe:
        raise ReachError(NEED_SETUP, "CloakBrowser executable is missing")
    python = Path(exe).parent / ("python.exe" if os.name == "nt" else "python")
    env = {**tools.install_env(), "CLOAKBROWSER_AUTO_UPDATE": "false",
           "CLOAKBROWSER_CACHE_DIR": str(state.root() / "cloak-binaries"),
           "PYTHONPATH": str(Path(__file__).parents[2])}
    try:
        run_cmd([exe, "install"], timeout=180, env=env)
        result = json.loads(run_cmd([str(python), "-m", "agent_reach.backends.cloak"], timeout=20, env=env))
    except ReachError as exc:
        raise ReachError(exc.code, f"CloakBrowser installation/download failed: {exc.message}",
                         hint=exc.hint or "Reach will use installed Chrome when available. Use browser setup --retry-cloak to retry CloakBrowser.") from exc
    except (ValueError, OSError) as exc:
        raise ReachError(UPSTREAM_BROKEN, f"CloakBrowser installation/download failed: {exc}",
                         hint="Reach will use installed Chrome when available. Use browser setup --retry-cloak to retry CloakBrowser.") from exc
    return {**result, "python": str(python), "backend": "cloakbrowser"}


if __name__ == "__main__":
    # cloakbrowser 0.7.x dropped PLATFORM_CHROMIUM_VERSIONS — locate the installed
    # binary under the cache dir instead of relying on its internal API.
    root = Path(os.environ.get("CLOAKBROWSER_CACHE_DIR", str(Path.home() / ".cloakbrowser")))
    for d in sorted(root.glob("chromium-*"), key=lambda x: x.stat().st_mtime, reverse=True):
        for rel in ("Chromium.app/Contents/MacOS/Chromium", "chrome", "Chromium", "chrome.exe"):
            binary = d / rel
            if binary.is_file():
                print(json.dumps({"executable": str(binary),
                                  "version": d.name.removeprefix("chromium-")}))
                raise SystemExit(0)
    raise SystemExit("CloakBrowser binary is missing")

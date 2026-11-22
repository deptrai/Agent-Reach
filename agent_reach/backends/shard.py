"""ShardBrowser backend for farm containers (spike 14.1 verdict: shardx 2.1.7, MIT).

Optional dependency — `pip install shardx==2.1.7` is baked into the farm
container image; host installs keep CloakBrowser unchanged. Selected via
`REACH_BROWSER_BACKEND=shard`. C7: versions pin here and in the Dockerfile;
upgrades happen by rebuilding the image, never at runtime.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

from agent_reach.runtime.result import NEED_SETUP, UPSTREAM_BROKEN, ReachError

SHARDX_VERSION = "2.1.7"
# Patched Chromium build shardx 2.1.7 installed during the spike probe.
ENGINE_VERSION = "158.0.8127.36"
_CDP_WAIT = 90.0


def _shardx():
    try:
        import shardx
    except ImportError as exc:
        raise ReachError(
            NEED_SETUP, "shardx is not installed",
            hint=f"pip install shardx=={SHARDX_VERSION} (already baked into the farm container image).",
        ) from exc
    return shardx


def find_executable() -> Optional[str]:
    """Locate the shardx-patched Chromium binary under its per-user cache dir.

    The SDK's own ``Runtime.binary_path`` is authoritative (``ShardX-Linux/chrome``
    under ``~/.cache/shardx-sdk``); the walk below only covers a relocated cache.
    """
    try:
        path = _shardx().Runtime().binary_path
        if path.is_file():
            return str(path)
    except Exception:  # noqa: BLE001 - shardx absent or unsupported host
        pass
    roots = [
        os.environ.get("SHARDX_CACHE_DIR"),
        Path.home() / ".cache" / "shardx-sdk",
        Path.home() / ".local" / "share" / "shardx-sdk",
        Path.home() / ".shardx",
        Path.home() / "Library" / "Application Support" / "shardx-sdk",
    ]
    names = {"chrome", "chromium", "headless_shell", "ShardX", "ShardX Browser", "chrome.exe"}
    for root in roots:
        if not root:
            continue
        root = Path(root)
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.name in names and path.is_file() and os.access(path, os.X_OK):
                return str(path)
    return None


def install() -> dict:
    """Fetch the pinned patched-Chromium build into the shardx cache (image build only).

    ``Runtime.install()`` returns None and raises on failure; success is the
    binary landing where ``Runtime.binary_path`` points.
    """
    shardx = _shardx()
    shardx.Runtime().install()
    exe = find_executable()
    return {"installed": exe is not None, "executable": exe, "engine": ENGINE_VERSION}


def setup() -> dict:
    """Parity with cloak.setup(): resolve (installing when absent) and report the backend."""
    shardx = _shardx()
    exe = find_executable()
    if not exe:
        install()
        exe = find_executable()
    if not exe:
        raise ReachError(
            NEED_SETUP, "ShardBrowser executable is missing",
            hint="Run `python -m agent_reach.backends.shard install` (the farm image does this at build).",
        )
    return {"executable": exe, "version": getattr(shardx, "__version__", SHARDX_VERSION),
            "engine": ENGINE_VERSION, "python": sys.executable, "backend": "shardbrowser"}


def _sdk(shardx, profile_dir):
    """ShardX bound so its profile store lives inside the account volume."""
    if profile_dir:
        return shardx.ShardX(profiles_dir=str(Path(profile_dir) / "shardx-profiles"))
    return shardx.ShardX()


# Desktop-only random pick: an Android fingerprint makes sites serve mobile
# layouts, which breaks DOM-based adapters (opencli read lanes).
_DESKTOP_PLATFORMS = {"darwin": "macOS", "linux": "Linux", "win32": "Windows"}


def _create_profile(sdk, template: Optional[str]):
    """Create a fingerprint profile — explicit template, or desktop-only random.

    `create_profile(None)` picks from the whole library including ~50 Android
    fingerprints; pages then render mobile DOM that opencli adapters cannot
    parse. Filtering by the host platform keeps fingerprints plausible too.
    """
    if template is not None:
        return sdk.create_profile(template)
    platform = _DESKTOP_PLATFORMS.get(sys.platform, "Windows")
    return sdk.create_profile(None, platform=platform)


def _profile(sdk, profile_dir, template: Optional[str]):
    """Reopen the frozen fingerprint profile, or create + memoise one.

    The profile id is written once to ``<profile_dir>/shardx-profile-id`` so an
    account keeps one fingerprint across container restarts. `template` (env
    REACH_BROWSER_PROFILE) names a bundled fingerprint template; None picks a
    desktop-only random one the first time only.
    """
    if not profile_dir:
        return _create_profile(sdk, template)
    id_file = Path(profile_dir) / "shardx-profile-id"
    if id_file.is_file():
        saved = id_file.read_text(encoding="utf-8").strip()
        if saved:
            return sdk.open_profile(saved)
    profile = _create_profile(sdk, template)
    id_file.parent.mkdir(parents=True, exist_ok=True)
    id_file.write_text(profile.id, encoding="utf-8")
    return profile


def _wait_cdp(port: int, seconds: float) -> bool:
    """Wait for the DevTools endpoint; loopback must never go through the proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2):
                return True
        except Exception:  # noqa: BLE001 - browser still starting
            time.sleep(0.5)
    return False


def launch(url: str = "about:blank", *, headless: bool = True, proxy: Optional[str] = None,
           cdp_port: int = 9222, extension_dir=None, profile_dir=None,
           template: Optional[str] = None, wait: float = _CDP_WAIT):
    """Launch ShardBrowser via the SDK call verified in spike 14.1; return BrowserSession.

    `ShardX(profiles_dir=...).launch(profile, proxy=..., cdp=True, headless=...,
    extra_args=[...])` where `profile` is a frozen `Profile` from
    `create_profile()`/`open_profile()` — shardx 2.1.7 refuses to launch a
    template string directly. The extension and CDP port ride in extra_args,
    the proxy is the SDK's own param (engine kills QUIC + forces WebRTC
    tcp_only itself).
    """
    shardx = _shardx()
    extra_args = [
        f"--remote-debugging-port={cdp_port}",
        "--remote-debugging-address=127.0.0.1",
    ]
    ext = Path(extension_dir) if extension_dir else None
    if ext and (ext / "manifest.json").is_file():
        extra_args += [f"--disable-extensions-except={ext}", f"--load-extension={ext}"]
    if os.environ.get("REACH_BROWSER_NO_SANDBOX") == "1":
        # Container kernels often lack the userns features Chromium's sandbox wants.
        extra_args += ["--no-sandbox", "--disable-setuid-sandbox"]
    sdk = _sdk(shardx, profile_dir)
    session = sdk.launch(
        _profile(sdk, profile_dir, template),
        proxy=proxy or None,
        cdp=True,
        headless=headless,
        extra_args=extra_args,
    )
    if not _wait_cdp(cdp_port, wait):
        close_session(session)
        raise ReachError(UPSTREAM_BROKEN, f"ShardBrowser did not open CDP :{cdp_port} within {wait:.0f}s")
    return session


def close_session(session) -> None:
    """Terminate a BrowserSession; tolerate whichever close API the SDK ships."""
    for attr in ("close", "stop", "quit"):
        fn = getattr(session, attr, None)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
            return
    proc = getattr(session, "process", None)
    if proc is not None:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def main(argv) -> int:
    cmd = argv[0] if argv else "install"
    if cmd == "install":
        print(json.dumps(install()))
        return 0
    if cmd == "version":
        shardx = _shardx()
        print(json.dumps({"shardx": getattr(shardx, "__version__", SHARDX_VERSION),
                          "engine": ENGINE_VERSION, "executable": find_executable()}))
        return 0
    if cmd == "path":
        print(find_executable() or "")
        return 0
    print("usage: python -m agent_reach.backends.shard [install|version|path]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

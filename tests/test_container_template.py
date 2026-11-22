"""Static contract for the farm container template (story 14.2) + shard backend.

No Docker daemon or real browser needed: the Dockerfile/compose/entrypoint are
checked as text, and the shard backend is exercised with a stubbed shardx SDK.
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

from agent_reach.backends import shard
from agent_reach.runtime.result import NEED_SETUP, UPSTREAM_BROKEN, ReachError

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "farm" / "container"
DOCKERFILE = (TEMPLATE / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (TEMPLATE / "docker-compose.yml").read_text(encoding="utf-8")
ENTRYPOINT = (TEMPLATE / "entrypoint.sh").read_text(encoding="utf-8")


def test_template_files_exist():
    for name in ("Dockerfile", "docker-compose.yml", "entrypoint.sh",
                 "Dockerfile.dockerignore", "README.md", ".env.example", "verify.sh"):
        assert (TEMPLATE / name).is_file(), name


def test_dockerfile_pins_every_version():
    # C7: pin at build time — shardx, opencli, node (+sha256), base image tag.
    assert 'SHARDX_VERSION=2.1.7' in DOCKERFILE
    assert 'OPENCLI_VERSION=2.0.3' in DOCKERFILE
    assert 'NODE_VERSION=22.11.0' in DOCKERFILE
    assert re.search(r"NODE_SHA256_X64=[0-9a-f]{64}", DOCKERFILE)
    assert re.search(r"NODE_SHA256_ARM64=[0-9a-f]{64}", DOCKERFILE)
    assert "python:3.12.7-slim-bookworm" in DOCKERFILE
    assert "agent-reach-farm:1.6.1" in DOCKERFILE


def test_dockerfile_has_no_latest_or_unpinned_installs():
    assert ":latest" not in DOCKERFILE
    for line in DOCKERFILE.splitlines():
        code = line.split("#", 1)[0]
        for match in re.finditer(r"pip install[ \t]+(?:--\S+[ \t]+)*([\"']?)([^\s\"']+)", code):
            spec = match.group(2).strip("'\"")
            assert spec == "." or "==" in spec, f"unpinned pip install: {spec}"
        for match in re.finditer(r"npm install -g[ \t]+[\"']?([^\s\"']+)", code):
            assert "@" in match.group(1).split("/", 1)[-1], f"unpinned npm install: {match.group(1)}"


def test_dockerfile_bakes_chromium_and_extension_at_build_time():
    assert "python -m agent_reach.backends.shard install" in DOCKERFILE
    assert "releases/tags/v" in DOCKERFILE  # extension from the pinned release, not latest
    assert "releases/latest" not in DOCKERFILE


def test_dockerfile_selects_shard_backend_and_isolated_home():
    assert "REACH_BROWSER_BACKEND=shard" in DOCKERFILE
    assert "HOME=/home/farm" in DOCKERFILE
    assert "USER farm" in DOCKERFILE


def test_compose_contract():
    assert "mem_limit: 1536m" in COMPOSE
    assert "REACH_ACCOUNT" in COMPOSE
    assert "REACH_BROWSER_PROXY" in COMPOSE
    assert "/home/farm/.agent-reach" in COMPOSE
    assert "network_mode" not in COMPOSE  # 14.3 chose in-netns iptables, not a network swap


def test_entrypoint_health_probe():
    assert "agent-reach check" in ENTRYPOINT
    assert 'exec "$@"' in ENTRYPOINT
    assert "FARM_BOOT_CHECK" in ENTRYPOINT


def _fake_shardx(calls: dict):
    class FakeSession:
        process = None

    class FakeProfile:
        def __init__(self, template):
            self.template = template
            self.id = "frozen-id"

    class FakeShardX:
        def __init__(self, profiles_dir=None):
            calls["profiles_dir"] = profiles_dir

        def create_profile(self, template=None, *, platform=None):
            return FakeProfile(template or f"random:{platform}")

        def open_profile(self, pid):
            return FakeProfile(f"reopened:{pid}")

        def launch(self, profile, **kwargs):
            calls["profile"] = profile
            calls.update(kwargs)
            return FakeSession()

    return types.SimpleNamespace(
        ShardX=FakeShardX,
        Runtime=lambda: types.SimpleNamespace(install=lambda: {"installed": True}),
        __version__="2.1.7")


def test_shard_launch_constructs_spike_verified_args(monkeypatch, tmp_path):
    calls: dict = {}
    monkeypatch.setitem(sys.modules, "shardx", _fake_shardx(calls))
    monkeypatch.setattr(shard, "_wait_cdp", lambda port, seconds: True)
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "manifest.json").write_text("{}", encoding="utf-8")
    profile = tmp_path / "profile"

    session = shard.launch(
        headless=True, proxy="http://127.0.0.1:19876", cdp_port=19898,
        extension_dir=ext, profile_dir=profile, template="linux-x")

    assert calls["proxy"] == "http://127.0.0.1:19876"
    assert calls["cdp"] is True
    assert calls["headless"] is True
    assert calls["profiles_dir"] == str(profile / "shardx-profiles")
    args = calls["extra_args"]
    assert "--remote-debugging-port=19898" in args
    assert "--remote-debugging-address=127.0.0.1" in args
    assert f"--load-extension={ext}" in args
    assert f"--disable-extensions-except={ext}" in args
    assert calls["profile"].template == "linux-x"
    assert (profile / "shardx-profile-id").read_text(encoding="utf-8") == "frozen-id"
    assert session is not None


def test_shard_launch_no_sandbox_only_when_env_set(monkeypatch, tmp_path):
    calls: dict = {}
    monkeypatch.setitem(sys.modules, "shardx", _fake_shardx(calls))
    monkeypatch.setattr(shard, "_wait_cdp", lambda port, seconds: True)
    monkeypatch.delenv("REACH_BROWSER_NO_SANDBOX", raising=False)
    shard.launch(headless=True, cdp_port=9222, profile_dir=tmp_path)
    assert "--no-sandbox" not in calls["extra_args"]
    monkeypatch.setenv("REACH_BROWSER_NO_SANDBOX", "1")
    shard.launch(headless=True, cdp_port=9222, profile_dir=tmp_path)
    assert "--no-sandbox" in calls["extra_args"]


def test_shard_launch_cdp_timeout_closes_session(monkeypatch, tmp_path):
    calls: dict = {}
    monkeypatch.setitem(sys.modules, "shardx", _fake_shardx(calls))
    monkeypatch.setattr(shard, "_wait_cdp", lambda port, seconds: False)
    closed = []
    monkeypatch.setattr(shard, "close_session", lambda s: closed.append(s))
    with pytest.raises(ReachError) as err:
        shard.launch(headless=True, cdp_port=9222, profile_dir=tmp_path)
    assert err.value.code == UPSTREAM_BROKEN
    assert closed, "session must be closed when CDP never comes up"


def test_shard_setup_requires_shardx(monkeypatch):
    real_import = __import__

    def no_shardx(name, *args, **kwargs):
        if name == "shardx":
            raise ImportError("no module named shardx")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "shardx", raising=False)
    monkeypatch.setattr("builtins.__import__", no_shardx)
    with pytest.raises(ReachError) as err:
        shard.setup()
    assert err.value.code == NEED_SETUP

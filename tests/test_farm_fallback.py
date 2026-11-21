# -*- coding: utf-8 -*-
"""Fallback routing to the xactions HTTP lane (story 16.2).

A twitter write that dies with PERSISTED_QUERY_DEAD (or an upstream schema
error) is replayed once through the xactions lane. Everything else —
RATE_LIMITED, QUOTA_EXCEEDED, a dead proxy — keeps its own semantics and the
lane is never called. The lane subprocess is stubbed; nothing here posts.
"""

import json
from contextlib import closing

import pytest

from agent_reach.channels.base import Action, Channel, Param
from agent_reach.runtime import farm
from agent_reach.runtime import xactions_bridge as bridge
from agent_reach.runtime import xactions_docids, xactions_lane
from agent_reach.runtime.dispatch import run_action
from agent_reach.runtime.result import (
    PERSISTED_QUERY_DEAD,
    PLATFORM_BLOCKED,
    QUOTA_EXCEEDED,
    RATE_LIMITED,
    ReachError,
    make_item,
)

PROXY = "http://p1.example:8080"


class _X(Channel):
    name = "twitter"
    level = "optional"
    actions = (
        Action("post", "w", (Param("text", "t"),), (("probe", "_post"),)),
        Action("reply", "w", (Param("url", "u"), Param("text", "t")), (("probe", "_post"),)),
        Action("search", "r", (Param("query", "q"),), (("probe", "_post"),)),
    )

    def __init__(self, fail=None):
        self.fail = fail
        self.calls = 0

    def can_handle(self, url):
        return False

    def _post(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise ReachError(self.fail, "probe failure")
        return [make_item(title="ok", url="https://x.com/i/status/1")]


@pytest.fixture(autouse=True)
def _fresh_proxy_verdicts():
    """The kill-switch caches proxy verdicts ~60s process-wide; tests must
    not inherit a dead verdict a sibling test planted."""
    from agent_reach.runtime import dispatch

    dispatch._PROXY_VERDICTS.clear()
    yield
    dispatch._PROXY_VERDICTS.clear()


@pytest.fixture(autouse=True)
def xactions_home(tmp_path, monkeypatch):
    home = tmp_path / "xactions"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("XACTIONS_HOME", str(home))
    monkeypatch.setenv("XACTIONS_REPO", str(tmp_path))  # no real repo needed: invoke is stubbed
    (tmp_path / "src" / "scrapers" / "twitter" / "http").mkdir(parents=True)
    (tmp_path / "src" / "scrapers" / "twitter" / "http" / "index.js").write_text("")
    return home


def _ready_account():
    farm.add_account("a1", PROXY)
    bridge.link("a1")
    bridge.provision_session("a1", "auth_token=tok; ct0=csrf")


class _Proc:
    def __init__(self, stdout):
        self.returncode, self.stdout, self.stderr = 0, stdout, ""


def _stub(monkeypatch, captured, stdout='{"ok": true, "tweets": [{"id": "555"}]}'):
    def invoke(argv, **kwargs):
        captured.append(kwargs.get("env") or {})
        return _Proc(stdout)

    monkeypatch.setattr(xactions_lane, "subprocess_run", invoke)


def _post(channel, monkeypatch, text="hello"):
    monkeypatch.setenv("REACH_ACCOUNT", "a1")
    return run_action(channel, "post", {"text": text})


def test_persisted_query_dead_routes_to_lane(monkeypatch):
    _ready_account()
    captured = []
    _stub(monkeypatch, captured)
    out = _post(_X(fail=PERSISTED_QUERY_DEAD), monkeypatch)
    assert out["ok"], out
    assert out["backend"] == "xactions-fallback"
    assert out["items"][0]["url"] == "https://x.com/i/web/status/555"
    assert captured, "lane was not invoked"


def test_upstream_schema_error_routes_to_lane(monkeypatch):
    _ready_account()
    captured = []
    _stub(monkeypatch, captured)

    class _Schema(_X):
        def _post(self, **kwargs):
            raise ReachError("UPSTREAM_BROKEN", "CreateTweet failed: schema field not defined")

    out = _post(_Schema(), monkeypatch)
    assert out["ok"] and captured


def test_rate_limited_does_not_fall_back(monkeypatch):
    _ready_account()
    captured = []
    _stub(monkeypatch, captured)
    out = _post(_X(fail=RATE_LIMITED), monkeypatch)
    assert not out["ok"] and out["error"]["code"] == RATE_LIMITED
    assert captured == []


def test_quota_exceeded_never_reaches_lane(monkeypatch):
    _ready_account()
    for _ in range(30):
        farm.record_action("a1", "post", "success")
    captured = []
    _stub(monkeypatch, captured)
    out = _post(_X(fail=PERSISTED_QUERY_DEAD), monkeypatch)
    assert not out["ok"] and out["error"]["code"] == QUOTA_EXCEEDED
    assert captured == []


def test_no_link_surfaces_original_error(monkeypatch):
    farm.add_account("a1", PROXY)
    captured = []
    _stub(monkeypatch, captured)
    out = _post(_X(fail=PERSISTED_QUERY_DEAD), monkeypatch)
    assert not out["ok"] and out["error"]["code"] == PERSISTED_QUERY_DEAD
    assert captured == []


def test_fallback_success_counts_toward_quota(monkeypatch):
    _ready_account()
    _stub(monkeypatch, [])
    monkeypatch.setenv("REACH_CAP_POST", "1")
    assert _post(_X(fail=PERSISTED_QUERY_DEAD), monkeypatch)["ok"]
    ledger = farm.ledger_24h("a1")
    assert ledger["by_class"]["post"]["success"] == 1
    again = _post(_X(fail=PERSISTED_QUERY_DEAD), monkeypatch)
    assert not again["ok"] and again["error"]["code"] == QUOTA_EXCEEDED


def test_dead_proxy_blocks_before_fallback(monkeypatch):
    _ready_account()
    captured = []
    _stub(monkeypatch, captured)
    monkeypatch.setattr(
        "agent_reach.runtime.dispatch._proxy_alive",
        lambda proxy: (_ for _ in ()).throw(OSError("connection refused")),
    )
    out = _post(_X(fail=PERSISTED_QUERY_DEAD), monkeypatch)
    assert not out["ok"] and out["error"]["code"] == PLATFORM_BLOCKED
    assert captured == []


def test_lane_child_gets_operator_home_not_account_home(monkeypatch):
    _ready_account()
    captured = []
    _stub(monkeypatch, captured)
    assert _post(_X(fail=PERSISTED_QUERY_DEAD), monkeypatch)["ok"]
    child_home = captured[0]["HOME"]
    account_home = farm.account_env("a1")["HOME"]
    assert child_home != account_home
    assert child_home == str(farm.farm_root().parent.parent)


def test_sync_docids_dry_run_then_apply(tmp_path, monkeypatch):
    reach = tmp_path / "home" / ".agent-reach"
    reach.mkdir(parents=True)
    (reach / "query-ids.json").write_text(json.dumps({"x": {"CreateTweet": "NEWDOCID"}}))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cache = xactions_docids.xactions_cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"operations": {"CreateTweet": {"queryId": "OLD", "operationType": "mutation"}}}))

    diff = xactions_docids.plan()
    assert diff["changes"] == [{"operation": "CreateTweet", "from": "OLD", "to": "NEWDOCID"}]
    assert json.loads(cache.read_text())["operations"]["CreateTweet"]["queryId"] == "OLD"  # dry-run wrote nothing

    applied = xactions_docids.apply()
    assert applied["changes"][0]["to"] == "NEWDOCID"
    assert json.loads(cache.read_text())["operations"]["CreateTweet"]["queryId"] == "NEWDOCID"
    assert xactions_docids.plan()["changes"] == []  # idempotent


def test_sync_keeps_xactions_only_operations(tmp_path, monkeypatch):
    reach = tmp_path / "home" / ".agent-reach"
    reach.mkdir(parents=True)
    (reach / "query-ids.json").write_text(json.dumps({"x": {"CreateTweet": "X"}, "facebook": {"post_create": "FB"}}))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cache = xactions_docids.xactions_cache_path()
    cache.write_text(json.dumps({"operations": {"SomeOtherOp": {"queryId": "KEEP"}}}))
    xactions_docids.apply()
    ops = json.loads(cache.read_text())["operations"]
    assert ops["SomeOtherOp"]["queryId"] == "KEEP"
    assert "post_create" not in ops  # facebook ids never cross

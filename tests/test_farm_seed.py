# -*- coding: utf-8 -*-
"""Farm account seed: onboarding copies the small runtime files, shares the
operator's binaries by path, and never leaks the cookie snapshot unless asked."""

import json
import os
import stat
from pathlib import Path

import pytest

from agent_reach.runtime import cli as unified
from agent_reach.runtime import farm, state
from agent_reach.runtime.result import NEED_SETUP, ReachError


def _operator_tree() -> Path:
    """Fake operator runtime under the isolated test HOME (state.root())."""
    root = state.root()
    (root / "cloak-binaries").mkdir(parents=True, exist_ok=True)
    (root / "cloak-binaries" / "license.key").write_text("cb-key-test")
    (root / "cloak-binaries" / ".free_claim").write_text("claimed")
    (root / "browser" / "extension").mkdir(parents=True, exist_ok=True)
    (root / "browser" / "extension" / "manifest.json").write_text('{"version": "9.9.9"}')
    exe = root / "cloak-binaries" / "chromium-1-pro" / "chrome"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!fake")
    st = {
        "backend": "cloakbrowser",
        "executable": str(exe),
        "launcher_python": "/op/tools/python",
        "extension_asset": "asset-x",
        "extension_id": "extid",
        "extension_version": "9.9.9",
        "profile_backend": "cloakbrowser",
        "headless": True,
        # volatile/session fields that must NOT be seeded:
        "opencli_context": "ctx-operator",
        "started_at": 1,
        "visited_origins": ["https://x.com"],
        "restored_cookies": True,
        "profile_backup": "/tmp/backup",
    }
    (root / "browser" / "state.json").write_text(json.dumps(st))
    (root / "browser" / "session.json").write_text('{"cookies": []}')
    return root


def _acct_home(name: str) -> Path:
    return Path(farm.account_env(name)["HOME"])


def test_add_seeds_small_files_and_sanitizes_state():
    _operator_tree()
    row = farm.add_account("a1", "http://p1:8080")
    home = _acct_home("a1")
    assert row["seed"]["seeded"], row
    # license + extension copied (0600)
    lic = home / ".agent-reach" / "cloak-binaries" / "license.key"
    assert lic.is_file() and lic.read_text() == "cb-key-test"
    assert stat.S_IMODE(lic.stat().st_mode) == 0o600
    assert (home / ".agent-reach" / "browser" / "extension" / "manifest.json").is_file()
    # state.json sanitized: runtime paths kept, volatile/session fields dropped
    st = json.loads((home / ".agent-reach" / "browser" / "state.json").read_text())
    assert st["backend"] == "cloakbrowser"
    assert st["executable"].endswith("chrome")
    assert st["extension_version"] == "9.9.9"
    for k in ("opencli_context", "started_at", "visited_origins",
              "restored_cookies", "profile_backup"):
        assert k not in st
    # session never cloned by default
    assert not (home / ".agent-reach" / "browser" / "session.json").exists()


def test_share_session_opt_in_copies_cookie_snapshot():
    _operator_tree()
    farm.add_account("a1", "http://p1:8080")
    out = farm.seed_account_home("a1", share_session=True)
    assert "browser/session.json" in out["seeded"]
    assert (Path(farm.account_env("a1")["HOME"]) / ".agent-reach"
            / "browser" / "session.json").is_file()


def test_no_seed_flag_skips_seed():
    _operator_tree()
    row = farm.add_account("a1", "http://p1:8080", seed=False)
    assert "seed" not in row
    home = _acct_home("a1")
    assert not (home / ".agent-reach").exists()


def test_seed_without_operator_runtime_is_noop():
    # isolated HOME has no .agent-reach/browser etc — seed must not explode
    row = farm.add_account("a1", "http://p1:8080")
    assert row["seed"]["seeded"] == []


def test_seed_unknown_account_needs_setup():
    with pytest.raises(ReachError) as excinfo:
        farm.seed_account_home("ghost")
    assert excinfo.value.code == NEED_SETUP


def test_cli_add_and_seed(capsys):
    _operator_tree()

    def _run(argv):
        code = unified.main(argv)
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 1
        return code, json.loads(lines[0])

    code, out = _run(["farm", "account", "add", "cli1", "--proxy", "http://p:1"])
    assert code == 0 and out["ok"]
    code, out = _run(["farm", "account", "seed", "cli1", "--share-session"])
    assert code == 0 and out["ok"]
    assert "browser/session.json" in out["result"]["seeded"]

    code, out = _run(["farm", "account", "add", "cli2", "--proxy", "http://p:1", "--no-seed"])
    assert code == 0 and out["ok"]
    assert "seed" not in out["result"]

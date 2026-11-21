"""Static contract for the netns hard kill-switch (story 14.3).

Contract §2 layer-2: egress outside proxy_i must not exist at the network
layer. No Docker daemon needed — the killswitch script, entrypoint chain,
compose wiring, Dockerfile and docs are checked as text. The live iptables
evidence (direct/direct-IP/DNS dead, only tcp proxy_ip:port allowed) is in
the story plan.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "farm" / "container"
SCRIPT_PATH = TEMPLATE / "netns-killswitch.sh"
SCRIPT = SCRIPT_PATH.read_text(encoding="utf-8")
ENTRYPOINT = (TEMPLATE / "entrypoint.sh").read_text(encoding="utf-8")
COMPOSE = (TEMPLATE / "docker-compose.yml").read_text(encoding="utf-8")
DOCKERFILE = (TEMPLATE / "Dockerfile").read_text(encoding="utf-8")
README = (TEMPLATE / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (TEMPLATE / ".env.example").read_text(encoding="utf-8")


def test_killswitch_script_exists_executable_and_strict():
    assert SCRIPT_PATH.is_file()
    assert os.access(SCRIPT_PATH, os.X_OK), "netns-killswitch.sh must be executable"
    assert SCRIPT.startswith("#!/bin/sh")
    assert "set -eu" in SCRIPT  # fail-closed: any error aborts container start


def test_killswitch_drops_all_non_proxy_egress():
    # Policy DROP + wipe-then-re-add (idempotent on container restart).
    assert "iptables -P OUTPUT DROP" in SCRIPT
    assert "iptables -F OUTPUT" in SCRIPT
    # Loopback allowed — the 127.0.0.1:19825 bridge and CDP ride this rule.
    assert "iptables -A OUTPUT -o lo -j ACCEPT" in SCRIPT
    # DNS + DoT dead against every resolver (docker embedded DNS included).
    for port in (53, 853):
        assert f"--dport {port} -j DROP" in SCRIPT


def test_killswitch_derives_allow_rule_from_proxy_env():
    # Only egress: tcp to the resolved proxy IP(s):port, parsed from
    # REACH_BROWSER_PROXY (fail-closed when the env is absent/unparseable).
    assert "REACH_BROWSER_PROXY" in SCRIPT
    assert "urlparse" in SCRIPT and "getaddrinfo" in SCRIPT
    assert '--dport "$PROXY_PORT" -j ACCEPT' in SCRIPT
    # DNS dies with the rules, so the proxy host is pinned into /etc/hosts.
    assert "/etc/hosts" in SCRIPT and "farm-netns-killswitch" in SCRIPT


def test_killswitch_ipv6_fail_closed():
    # An IPv6-capable netns without programmed rules is a leak path.
    assert "ip6tables -P OUTPUT DROP" in SCRIPT
    assert "/proc/net/if_inet6" in SCRIPT


def test_entrypoint_root_phase_applies_then_drops():
    # Root once: install rules, then re-exec as farm — nothing inside keeps
    # NET_ADMIN to undo the kill-switch.
    assert '"$(id -u)" = "0"' in ENTRYPOINT
    assert "farm-netns-killswitch" in ENTRYPOINT
    assert "runuser -u farm" in ENTRYPOINT
    # Non-root + killswitch requested = refuse to boot (fail closed).
    assert "FARM_KILLSWITCH" in ENTRYPOINT and "failing closed" in ENTRYPOINT


def test_compose_grants_root_once_and_net_admin():
    assert "user: root" in COMPOSE
    assert "NET_ADMIN" in COMPOSE
    assert "FARM_KILLSWITCH" in COMPOSE
    assert "FARM_KILLSWITCH" in ENV_EXAMPLE


def test_dockerfile_installs_iptables_and_ship_script():
    assert "iptables" in DOCKERFILE
    assert "netns-killswitch.sh" in DOCKERFILE
    assert "farm-netns-killswitch" in DOCKERFILE
    # Rules tool is in the tail, after the heavy cached layers; default
    # runtime user stays `farm` (compose escalates only at boot).
    assert DOCKERFILE.rindex("USER farm") > DOCKERFILE.index("netns-killswitch.sh")


def test_readme_and_verify_document_the_probes():
    assert "kill-switch" in README and "netns" in README
    assert "iptables -S OUTPUT" in README or "iptables -S OUTPUT" in (
        TEMPLATE / "verify.sh").read_text(encoding="utf-8")
    verify = (TEMPLATE / "verify.sh").read_text(encoding="utf-8")
    assert "--cap-add NET_ADMIN" in verify
    assert "--noproxy" in verify  # direct-IP probe must fail
    assert "getent hosts" in verify  # DNS-leak probe

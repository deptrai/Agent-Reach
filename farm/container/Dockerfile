# syntax=docker/dockerfile:1
# Farm container template — 1 account : 1 container (story 14.2).
# Browser vendor per spike 14.1 verdict: ShardBrowser (shardx, MIT).
# C7: EVERY version is pinned at build time — upgrades = rebuild, never runtime
# auto-update (CLOAKBROWSER_AUTO_UPDATE=false, no latest tags, no unpinned pip/npm).
#
# Build (from repo root) — linux/amd64 only: shardx ships mac-arm64/win-x64/
# linux-x64, and the farm host is x86_64 (Apple Silicon builds run emulated).
#   docker build --platform linux/amd64 -f farm/container/Dockerfile \
#     -t agent-reach-farm:1.6.1 --build-arg AGENT_REACH_SOURCE_SHA="$(git rev-parse HEAD)" .

FROM python:3.12.7-slim-bookworm

ARG NODE_VERSION=22.11.0
ARG NODE_SHA256_X64=83bf07dd343002a26211cf1fcd46a9d9534219aad42ee02847816940bf610a72
ARG NODE_SHA256_ARM64=6031d04b98f59ff0f7cb98566f65b115ecd893d3b7870821171708cdbaf7ae6e
ARG OPENCLI_VERSION=2.0.3
ARG SHARDX_VERSION=2.1.7
ARG AGENT_REACH_SOURCE_SHA=unknown

# Versions visible to build heredocs + inspectable inside the image (C7 evidence).
ENV NODE_VERSION=${NODE_VERSION} \
    NODE_SHA256_X64=${NODE_SHA256_X64} \
    NODE_SHA256_ARM64=${NODE_SHA256_ARM64} \
    OPENCLI_VERSION=${OPENCLI_VERSION} \
    SHARDX_VERSION=${SHARDX_VERSION} \
    AGENT_REACH_SOURCE_SHA=${AGENT_REACH_SOURCE_SHA}

# Shared libraries the patched Chromium links against (headless still needs them).
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
      libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 libx11-6 libxcb1 \
      libxext6 libxi6 libxtst6 libglib2.0-0 libdbus-1-3 libexpat1 \
      fonts-liberation \
      unzip \
    && rm -rf /var/lib/apt/lists/*

# Node.js — official dist tarball, sha256-verified (opencli is an npm CLI).
RUN python3 - <<'PY'
import hashlib, os, platform, tarfile, tempfile, urllib.request
ver = os.environ["NODE_VERSION"]
arch = {"x86_64": "x64", "aarch64": "arm64"}.get(platform.machine())
if arch is None:
    raise SystemExit(f"unsupported arch: {platform.machine()}")
want = os.environ[f"NODE_SHA256_{arch.upper()}"]
url = f"https://nodejs.org/dist/v{ver}/node-v{ver}-linux-{arch}.tar.xz"
with tempfile.NamedTemporaryFile(suffix=".tar.xz") as tmp:
    urllib.request.urlretrieve(url, tmp.name)
    got = hashlib.sha256(open(tmp.name, "rb").read()).hexdigest()
    if got != want:
        raise SystemExit(f"node sha256 mismatch: {got} != {want}")
    with tarfile.open(tmp.name) as tf:
        tf.extractall("/opt", filter="data")
os.rename(f"/opt/node-v{ver}-linux-{arch}", "/opt/node")
print(f"node {ver} ({arch}) sha256 OK")
PY
ENV PATH=/opt/node/bin:$PATH

# OpenCLI at the version runtime/tools.py declares `tested`; --ignore-scripts keeps
# install hooks from replacing the verified adapters (same guard as tools.py).
RUN npm install -g "@jackwener/opencli@${OPENCLI_VERSION}" --ignore-scripts \
    && opencli --version

# agent-reach from THIS source tree (pin = AGENT_REACH_SOURCE_SHA build arg) +
# shardx at the spike-pinned version.
WORKDIR /opt/agent-reach
COPY pyproject.toml README.md ./
COPY agent_reach ./agent_reach
RUN pip install --no-cache-dir . \
    && pip install --no-cache-dir "shardx==${SHARDX_VERSION}"

# OpenCLI browser extension, pinned to the matching release tag. Seeded into
# /opt/farm-seed — the entrypoint copies it into each account's HOME volume on
# first boot (runtime never hits GitHub; refresh_extension() sees it current).
RUN python3 - <<'PY'
import io, json, os, re, shutil, tempfile, urllib.request, zipfile
ver = os.environ["OPENCLI_VERSION"]
req = urllib.request.Request(
    f"https://api.github.com/repos/jackwener/OpenCLI/releases/tags/v{ver}",
    headers={"User-Agent": "agent-reach"})
rel = json.load(urllib.request.urlopen(req, timeout=30))
asset = next(a for a in rel.get("assets", []) if re.match(r"opencli-extension-.*\.zip$", a.get("name", "")))
data = urllib.request.urlopen(asset["browser_download_url"], timeout=120).read()
with tempfile.TemporaryDirectory() as work:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            target = os.path.realpath(os.path.join(work, info.filename))
            if not target.startswith(os.path.realpath(work) + os.sep):
                raise SystemExit(f"unsafe path in extension zip: {info.filename}")
        zf.extractall(work)
    manifest = None
    for base, _dirs, files in os.walk(work):
        if "manifest.json" in files:
            manifest = base
            break
    if manifest is None:
        raise SystemExit("no manifest.json in the extension zip")
    os.makedirs("/opt/farm-seed", exist_ok=True)
    shutil.move(manifest, "/opt/farm-seed/extension")
print(f"opencli extension seeded from release v{ver}: {asset['name']}")
PY
# The extracted tree lands mode-700 (TemporaryDirectory) — the entrypoint seeds
# it as uid `farm`, and under the 14.3 kill-switch there is no runtime
# re-download to fall back on, so the seed must be world-readable.
RUN chmod -R a+rX /opt/farm-seed

# Dedicated account home (HOME riêng per container; volume mounts .agent-reach).
RUN useradd --create-home --uid 1000 farm \
    && mkdir -p /home/farm/.agent-reach \
    && chown -R farm:farm /home/farm

USER farm
ENV HOME=/home/farm \
    REACH_BROWSER_BACKEND=shard \
    REACH_BROWSER_PORT=9222 \
    REACH_BROWSER_NO_SANDBOX=1 \
    CLOAKBROWSER_AUTO_UPDATE=false \
    PYTHONUNBUFFERED=1

# Patched Chromium downloaded at BUILD time (C7); records resolved versions
# in-image for audit. Lives under /home/farm/.local — outside the account volume.
RUN python -m agent_reach.backends.shard install \
    && python -m agent_reach.backends.shard version | tee /home/farm/farm-versions.json

# Story 14.3 — netns hard kill-switch. Kept in the tail on purpose: the heavy
# layers above stay cached when the scripts change. iptables (nf_tables on
# bookworm) is the egress enforcement tool — apt pin = base image tag.
# `user: root` + `cap_add: NET_ADMIN` come from compose/CLI at run time; the
# entrypoint applies the rules then drops to uid `farm` via runuser.
USER root
RUN apt-get update && apt-get install -y --no-install-recommends iptables curl \
    && rm -rf /var/lib/apt/lists/*
COPY --chmod=755 farm/container/entrypoint.sh /usr/local/bin/farm-entrypoint
COPY --chmod=755 farm/container/netns-killswitch.sh /usr/local/bin/farm-netns-killswitch
USER farm

# Boot probe = `agent-reach check` (JSON to stdout; exit code = health), then CMD.
ENTRYPOINT ["/usr/local/bin/farm-entrypoint"]
CMD ["sleep", "infinity"]

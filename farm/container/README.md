# Farm container template — 1 account : 1 container (story 14.2)

Image chứa agent-reach + OpenCLI + ShardBrowser, pin version tại build time
(C7: upgrade = rebuild, không auto-update). Mỗi account = 1 container: HOME
riêng, volume `~/.agent-reach` riêng, proxy riêng (`REACH_BROWSER_PROXY`),
cap RAM 1.5GB. Bridge `127.0.0.1:19825` chạy trong netns của chính container
— không đụng host.

Vendor theo verdict spike 14.1: **ShardBrowser** (`shardx==2.1.7`, MIT).
CloakBrowser giữ nguyên làm fallback host-local, không vào image này.

## Pins (C7)

| Thành phần | Pin |
|---|---|
| Base image | `python:3.12.7-slim-bookworm` |
| Node.js | `22.11.0` (tarball chính thức, verify sha256 trong build) |
| OpenCLI | `@jackwener/opencli@2.0.3` (`tested` trong `runtime/tools.py`) |
| shardx | `2.1.7` (cũng là extra `farm` trong `pyproject.toml`) |
| Chromium patched | engine `158.0.8127.36` — tải lúc **build** qua `Runtime().install()`, không lúc runtime |
| OpenCLI extension | release tag `v2.0.3`, seed vào `/opt/farm-seed` |
| agent-reach source | build arg `AGENT_REACH_SOURCE_SHA` (digest, ghi vào image) |

Không tag `latest`, không `pip`/`npm` unpinned. `shardx` là dependency
**optional** (`pip install agent-reach[farm]`) — host install thường không
kéo theo.

## Build

```bash
docker build --platform linux/amd64 -f farm/container/Dockerfile \
  -t agent-reach-farm:1.6.1 --build-arg AGENT_REACH_SOURCE_SHA="$(git rev-parse HEAD)" .
```

`linux/amd64` bắt buộc: shardx chỉ ship mac-arm64 / win-x64 / linux-x64, và
host farm là x86_64. Trên Apple Silicon build chạy qua emulation (chậm hơn).

## Run — 1 container cho 1 account

```bash
cd farm/container
cp .env.example accounts/acct001.env   # sửa REACH_ACCOUNT + REACH_BROWSER_PROXY
docker compose -p farm-acct001 --env-file accounts/acct001.env up -d
```

Tương đương docker run (cap RAM = `--memory=1536m`):

```bash
docker run -d --name farm-acct001 --memory=1536m \
  -e REACH_ACCOUNT=acct001 -e REACH_BROWSER_PROXY=http://user:pass@proxy:7000 \
  -v farm-acct001-home:/home/farm/.agent-reach \
  agent-reach-farm:1.6.1
```

## Onboarding (lần đầu)

Container mới chưa có channel nào configured → `agent-reach check` fail →
entrypoint thoát (đúng semantics: check fail = container không healthy).
Onboarding thì bypass gate, xong bật lại:

```bash
docker compose -p farm-acct001 --env-file accounts/acct001.env run --rm \
  -e FARM_BOOT_CHECK=0 account agent-reach login twitter
# sau khi login xong: FARM_BOOT_CHECK=1 (mặc định) trong accounts/acct001.env
```

## Verify

`./verify.sh` (xem trong file) chạy đủ acceptance: build → `agent-reach check`
trả JSON từ trong container → RAM cap qua `docker stats` → `browser setup`.
Bước post là manual — cần test account đã login ("`x post`" trong ticket =
`agent-reach twitter post '<text>'`).

## Ghi chú

- `network_mode` mặc định (bridge). Story 14.3 thay bằng netns riêng +
  kill-switch egress cứng (zero route ngoài proxy). Bridge `127.0.0.1:19825`
  đã nằm trong netns container ngay từ story này.
- `REACH_BROWSER_NO_SANDBOX=1`: Chromium sandbox cần userns mà kernel
  container thường thiếu; proxy + netns là biên isolation của farm.
- Fingerprint per account: `REACH_BROWSER_PROFILE` (template shardx, giữ cố
  định cho mỗi account để fingerprint không đổi giữa các lần restart).

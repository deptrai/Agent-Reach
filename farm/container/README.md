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
  --user root --cap-add NET_ADMIN \
  -e REACH_ACCOUNT=acct001 -e REACH_BROWSER_PROXY=http://user:pass@proxy:7000 \
  -v farm-acct001-home:/home/farm/.agent-reach \
  agent-reach-farm:1.6.1
```

`--user root --cap-add NET_ADMIN` bắt buộc khi `FARM_KILLSWITCH=1` (mặc định):
entrypoint cần root một lần để cài iptables, rồi drop về `farm` (xem mục
kill-switch). `docker exec` vào container này mặc định là root — luôn thêm
`-u farm` khi chạy `agent-reach` để file trong HOME volume giữ đúng owner.

## netns hard kill-switch (story 14.3 — contract §2 layer-2)

Layer cứng của kill-switch: iptables trong chính netns của container, cài
lúc boot bởi `netns-killswitch.sh` (chạy root, trước mọi traffic account).
Không phải `network_mode: none` + sidecar — proxy farm là host:port bên ngoài
(residential proxy), nên rule in-netns nhẹ hơn mà vẫn bảo đảm zero non-proxy
egress.

Rules (OUTPUT, filter table):

| Rule | Tác dụng |
|---|---|
| `-P OUTPUT DROP` + `-F OUTPUT` | policy DROP; wipe + re-add → idempotent khi restart |
| `-o lo ACCEPT` | loopback: bridge `127.0.0.1:19825`, CDP — không đổi |
| `udp/tcp --dport 53, 853 → DROP` | DNS/DoT chết với mọi resolver (kể cả embedded DNS của docker) |
| `-d <proxy_ip> -p tcp --dport <port> ACCEPT` | egress duy nhất: TCP tới IP:port của proxy |

Reply packets về qua chain INPUT (không filter) nên không cần conntrack.
IPv6 có cùng bộ rule (ip6tables) khi netns có stack; thiếu ip6tables mà có
IPv6 stack = fail closed.

Hệ quả:

- **Proxy sống** → `curl -x $REACH_BROWSER_PROXY https://...` OK; mọi đường
  khác (direct, `--noproxy`, direct-IP, DNS) đều fail.
- **Proxy chết** → không còn đường ra ngoài nào — connect tới proxy bị
  refuse ngay, còn lại bị DROP. Không chờ dispatch soft-check (13.2).
- Proxy host được resolve **một lần lúc boot** rồi pin vào `/etc/hosts`
  (DNS đã chết nên phải resolve qua files). Dùng proxy có gateway hostname
  ổn định hoặc IP literal; hostname mà IP xoay liên tục sẽ cần restart
  container để re-resolve.
- **Fail-closed**: script lỗi (thiếu iptables, proxy URL xấu, không
  resolve được, không phải root khi `FARM_KILLSWITCH=1`) → container start
  abort. `FARM_KILLSWITCH=0` là escape hatch cho dev — không compliant.

Verify nhanh trong container đang chạy:

```bash
docker exec farm-acct001 iptables -S OUTPUT                       # xem rules
docker exec -u farm farm-acct001 curl --noproxy '*' -m5 https://ifconfig.me   # phải FAIL
docker exec -u farm farm-acct001 curl -x "$REACH_BROWSER_PROXY" -m10 https://ifconfig.me  # OK khi proxy sống
docker exec -u farm farm-acct001 getent hosts example.com         # phải FAIL
```

`./verify.sh` bước `[5/6]` chạy đủ các probe trên.

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

- `network_mode` mặc định (bridge) + iptables kill-switch trong netns —
  egress chỉ còn tới proxy. Bridge `127.0.0.1:19825` nằm trong netns
  container, đi qua `-o lo ACCEPT` nên không bị rule chặn.
- `REACH_BROWSER_NO_SANDBOX=1`: Chromium sandbox cần userns mà kernel
  container thường thiếu; proxy + netns là biên isolation của farm.
- Fingerprint per account: `REACH_BROWSER_PROFILE` (template shardx, giữ cố
  định cho mỗi account để fingerprint không đổi giữa các lần restart).

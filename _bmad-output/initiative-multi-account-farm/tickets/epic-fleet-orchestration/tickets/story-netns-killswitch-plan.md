---
type: feature
ticket: '3'
status: built
route: full
risk: medium
baseline_revision: 28cfb7c
---

# Story 14.3 — netns hard-enforce kill-switch (zero egress ngoài proxy)

## Kết quả

Contract §2 layer-2 hiện thực bằng iptables trong chính netns của từng farm
container: OUTPUT policy DROP, egress duy nhất là TCP tới `proxy_ip:port`
(resolve 1 lần lúc boot, pin vào `/etc/hosts`), DNS/DoT chết hoàn toàn, v4
lẫn v6. Fail-closed mọi nhánh: không root / thiếu iptables / proxy URL xấu /
không resolve được → container start abort.

## Design — in-netns iptables, không dùng `network_mode: none`

| Phương án | Đánh giá |
|---|---|
| **`cap_add NET_ADMIN` + iptables trong netns container** (chọn) | Proxy farm là host:port bên ngoài (residential proxy). Rule áp thẳng trong netns → leak path không tồn tại, đúng contract "hard". Không thêm container nào. |
| `network_mode: none` + egress network qua proxy container | Chỉ hợp khi proxy là container sidecar. Proxy của ta là dịch vụ ngoài → phải dựng thêm forwarder per account → nặng hơn, cùng guarantee. Bỏ. |

Entrypoint chain: compose `user: root` (một lần) + `cap_add: NET_ADMIN` →
`netns-killswitch.sh` áp rules → `runuser -u farm` re-exec entrypoint → phase
farm như cũ. Sau khi drop uid **không process nào trong container giữ
NET_ADMIN** → rules không gỡ được từ trong (chắc hơn phương án setcap
xtables cho user farm).

## Rule set (đã live-verify)

```
-P OUTPUT DROP                    # policy
-F OUTPUT                         # wipe + re-add → idempotent khi restart
-A OUTPUT -o lo -j ACCEPT         # loopback: bridge 127.0.0.1:19825, CDP
-A OUTPUT -p udp/tcp --dport 53  -j DROP   # DNS chết với mọi resolver
-A OUTPUT -p udp/tcp --dport 853 -j DROP   # DoT
-A OUTPUT -d <proxy_ip> -p tcp --dport <port> -j ACCEPT   # egress duy nhất
```

`ip6tables` nhận bộ rule tương đương khi netns có stack v6; có stack mà
không program được = fail closed. Reply packets về qua INPUT (không filter)
→ không cần conntrack. Proxy host pin `/etc/hosts` (append + marker —
idempotent) vì DNS chết sau khi rules áp; glibc `files` trước `dns` nên
mọi in-container resolution của proxy host đi qua pin.

## Auto Run Result

- Static: `pytest tests/test_netns_killswitch.py` — **8/8 pass**; suite đầy
  đủ **1067 passed**.
- **Live-verify trên host này** (Docker 29.8.1, macOS arm64, image
  `linux/amd64` emulation, throwaway `farm-ks-iter` + CONNECT proxy giả
  `host.docker.internal:19877`) — script thật chạy trong container:
  - Proxy sống: `curl -x http://host.docker.internal:19877 https://ifconfig.me`
    → **OK** (trả WAN IP — egress đi đúng đường proxy).
  - `curl --noproxy '*' https://ifconfig.me` → **FAIL rc=6**; `curl
    --noproxy '*' http://1.1.1.1/` → **FAIL rc=28** (direct + direct-IP đều
    không có route).
  - `getent hosts example.com` → **FAIL** (DNS chết); `getent hosts
    host.docker.internal` → trả IP pin từ `/etc/hosts`.
  - Kill proxy → `curl -x` **FAIL rc=7** → total egress death; mọi probe
    đều chết.
  - Loopback `python3 -m http.server 19825` → `curl 127.0.0.1:19825` →
    **200** (bridge không bị ảnh hưởng).
  - `iptables -S OUTPUT` + `ip6tables -S OUTPUT` đúng rule set trên cả hai
    family (proxy giả resolve cả `192.168.65.254` + `fdc4:f303:9324::254` →
    cả hai IP được allow).
  - Entrypoint chain: root → killswitch armed → runuser → `id` =
    `uid=1000(farm)`; kill proxy rồi chạy entrypoint → `proxy dead at boot:
    OSError …` → **rc=1** (gate fail-closed, restart-loop cho tới khi proxy
    sống lại).
  - Fail-closed kiểm thêm: DNS đã chết sẵn + chạy script → exit non-zero
    (không resolve được proxy = abort); `-u farm` + `FARM_KILLSWITCH=1` →
    refuse message + exit 1; root + `FARM_KILLSWITCH=0` → bypass message,
    re-exec farm, egress mở nguyên.
- **E2E trên image thật** `agent-reach-farm:1.6.1` (rebuild với Dockerfile
  mới — iptables 1.8.9 nf_tables + curl + 2 script baked, `chmod` seed fix):
  `docker run --user root --cap-add NET_ADMIN -e REACH_BROWSER_PROXY=…
  sleep infinity` → boot log `kill-switch armed` → `proxy handshake OK` →
  container UP. Probe battery lặp lại đủ 6 mục trên: via-proxy trả WAN IP
  thật `2405:4803:…`, direct rc=6, direct-IP rc=28, DNS dead, pin OK,
  loopback :19825 = 200, iptables/ip6tables dump đúng rule set. Kill proxy
  → via-proxy **rc=7** + direct vẫn chết = total egress death.
- Full image rebuild sau thay đổi Dockerfile: tail-layer giữ cache chromium —
  rebuild lần 2 (fix entrypoint guard) và lần 3 (thêm curl) chỉ ~20s mỗi lần.

## Files

### Mới
- `farm/container/netns-killswitch.sh` — toàn bộ logic: parse
  `REACH_BROWSER_PROXY` (python3 urlparse, fail nếu không có host:port),
  resolve getaddrinfo cả v4+v6 **trước khi** áp rules, pin `/etc/hosts`, áp
  iptables/ip6tables OUTPUT policy DROP + DNS/DoT drop + proxy allow,
  `set -eu` fail-closed, `FARM_KILLSWITCH=0` escape hatch (documented
  non-compliant).
- `tests/test_netns_killswitch.py` — 8 static-contract tests.
- Plan file này.

### Sửa
- `farm/container/entrypoint.sh` — root phase: chạy killswitch → export
  `FARM_KILLSWITCH_APPLIED=1` → `exec runuser -u farm` re-exec; gate
  fail-closed khi `FARM_KILLSWITCH=1` mà không phải root. **Boot probe đổi
  semantics khi killswitch armed**: `agent-reach check` probe direct → chết
  DNS by design → gate thành TCP handshake proxy host:port qua
  `dispatch._proxy_alive` (tái dùng layer-1, fail-closed).
- `farm/container/docker-compose.yml` — `user: root`, `cap_add: NET_ADMIN`,
  env `FARM_KILLSWITCH` (mặc định 1).
- `farm/container/Dockerfile` — `apt install iptables` + COPY 2 script ở
  **tail** (sau layer chromium → giữ cache); `chmod -R a+rX /opt/farm-seed`
  (bug fix, xem deviations).
- `farm/container/.env.example` — `FARM_KILLSWITCH=1`.
- `farm/container/README.md` — mục kill-switch: rule set, hệ quả, verify
  probes, `docker exec -u farm`.
- `farm/container/verify.sh` — docker run thêm `--user root --cap-add
  NET_ADMIN`, exec `-u farm`, bước `[5/6]` probes kill-switch.
- `docs/farm-account-contract.md` — layer-2 + container-template rows:
  Deferred → Implemented (story 14.3 / 14.2+14.3).
- `tests/test_container_template.py` — comment cập nhật (vẫn đúng: không
  `network_mode`).

## Live verify log

- 2026-11-22: `farm-ks-iter` (image 1.6.1 cũ + `apt install iptables` +
  script copy vào) — toàn bộ probes trên; container đã rm sau verify.
- 2026-11-22: rebuild `agent-reach-farm:1.6.1` thành công 3 lần (iptables
  layer → entrypoint guard fix → +curl); `farm-ks-final` từ image thật chạy
  đầy đủ probe battery (kết quả ở trên); container + mini-proxy đã dọn.

## Deviations

- **`agent-reach check` không còn là boot gate khi killswitch armed** — bản
  chất contract (direct egress chết → direct probes phải fail), không phải
  regression. Gate thay bằng proxy TCP handshake (`dispatch._proxy_alive`).
  Fleet manager 14.4 đọc health cần biết semantics này (check JSON trong
  killswitch container = "mọi channel UPSTREAM_BROKEN" là trạng thái thiết
  kế, không phải lỗi).
- **Bug fix kèm**: `/opt/farm-seed/extension` build ra `drwx------ root` →
  `cp -a` trong entrypoint (uid farm) fail âm thầm. Ở 14.2 bị che vì runtime
  còn egress tải lại extension; 14.3 egress chết → seed là nguồn duy nhất →
  `chmod -R a+rX` trong Dockerfile.
- Proxy host resolve 1 lần lúc boot + pin `/etc/hosts`: hostname mà IP xoay
  liên tục cần container restart để re-resolve — ghi trong README; khuyến
  nghị proxy gateway hostname ổn định hoặc IP literal.
- Chỉ allow TCP tới proxy (HTTP CONNECT + socks5 đều TCP). UDP-associate
  không mở — không cần cho browser path.
- `docker exec` vào container giờ mặc định root → docs/verify.sh luôn
  `-u farm` khi chạy `agent-reach` (file ownership trong HOME volume).

## Risks / pending

- Verify e2e trên container thật của image mới (có sẵn killswitch) qua
  `verify.sh` — script đã live-verify trong-container; rebuild + compose-up
  e2e cùng lúc với 14.6 scale gate cũng được.
- Proxy residential thật: pending từ 14.1 — probes dùng CONNECT proxy giả
  trên host; rule semantics giống hệt với proxy thật.
- `/etc/hosts` append-only (bind-mount không cho rename) — marker đảm bảo
  idempotent; file do docker regenerate mỗi restart nên pin tự làm mới.

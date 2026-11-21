---
type: feature
ticket: '6'
status: built
route: full
risk: medium
baseline_revision: ab4941b
---

# Story 14.6 — Scale e2e ≥4 container + runbook nhân bản + docs sweep

## Kết quả

- Live-verify 4 container `farm-scale{1..4}` chạy song song trên host này:
  volume riêng, netns riêng, iptables kill-switch riêng, không publish port.
- Runbook `docs/farm-scale-runbook.md`: nhân bản 2→N (registry add → env →
  volume/compose up → login window → verify) + checklist isolation.
- Docs sweep: `docs/multi-account-farm-architecture.md` §4b/§4c và
  `docs/farm-account-contract.md` §2/§4/§6 — bỏ claim "layer-2 còn ở
  epic-fleet", "netns riêng còn ở 14.3", "PQD fallback deferred".
- Scale cap: ticket gắn cap ≤20 với điều kiện "tới khi epic-farm-resilience
  live". Resilience **đã ship** (16.1–16.4) → cap không còn là gate cứng;
  runbook ghi gate thực tế còn lại là RAM host + proxy 1:1 + login thủ công.

## Auto Run Result

Host: Docker 29.x, macOS arm64, image `agent-reach-farm:1.6.1` (linux/amd64
emulation, 1.72GB), RAM host 32GB — 4 container × cap 1.5GB chạy ổn, không
cần hạ xuống 2–3. Proxy là IP literal placeholder `10.255.255.{1..4}:7000`
(không có proxy/account thật); container chạy `FARM_BOOT_CHECK=0`.

**Verified:**

- 4 container `Up` đồng thời (`docker ps --filter label=com.agent-reach.farm`).
- Volume tách biệt: ghi `marker.txt` trong `farm-scale1` → `scale2/3/4`
  `ABSENT`. Mounts: `farm-scaleN-home -> /home/farm/.agent-reach` riêng từng cái.
- Netns riêng: `/proc/1/ns/net` inode `4026534331 / 4467 / 4603 / 4739` — 4
  giá trị khác nhau. (`ip` không có trong image; inode netns là bằng chứng.)
- `docker port farm-scale1` và `farm-scale2` **rỗng** — bridge :19825 không
  publish ra host.
- Kill-switch per container: `iptables -S OUTPUT` của scale1 ACCEPT đúng
  `10.255.255.1:7000`, scale2 ACCEPT đúng `10.255.255.2:7000`, cả hai
  `-P OUTPUT DROP` + DROP dport 53/853. `/etc/hosts` pin đúng IP từng proxy.
- Egress chặn: `curl --noproxy '*' http://1.1.1.1/` trong scale1 →
  `direct BLOCKED rc=28`.
- `agent-reach fleet poll --once` → `polled: 4`, 4 container
  `session_state: degraded` (NEED_SETUP — chưa onboarding, đúng kỳ vọng).
- Write song song: `agent-reach twitter post` chạy đồng thời trong scale1 và
  scale2 (mỗi container có registry riêng với account của nó) → cả hai fail
  **độc lập** sau ~5s với envelope đúng danh tính:
  scale1 `PLATFORM_BLOCKED "proxy dead for account scale1: http://10.255.255.1:7000"`,
  scale2 `"proxy dead for account scale2: http://10.255.255.2:7000"`.
  Không lẫn session/proxy.

**Pending-operator (cần account đã login + proxy residential thật):**

- `session_state: ok` thật (ở đây là `degraded` vì container chưa onboard).
- 1 write **thành công** đồng thời trên ≥2 container — ở đây cả hai bị
  kill-switch chặn ở proxy chết (placeholder), nên mới chứng minh được
  *fail độc lập đúng account*, chưa chứng minh được post thật.
- `fleet relogin` / login window end-to-end — không fire vì check không trả
  `NEED_LOGIN` (chưa có channel configured).
- Walk-through runbook trên account thật (bước 4–5) — các lệnh đã đối chiếu
  với compose/entrypoint/CLI đang ship, chưa chạy với proxy sống.

Dọn sau test: `docker rm -f` 4 container, `docker volume rm` 4 volume, xóa
row `accounts` + `fleet_containers` và HOME skeleton `scale1/scale2` trong
registry host. Image giữ nguyên.

---
type: feature
ticket: '1'
status: built
route: oneshot
risk: high
baseline_revision: 0bb704e7c56f6175713d63e3cab77f53332d75c2
---

# Spike 14.1 — Browser vendor + proxy path verify

## Verdict

**Chọn ShardBrowser (`shardx` 2.1.7, MIT) làm vendor cho container template 14.2.** CloakBrowser giữ vai trò fallback host-local như hiện tại (đã tích hợp sẵn, upstream-tested) nhưng không đưa vào farm: free license chỉ 1 profile concurrent, không có knob engine-level cho QUIC/WebRTC.

Cả hai vendor đều live-verified đầy đủ trên máy này (probe profile riêng, browser dedicated trên :9222 không bị đụng): `proxy=` param đẩy toàn bộ egress TCP/TLS qua proxy, extension OpenCLI load được và service_worker hiện trên CDP `/json`, CDP port cố định bằng flag, x.com load qua proxy. ShardX thắng ở 3 điểm quyết định cho farm: MIT unlimited profile, engine tự tắt QUIC + ép WebRTC `tcp_only` khi proxy không có UDP (đúng leak-vector ticket lo), và fingerprint engine-level coherent tới WebGL renderer.

## Bằng chứng (live, fake CONNECT proxy 127.0.0.1:19876 + CDP port riêng)

### CloakBrowser (binary hiện có, `launch_persistent_context(proxy=)`)

- Setup probe y hệt `browser_watcher._launch_cloak` nhưng profile scratch `/tmp/spike141-scratch/cloak-profile`, port 19899 — không đụng browser dedicated đang chạy trên :9222.
- Fake proxy log ghi nhận: `GET http://example.com/ HTTP/1.1` (absolute-URI → HTTP đi qua proxy), `CONNECT x.com:443` + `TUNNEL-ESTABLISHED`, `CONNECT abs.twimg.com:443` ×4 (subresource đi cùng đường). `page.goto("https://x.com/")` trả `HTTPS-NAV-OK` — Chrome tự fallback TCP qua CONNECT tunnel khi proxy explicit (QUIC không chạy qua HTTP proxy).
- `/json` trên 19899: page `https://x.com/` + `service_worker chrome-extension://lapcdjabnpnfiggbflfgafdhjmmkhoeg/dist/background.js` — extension OpenCLI load qua `extension_paths` như production.
- Lỗ hổng wiring production (không phải blocker, ghi cho 14.2): `_launch_cloak` hardcode `args` chỉ 2 flag remote-debugging → không có seam truyền `--disable-quic` / WebRTC policy flag nếu cần.

### ShardBrowser (`pip install shardx` → 2.1.7 vào scratch venv, binary đã có sẵn)

- `shardx.Runtime().install()` → `installed: True` ngay (patched Chromium `158.0.8127.36` tại `~/Library/Application Support/shardx-sdk/ShardX-Mac-arm64/ShardX.app`). SDK thuần pip, không cần launcher venv riêng kiểu cloak.
- `ShardX().launch(random_profile(), proxy="http://127.0.0.1:19876", cdp=True, headless=True, user_data_dir=..., extra_args=["--remote-debugging-port=19898","--remote-debugging-address=127.0.0.1","--load-extension=...","--disable-extensions-except=..."])` → `BrowserSession` với `quic_enabled=False`, `webrtc_mode="tcp_only"` (SDK auto: probe UDP chỉ bật QUIC khi SOCKS5 có UDP; `webrtc="auto"` → `tcp_only` khi proxy không UDP). Đây là phần CloakBrowser thiếu.
- `/json` trên 19898: service_worker `lapcdjabnpnfiggbflfgafdhjmmkhoeg` (cùng OpenCLI extension) hiện diện cạnh 1 extension nội bộ ShardX; page mặc định `chrome://shardx/`. `PUT /json/new?https://x.com/` mở tab thành công → proxy log ghi `CONNECT x.com`, `api.x.com`, `jf.x.com`, `abs.twimg.com`, sentry — toàn bộ TLS egress qua proxy.
- Fingerprint engine-level verify trực tiếp qua CDP evaluate (playwright `connect_over_cdp`): profile `android-oppo-f31-pro-5g` → UA `Linux; Android 10; K … Chrome/158.0.0.0 Mobile`, `navigator.platform = Linux armv81`, `hardwareConcurrency/deviceMemory = 8/8`, `lang vi-VN`, `tz Asia/Bangkok`, `webdriver = false`, **WebGL UNMASKED_RENDERER = `ANGLE (Qualcomm, Adreno (TM) 720, OpenGL ES 3.2)`** — spoof tới tầng GPU, không phải JS shim. Template library có cả `mac-m4-imac24`, `win-gtx1060` (host-coherent).
- Caveat nhỏ: `sess.cdp_url` trả `None` dù `cdp=True` (cùng tính đã biết ở Cloak) → dùng port cố định trực tiếp, không sao.

## Ma trận quyết định

| Tiêu chí | CloakBrowser | ShardBrowser (shardx 2.1.7) |
|---|---|---|
| `proxy=` egress (HTTP+CONNECT) | ✅ verified | ✅ verified |
| QUIC/WebRTC khi proxy không UDP | ⚠️ fallback TCP tự nhiên; không knob, STUN leak chưa chặn | ✅ `quic_enabled=False`, `webrtc=tcp_only` auto + SOCKS5 UDP probe |
| Extension OpenCLI | ✅ `extension_paths` | ✅ `--load-extension` qua `extra_args` |
| CDP port cố định | ✅ | ✅ |
| Fingerprint | ✅ engine-level (đã verify trước) | ✅ engine-level + WebGL/GPU coherent, template theo host |
| Profile-per-account fit | 1 profile dir chia sẻ; fingerprint qua launcher `--fingerprint-profile` | `Profile(config)` + `user_data_dir` tách biệt per launch → 1:1 account↔profile↔proxy |
| Cost ở scale | free key **1 profile concurrent** → farm = paid | MIT, unlimited |
| Launch API | `launch_persistent_context` + launcher venv riêng, `cloak.setup()` phức tạp | `pip install shardx` + `ShardX().launch(...)` → `BrowserSession` (subprocess trực tiếp) — gọn hơn cho container |

## Integration shape đề xuất cho 14.2

- `agent_reach/backends/shard.py` song song `cloak.py`: `setup()` pin `shardx==2.1.7` + `Runtime().install()` (ghi engine version `158.0.8127.36` vào build — C7: upgrade = rebuild).
- `browser_watcher`: nhánh `--shard` cạnh `--cloak`. Body ≈ `ShardX().launch(profile_cfg, proxy=os.environ["REACH_BROWSER_PROXY"], cdp=True, headless=…, user_data_dir=str(browser.profile_dir()), extra_args=[--remote-debugging-port, --remote-debugging-address, --load-extension, --disable-extensions-except])`; giữ `sess.process` làm handle thay `ctx`. `REACH_BROWSER_PROXY` wiring hiện tại (`Config.proxy` → env) tái dùng nguyên.
- Farm mapping: account → `Profile` fingerprint config (persist trong registry epic-11) + `user_data_dir` riêng per account + `proxy_i` string per container.
- Bridge `127.0.0.1:19825` và extension đều in-container — cùng cơ chế đã verify, không cần đổi `opencli.py`.

## Pending (đánh dấu rõ)

- **Proxy residential thật**: fake proxy chứng minh wiring TCP/CONNECT (`CONNECT host:443` gửi hostname → DNS resolve ở phía proxy là đúng mô hình); chưa chứng minh pass anti-fraud X trên IP residential → operator cung cấp proxy thật ở 14.2/14.6.
- **WebRTC leak thực tế**: engine báo `tcp_only` nhưng chưa mở trang webrtc-test + capture STUN; kèm `--force-webrtc-ip-handling-policy` nếu test lộ.
- **Extension↔daemon bind trên ShardX**: service_worker hiện diện nhưng probe không chạy bridge → `bind()`/`opencli_context` verify trong 14.2 (cùng cơ chế Cloak, low risk).
- **CloakBrowser license enforcement**: probe chạy song song với browser dedicated không bị chặn (local check lỏng), nhưng license terms vẫn 1 profile → không dùng cho farm dù kỹ thuật không fail.
- Soak dài / rate-limit trên X: skip theo ticket (không post gì, chỉ GET/CONNECT).

## Scratch (đã dọn)

Probe scripts + fake proxy + venv shardx + 3 profile dir tại `/tmp/spike141-scratch/` (ngoài repo, không commit); mọi process probe đã terminate. Browser dedicated :9222 của user giữ nguyên trạng.

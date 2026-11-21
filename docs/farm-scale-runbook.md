# Farm scale runbook — nhân bản 2 → N container (story 14.6)

Mỗi account = 1 container (`farm/container/`, story 14.2/14.3): volume
`~/.agent-reach` riêng, netns riêng, iptables kill-switch riêng, bridge
`127.0.0.1:19825` chạy trong netns container — không publish port ra host nên
N container không đụng bridge/proxy/session của nhau. Đã verify 4 container
đồng thời trên 1 host (xem `## Auto Run Result` trong plan story 14.6).

## Scale cap

Cap ≤20 account trong ticket 14.6 gắn điều kiện "tới khi epic-farm-resilience
live (no fallback/no monitor → không scale qua)". **Điều kiện đó đã thỏa:**
fallback routing (16.1/16.2), drift monitor (16.3) và incident playbook (16.4)
đã ship. Cap ≤20 không còn là gate cứng — gate thực tế còn lại là tài nguyên
host (1 container ≈ 1.5GB RAM cap + 1 proxy residential riêng) và vận hành
(mỗi account cần login window thủ công lần đầu).

## Bước nhân bản — thêm account thứ N

Lặp lại cho từng account. `<acct>` là tên registry (`[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}`).

### 1. Đăng ký account

```bash
agent-reach farm account add <acct> --proxy http://user:pass@proxy-host:port
```

Proxy URL phải có scheme (`http`/`https`/`socks5`) + host + port. 1 proxy
residential cho đúng 1 account — không chia sẻ proxy giữa các account.

### 2. Tạo env file

```bash
cd farm/container
mkdir -p accounts
cp .env.example accounts/<acct>.env
```

Sửa `accounts/<acct>.env`: `REACH_ACCOUNT=<acct>`,
`REACH_BROWSER_PROXY=<proxy-url>`. Giữ `REACH_BROWSER_PROFILE` cố định cho mỗi
account (fingerprint không đổi giữa các lần restart). `FARM_BOOT_CHECK=0` cho
lần đầu (chưa login), `FARM_KILLSWITCH=1`.

### 3. Volume + container up

```bash
docker compose -p farm-<acct> --env-file accounts/<acct>.env up -d
```

Compose tự tạo named volume `farm-<acct>_home` mount vào
`/home/farm/.agent-reach` và đặt label `com.agent-reach.farm=1` /
`com.agent-reach.account=<acct>` để fleet manager nhận ra. Tương đương
`docker run` (khi không dùng compose):

```bash
docker run -d --name farm-<acct> --platform linux/amd64 --memory=1536m \
  --user root --cap-add NET_ADMIN \
  -e REACH_ACCOUNT=<acct> -e REACH_BROWSER_PROXY=<proxy-url> \
  -e FARM_BOOT_CHECK=0 -e FARM_KILLSWITCH=1 \
  --label com.agent-reach.farm=1 --label com.agent-reach.account=<acct> \
  -v farm-<acct>-home:/home/farm/.agent-reach \
  agent-reach-farm:1.6.1
```

`--user root --cap-add NET_ADMIN` bắt buộc: entrypoint cài iptables kill-switch
rồi drop về uid `farm`. Image `agent-reach-farm:1.6.1` build 1 lần (xem
`farm/container/README.md`), dùng chung cho mọi container.

### 4. Login window lần đầu

```bash
docker exec -u farm -d farm-<acct> agent-reach login twitter
```

Hoặc để fleet manager tự mở: `agent-reach fleet poll --once` thấy
`NEED_LOGIN` sẽ fire 1 lần `docker exec -d ... login <channel>` và đánh
`pending_login` (không refire). Operator hoàn tất login trong browser của
container đó. Luôn thêm `-u farm` — `docker exec` trần chạy root, file session
sẽ sai owner.

### 5. Bật boot gate + verify

Sửa `accounts/<acct>.env`: `FARM_BOOT_CHECK=1`, rồi recreate container
(`docker compose -p farm-<acct> --env-file accounts/<acct>.env up -d`).
Verify:

```bash
agent-reach fleet list                      # container mới: session_state ok
agent-reach farm metrics --account <acct>   # actions/errors của riêng account này
docker exec farm-<acct> iptables -S OUTPUT  # -P OUTPUT DROP + ACCEPT đúng proxy IP
docker port farm-<acct>                     # rỗng — :19825 không lộ ra host
```

## Kiểm tra isolation (khi nghi ngờ lẫn session/proxy)

- **Volume**: `docker exec -u farm farm-<a> sh -c 'echo x > /home/farm/.agent-reach/marker'`
  rồi `cat` ở container `<b>` — phải vắng mặt.
- **Netns**: `docker exec farm-<acct> ls -l /proc/1/ns/net` — inode khác nhau
  từng container.
- **Kill-switch**: `iptables -S OUTPUT` mỗi container ACCEPT đúng IP:port proxy
  của nó; `docker exec -u farm farm-<acct> curl --noproxy '*' -m5 http://1.1.1.1/`
  phải fail.
- **Write độc lập**: `agent-reach twitter post` chạy đồng thời trong 2 container
  fail với envelope mang đúng `REACH_ACCOUNT` của từng container — không lẫn.

## Gỡ 1 account

`docker rm -f farm-<acct>` + `docker volume rm` volume của nó. Xóa row registry
là thao tác DB (`accounts` + `fleet_containers`) — không có CLI delete.

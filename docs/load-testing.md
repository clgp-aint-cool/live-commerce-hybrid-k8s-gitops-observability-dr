# Test tải và năng lực mở rộng (capacity) của Live Commerce

Tài liệu trả lời 2 câu hỏi:

1. Hệ thống trên cluster CMC chịu được **tối đa bao nhiêu người dùng đồng thời** trước khi vi phạm SLO?
2. Kết quả đó đã đạt mức **workload enterprise cho thương mại điện tử** chưa: tốc độ, độ đúng của đơn hàng (không bán vượt kho), khả năng chịu flash sale và lỗi?

Kịch bản k6 nằm ở repo app, thư mục `live-commerce-app/tests/k6/`:

| File | Mô phỏng | Ghi chú |
|---|---|---|
| `viewer-mix.js` | Người xem: danh sách live, vào phiên, poll `GET /api/lives/:id` mỗi 2 giây, thỉnh thoảng xem sản phẩm và mua | Có profile `smoke`, `load`, `stress`, `spike`, `soak` |
| `chat-ws.js` | Kết nối chat Socket.IO giữ lâu, một phần người dùng gửi tin | Đo độ trễ fan-out và số kết nối bị rớt |
| `live-order.js` | Flash sale: N request tranh M sản phẩm trong 1 phiên | Kiểm tra **không oversell**: số đơn trong DB phải đúng bằng tồn kho |

---

## 1. Hệ thống chịu tải ở đâu

```
Người xem ──video──> Amazon IVS (AWS)            <- KHÔNG đi qua cluster
        └──API/chat──> CMC ELB -> HAProxy (DaemonSet) -> Kong -> service -> PostgreSQL / Redis (DBaaS)
```

- **Video không tốn tài nguyên cluster.** Giới hạn số người xem video là **quota của IVS**: số lượt xem đồng thời và băng thông mỗi tài khoản/region. Xem tại AWS Console → Service Quotas → Amazon IVS, region `ap-northeast-1`, và xin tăng trước sự kiện lớn. Load test cluster **không** cần và **không nên** kéo video.
- **Cluster gánh API, chat realtime và đặt hàng.** Đây là phần cần đo.

### Tải mỗi người dùng sinh ra (đo từ code web)

| Hành vi | Nguồn | Tải |
|---|---|---|
| Đang xem 1 phiên live | `LivePage.tsx` poll `GET /api/lives/:id` mỗi 2 giây | **0,5 req/s mỗi người** → 10.000 người xem ≈ **5.000 req/s** vào live-manager |
| Ở trang danh sách | `LivesPage.tsx` poll `GET /api/lives` mỗi 5 giây | 0,2 req/s |
| Chat | 1 kết nối WebSocket/người; mỗi tin nhắn fan-out tới **mọi** người trong phòng | Phòng 5.000 người, 10 tin/s → 50.000 message/s phát ra |
| Mua hàng | `POST /api/orders`: Redis trừ kho nguyên tử, rồi ghi PostgreSQL | Đỉnh lúc flash sale, chỉ vài giây |

Công thức ước lượng: **RPS ≈ số người xem × 0,5 + số người ở trang danh sách × 0,2 + RPS mua hàng.**

### Điểm nghẽn dự kiến (theo cấu hình hiện tại)

| # | Điểm | Hiện trạng | Hệ quả |
|---|---|---|---|
| 1 | **live-manager** | 1 replica ở dev, **không có HPA**, limit 500m CPU; mỗi `GET /api/lives/:id` đọc PostgreSQL + Redis | Điểm nghẽn đầu tiên khi số người xem tăng |
| 2 | Kết nối PostgreSQL | `PG_POOL_MAX` mặc định 10/pod × số pod của 5 service | Khi HPA scale lên có thể vượt `max_connections` của DBaaS, gây lỗi `too many clients` |
| 3 | IVS ListStreams | live-manager gọi AWS (Tokyo) để lấy trạng thái stream, cache 5 giây (`IVS_STREAMS_CACHE_MS`), gọi cả khi chỉ có phiên demo | Request rơi vào lúc làm mới cache nhiều khả năng bị chậm. Đo thử 5 VU: p50 ≈ 30 ms nhưng p95 ≈ 590 ms |
| 4 | chat-service | HPA theo CPU 70%; fan-out qua Redis adapter (mọi pod nhận mọi tin) | Phòng lớn: băng thông và CPU tăng theo (số tin × số người) |
| 5 | Redis DBaaS | 1 instance dùng chung cho kho, chat adapter, history | Single point of failure; latency Redis kéo theo cả đặt hàng lẫn chat |
| 6 | Edge | HAProxy DaemonSet, Kong ClusterIP 1 replica (chart mặc định) | Kong 1 pod có thể nghẽn trước các service |
| 7 | Node | Worker 2 vCPU / 4 GiB | Tổng `requests` của pod khi HPA scale bị giới hạn bởi số node |

---

## 2. Tiêu chuẩn đạt (SLO) cho e-commerce enterprise

Không có một "chuẩn ISO" duy nhất cho tải e-commerce. Bảng dưới là mức các hệ thống thương mại điện tử lớn thường đặt làm SLO (tham chiếu SRE Book của Google, AWS Well-Architected Framework phần Reliability/Performance). Dự án dùng bảng này làm **tiêu chí đạt/không đạt**. Các ngưỡng đã được ghi sẵn vào `thresholds` của k6.

| Nhóm | Chỉ số | Mức enterprise | Cách đo |
|---|---|---|---|
| Tốc độ đọc | p95 / p99 `GET /api/lives`, `/api/lives/:id`, `/api/products` | **< 300 ms / < 1 s** | k6 `http_req_duration{api:*}` |
| Tốc độ đặt hàng | p95 / p99 `POST /api/orders` | **< 500 ms / < 1,5 s** | k6 `{api:order}` |
| Lỗi | Tỉ lệ 5xx/timeout (409 hết hàng là hợp lệ) | **< 0,1 %** | `http_req_failed` |
| **Đúng đơn** | Số đơn thành công so với tồn kho | **= tồn kho, oversell = 0** (bắt buộc) | `live-order.js` teardown |
| Chat | p95 fan-out (gửi → nhận) | **< 1 s** | `chat_fanout_ms` |
| Chat | Kết nối rớt khi giữ tải | **< 0,1 %** | `chat_disconnects` |
| Flash sale | Chịu tải tăng **x10 trong 30 giây** | SLO vẫn đạt sau ≤ 2 phút (HPA kịp scale) | profile `spike` |
| Co giãn | Thời gian HPA thêm pod tới khi Ready | **< 2-3 phút** | `kubectl get hpa -w` |
| Co giãn | Hiệu suất mở rộng: x2 replica → throughput | **≥ 1,7x** (gần tuyến tính) | so sánh 2 lần stress |
| Bền | Soak 4-8 giờ ở tải mục tiêu | Không rò bộ nhớ, không tăng latency, không restart | profile `soak` + Grafana |
| Chịu lỗi | Xoá 1 pod / drain 1 node khi đang tải | Không mất đơn, lỗi < 1 % trong < 1 phút | mục 5.6 |
| Sẵn sàng | Availability tháng | **99,9 %** (≈ 43 phút downtime/tháng); sự kiện lớn: 99,95 % | uptime monitor |
| Deploy | Rolling update khi đang tải | 0 request lỗi | mục 5.6 |

**Định nghĩa "chịu được N người":** N là số người dùng đồng thời lớn nhất mà **mọi** dòng trong bảng vẫn đạt. **Năng lực công bố = 70 % của N** để chừa headroom cho đột biến và mất 1 node.

---

## 3. Chuẩn bị môi trường

### 3.1 Chạy k6 ở đâu

**Không chạy tải lớn từ Mac qua VPN.** Khi đó bạn đo VPN và Wi-Fi, không phải cluster. Hãy tạo 1 VM riêng trong cùng VPC/subnet với ELB (ví dụ 4 vCPU / 8 GiB; k6 tốn khoảng 1-5 MB RAM mỗi VU) và cài k6:

```bash
sudo gpg -k && sudo gpg --no-default-keyring --keyring /usr/share/keyrings/k6-archive-keyring.gpg --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69
echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt-get update && sudo apt-get install -y k6
echo "192.168.6.8 cmc-cloud-hcm1.cloud" | sudo tee -a /etc/hosts   # trỏ thẳng ELB
ulimit -n 250000                                                   # đủ file descriptor cho WebSocket
```

Hơn khoảng 20.000 VU hoặc 30.000 WebSocket: chia làm nhiều VM chạy song song (mỗi VM 1 phần `TARGET`/`CONNS`), rồi cộng kết quả.

### 3.2 Môi trường test

- **Không test tải trên production đang bán thật.** Dùng `live-commerce-dev` (hoặc namespace `perf` riêng) với **cấu hình giống prod**: bỏ patch `replicas: 1` và min/max HPA của overlay dev trong lúc test, hoặc tạm thời:

  ```bash
  kubectl -n live-commerce-dev patch hpa chat-service  -p '{"spec":{"minReplicas":2,"maxReplicas":20}}'
  kubectl -n live-commerce-dev patch hpa order-service -p '{"spec":{"minReplicas":2,"maxReplicas":10}}'
  kubectl -n live-commerce-dev scale deploy live-manager product-service auth-service --replicas=2
  ```

  Test xong, chạy lại `scripts/deploy.sh` để overlay đưa mọi thứ về như cũ.
- **Dữ liệu:** chạy `live-commerce-app/scripts/seed-demo.cjs` để có 10 phiên demo, 100 sản phẩm. Phiên demo dùng kênh `arn:demo:ivs:`, nên live-manager **không gọi AWS** cho chúng. Test không tốn tiền IVS và không đụng giới hạn TPS của AWS.
- **Kiểm tra trần kết nối DB trước khi test:** tổng `PG_POOL_MAX × số pod tối đa` của các service phải nhỏ hơn `max_connections` của DBaaS (xem trên CMC portal), chừa khoảng 20 % cho admin và migrate.
- **Báo trước** cho đội vận hành CMC nếu tải lớn (ELB và DBaaS có thể có hạn mức hoặc cảnh báo DDoS).

### 3.3 Quan sát trong lúc test

Mở 3 cửa sổ:

```bash
watch -n2 'kubectl -n live-commerce-dev get hpa; kubectl -n live-commerce-dev top pods; kubectl top nodes'
kubectl -n live-commerce-dev get events -w --field-selector type=Warning
kubectl -n live-commerce-dev logs -f deploy/live-manager --tail=20 | grep -iE "error|timeout|ECONN"
```

- **Grafana:** namespace `monitoring`, kèm Loki/promtail cho log. Metrics node và pod lấy từ `kube-state-metrics` và `node-exporter` (namespace `cmc-monitoring`). Theo dõi CPU throttling, restart, network.
- **CMC portal → DBaaS:** CPU, connections, slow query của PostgreSQL; memory và ops/s của Redis.
- **k6:** thêm `--out json=result.json`, hoặc `--out experimental-prometheus-rw` nếu có Prometheus remote-write, để vẽ cùng Grafana.

---

## 4. Thứ tự chạy

Chạy từ gốc repo `live-commerce-app`. Đặt `B=https://cmc-cloud-hcm1.cloud`.

| Bước | Loại test | Mục tiêu | Lệnh |
|---|---|---|---|
| 1 | **Smoke** | Kịch bản chạy đúng, hệ thống không lỗi | `k6 run -e BASE_URL=$B -e PROFILE=smoke tests/k6/viewer-mix.js` |
| 2 | **Load (baseline)** | Đạt SLO ở tải mục tiêu, ví dụ 2.000 người xem | `k6 run -e BASE_URL=$B -e PROFILE=load -e TARGET=2000 tests/k6/viewer-mix.js` |
| 3 | **Stress** | Tìm điểm gãy: tăng tới 3x TARGET | `k6 run -e BASE_URL=$B -e PROFILE=stress -e TARGET=3000 tests/k6/viewer-mix.js` |
| 4 | **Spike** (flash sale) | Tải x10 trong 30 giây, HPA phản ứng kịp không | `k6 run -e BASE_URL=$B -e PROFILE=spike -e TARGET=5000 -e BUY_RATE=0.2 tests/k6/viewer-mix.js` |
| 5 | **Chat** | Số kết nối giữ được, fan-out | `k6 run -e BASE_URL=$B -e CONNS=5000 -e HOLD_S=600 tests/k6/chat-ws.js` |
| 6 | **Đúng đơn** | 0 oversell dưới tranh chấp | xem 4.1 |
| 7 | **Soak** | Rò rỉ, suy giảm theo thời gian | `k6 run -e BASE_URL=$B -e PROFILE=soak -e TARGET=1500 -e SOAK_DURATION=4h tests/k6/viewer-mix.js` |
| 8 | **Chịu lỗi** | Mất pod/node/deploy khi đang tải | chạy bước 2 song song với mục 5.6 |

Chạy **bước 2 + bước 5 cùng lúc** (2 VM) mới sát thực tế: người xem vừa poll API vừa giữ WebSocket chat.

### 4.1 Test không oversell (flash sale)

`live-order.js` tự đăng ký 1 người bán và mở 1 phiên (`ADMIN_TOKEN` nằm trong Secret `live-manager-secret`; đọc trên máy test, **không** dán vào chat hay commit):

```bash
ADMIN_TOKEN=$(kubectl -n live-commerce-dev get secret live-manager-secret -o jsonpath='{.data.ADMIN_TOKEN}' | base64 -d)
k6 run -e GATEWAY_URL=$B -e ADMIN_TOKEN="$ADMIN_TOKEN" -e PRODUCT_ID=1 -e STOCK=100 -e REQUESTS=20000 -e VUS=1000 --insecure-skip-tls-verify tests/k6/live-order.js
```

Lưu ý: với `IVS_MODE=aws`, bước setup **tạo 1 channel IVS thật**. Channel không phát thì không tính phí, nhưng nên xoá trên AWS Console sau khi test. Đạt khi teardown in `100 đơn trong DB / tồn kho 100` và `orders_unexpected = 0`. Lặp lại với `VUS` tăng dần (1.000 → 5.000) và `STOCK` nhỏ (1, 10) vì tranh chấp càng gắt càng dễ lộ lỗi race condition.

---

## 5. Đọc kết quả và kết luận "tối đa bao nhiêu người"

### 5.1 Từ test stress

k6 in p95/p99 theo từng nhãn `api:*`. Ghép timeline k6 với Grafana:

1. Ghi lại **mốc VU đầu tiên** mà một threshold bị vi phạm (latency p95 vượt 300 ms, hoặc lỗi > 0,1 %). Đó là **N_max**.
2. Tại mốc đó, xem **thành phần nào bão hoà trước**: pod CPU chạm limit (throttling), HPA đã max replica, DBaaS CPU/connections cao, node hết CPU allocatable, hay Kong/HAProxy.
3. **Năng lực công bố = 0,7 × N_max.**

Ví dụ báo cáo:

> Cấu hình: 3 worker 2 vCPU, live-manager 2 pod, chat 2-20, order 2-10. N_max = 4.200 người xem (2.100 req/s), giới hạn bởi live-manager CPU 100 % (không có HPA). Năng lực công bố: **2.900 người xem đồng thời / 1 cluster**.

### 5.2 Kiểm tra khả năng mở rộng (scale-out) thật sự

Một hệ thống "scale được" phải tăng năng lực **gần tuyến tính** khi thêm tài nguyên:

1. Stress với live-manager 1 replica → ghi N₁.
2. Scale lên 2, rồi 4 replica (kèm thêm node nếu cần) → ghi N₂, N₄.
3. N₂/N₁ ≥ 1,7 và N₄/N₁ ≥ 3,2 là đạt. Nếu không tăng, nghẽn nằm ở **tầng dùng chung** (PostgreSQL, Redis, Kong, ELB) và thêm pod không giúp được.

### 5.3 Spike / flash sale

- Xem `kubectl get hpa -w`: thời gian từ khi CPU vượt 70 % tới khi pod mới **Ready**. HPA mặc định có độ trễ khoảng 15-30 giây cộng thời gian pull image và readiness, nên cần tổng **< 2-3 phút**.
- Trong khoảng trễ đó, nếu lỗi tăng vọt: tăng `minReplicas` trước sự kiện (pre-scale), hoặc thêm `behavior.scaleUp` nhanh hơn cho HPA.

### 5.4 Chat

- `chat_fanout_ms` p95 tăng nhanh theo số người trong phòng là bình thường (O(N) mỗi tin). Cần xác định **số người/phòng tối đa** mà p95 vẫn < 1 s.
- `chat_disconnects` > 0 khi HPA scale chat-service: kiểm tra HAProxy/Kong có timeout WebSocket đủ dài không, và client có tự kết nối lại không.

### 5.5 Soak

So latency giờ đầu với giờ cuối (tăng > 20 % là có vấn đề); memory pod tăng đều không giảm là rò rỉ; restart > 0 là lỗi.

### 5.6 Chịu lỗi (chạy khi đang load test bước 2)

```bash
kubectl -n live-commerce-dev delete pod -l app.kubernetes.io/name=order-service --wait=false   # mất pod đột ngột
kubectl drain <worker-node> --ignore-daemonsets --delete-emptydir-data       # bảo trì node (nhớ uncordon)
kubectl -n live-commerce-dev rollout restart deploy/live-manager             # deploy khi đang bán
```

Đạt khi lỗi < 1 % và hồi phục < 1 phút, **không mất hay nhân đôi đơn**: đếm đơn trong DB phải khớp số `orders_success` của k6. Với DBaaS: hỏi CMC về failover PostgreSQL/Redis và thử nếu portal cho phép.

---

## 6. Hiện trạng so với chuẩn enterprise (cần làm trước khi test thật)

| Hạng mục | Hiện trạng | Đề xuất |
|---|---|---|
| live-manager không có HPA | Điểm nghẽn #1 vì gánh poll của mọi người xem | Thêm HPA cho live-manager và product-service; cache `GET /api/lives/:id` vài giây (Redis hoặc Kong proxy-cache) |
| Poll 2 giây | 0,5 req/s mỗi người, tải tăng tuyến tính theo người xem | Đẩy cập nhật tồn kho qua WebSocket (đã có chat Socket.IO), giảm poll xuống 10-15 giây làm dự phòng |
| Replica 1 ở dev, không có PodDisruptionBudget | Drain node hoặc deploy làm gián đoạn | prod: ≥ 2 replica/service, PDB `minAvailable: 1`, topologySpread theo node |
| Kong 1 replica, không rate limit | Kong là SPOF; bot có thể spam `/api/orders` | Kong ≥ 2 replica + HPA; plugin `rate-limiting` (Redis) theo IP/user cho `/api/orders`, `/api/auth` |
| HPA chỉ theo CPU 70 % | Node.js I/O-bound có thể chậm trước khi CPU cao | Thêm metric RPS/latency (Prometheus Adapter/KEDA); `behavior.scaleUp` nhanh |
| Pool PG cố định 10/pod | Có thể vượt `max_connections` khi scale | Tính lại pool theo số pod tối đa, hoặc dùng PgBouncer |
| Redis 1 instance | SPOF cho kho và chat | Dùng gói DBaaS Redis có replica/failover |
| TLS tự ký, ELB IP private | Chưa ra Internet thật | Cert thật (Cloudflare Origin), public IP/NAT; test lại qua Cloudflare vì thêm độ trễ và giới hạn |
| Quota IVS | Mặc định của tài khoản | Xin tăng concurrent views trước sự kiện |

Kết luận chỉ được ghi **"đạt workload enterprise"** khi: test 2-7 đạt **toàn bộ** bảng mục 2 ở tải mục tiêu, scale-out đạt mục 5.2, và chịu lỗi đạt mục 5.6.

---

## 7. Mẫu báo cáo kết quả

```
Ngày / commit / tag image:
Cấu hình: số node x vCPU/RAM, replica & HPA từng service, gói DBaaS PG/Redis
Máy k6: số VM, vị trí mạng

| Test   | Tải          | p95 / p99 đọc | p95 đặt hàng | Lỗi   | Oversell | Chat p95 | Kết quả |
|--------|--------------|---------------|--------------|-------|----------|----------|---------|
| Load   | 2.000 VU     |               |              |       |    -     |          |         |
| Stress | N_max = ...  |               |              |       |    -     |          |         |
| Spike  | x10 / 30s    |               |              |       |          |          |         |
| Order  | 20k req/100  |       -       |              |       |   0 ?    |    -     |         |
| Chat   | 5.000 conns  |       -       |      -       |       |    -     |          |         |
| Soak   | 4h           |               |              |       |    -     |          |         |

Thành phần bão hoà đầu tiên:
Năng lực công bố (0,7 × N_max):
Việc cần làm trước lần test sau:
```

Kết quả smoke đã chạy (từ Mac qua VPN, 2026-10-08, dev 1 replica): `viewer-mix` 5 VU, 0 % lỗi, p50 ≈ 30 ms, p95 ≈ 590 ms (vượt SLO 300 ms; nghi do làm mới cache IVS ListStreams + VPN, xem điểm nghẽn #3, cần đo lại từ VM trong VPC); `chat-ws` 4 kết nối, join 100 %, fan-out p95 ≈ 90 ms, 0 rớt kết nối. Đây chỉ là kiểm tra kịch bản, **không phải** số đo năng lực.

# Grafana – Dashboard & Alert Live Commerce (CMC HCM-01)

| Thành phần | File | Nạp bằng |
|---|---|---|
| 3 dashboard (folder **Live Commerce**) | `dashboards/lc-*.json` | ConfigMap `lc-dashboards` (nhãn `grafana_dashboard=1`) |
| Alert rules, templates, notification policies | `alerting/{rules,templates,policies}.yaml` | ConfigMap `lc-alerting` (nhãn `grafana_alert=1`) |
| Contact point Discord (chứa webhook) | `alerting/contact-points.tmpl.yaml` | **Secret** `grafana-alert-contact-points` (nhãn `grafana_alert=1`), không commit |
| Datasource uid cố định, bật sidecar alerts | `values.yaml` | `helm upgrade` |

Tất cả JSON/YAML trong `dashboards/` và `alerting/` được **sinh tự động** bằng `gen.py`. Sửa `gen.py`, đừng sửa file sinh ra:

```bash
python3 gen.py   # ghi lại dashboards/*.json và alerting/*.yaml
```

## Dashboard

| UID | Tên | Nội dung chính |
|---|---|---|
| `lc-overview` | 01 · Tổng quan dịch vụ | Kong req/s, tỷ lệ 5xx theo service, top endpoint lỗi, đơn hàng/phút theo status, WebSocket (chat), phiên live & lỗi IVS, moderation, HPA, log lỗi |
| `lc-workloads` | 02 · Workloads & Autoscaling | Deployment/HPA, CPU so với request (mục tiêu HPA 70%), throttling, memory so với limit, restart/OOM/Waiting, network, log pod |
| `lc-cluster` | 03 · Cluster & Nodes (CMC) | Node theo nodegroup, CPU/mem/disk/IO/network, allocation theo namespace, apiserver, scrape targets |

Metric nghiệp vụ (RPS, 5xx, đơn hàng) lấy từ **access log Kong qua Loki**, vì các service NestJS chưa có `/metrics`.

### Đặc thù label của Prometheus CMC (lý do dashboard cũ không chạy)

Prometheus ở ns `cmc-monitoring` do CMC dựng, label **khác** chuẩn kube-prometheus-stack:

| Nguồn | Đúng trên CMC | Dashboard cũ dùng (sai) |
|---|---|---|
| node-exporter | node = `node_name`. `instance` là **IP pod** | `instance`, `nodename`, `node_uname_info` (không tồn tại) |
| kubelet / cadvisor | `instance` = `node_name` = tên node | `kubernetes_io_hostname` |
| kube-state-metrics v2 | node = `node`, metric `kube_pod_container_resource_requests{resource="cpu"}` | `kube_pod_container_resource_requests_cpu_cores` (v1) |
| cadvisor CPU | có series cấp pod không có `container` → luôn lọc `container!=""` | |
| cadvisor network | chỉ ở container pause, có cả `tunl0` (Calico) → lọc `interface="eth0"` | |
| apiserver | job có target rác `10.100.25.68:80` luôn down → lọc `instance=~".*:6443"` | |
| nodegroup | không có `kube_node_labels` → suy ra từ tên node `kubernetes-n7yj-nodegroup-<group>-worker-…` | |

Retention Prometheus CMC chỉ **1 ngày**, nên dashboard metric không xem được quá 24h (log Loki giữ lâu hơn).

## Triển khai (đúng thứ tự)

> ⚠️ `helm upgrade` sẽ **restart Grafana**. Grafana không có PVC nên mọi thứ tạo tay trên UI (dashboard, mật khẩu admin đã đổi…) sẽ mất. Export trước nếu cần.

**0. Server Discord:** dựng bằng [`../discord/bootstrap.py`](../discord/README.md). Script ghi webhook và role ID vào `~/.config/live-commerce/discord.env`.

**1. Secret contact point Discord** (phải có trước bước 2):

```bash
source ~/.config/live-commerce/discord.env   # DISCORD_WEBHOOK_{CRITICAL,WARNING,TRUST_SAFETY}, DISCORD_ROLE_{ONCALL,TRUST_SAFETY}
envsubst '${DISCORD_WEBHOOK_CRITICAL} ${DISCORD_WEBHOOK_WARNING} ${DISCORD_WEBHOOK_TRUST_SAFETY} ${DISCORD_ROLE_ONCALL} ${DISCORD_ROLE_TRUST_SAFETY}' \
  < alerting/contact-points.tmpl.yaml > /tmp/contact-points.yaml
kubectl -n monitoring create secret generic grafana-alert-contact-points \
  --from-file=contact-points.yaml=/tmp/contact-points.yaml --dry-run=client -o yaml \
  | kubectl label --local -f - grafana_alert=1 -o yaml | kubectl apply -f -
rm /tmp/contact-points.yaml
```

Muốn GitOps: thay `kubectl apply` bằng `kubeseal --controller-namespace sealed-secrets --controller-name sealed-secrets-controller -o yaml` rồi commit SealedSecret (xem `docs/secrets.md`).

**2. Helm upgrade Grafana** (datasource uid `prometheus`/`loki`, sidecar alerts, `root_url`):

```bash
helm upgrade grafana grafana/grafana -n monitoring --version 10.5.15 -f platform/monitoring/grafana/values.yaml
```

**3. Dashboard + alert rules:**

```bash
kubectl apply -k platform/monitoring/grafana
```

**4. Kiểm tra:** Grafana → Alerting → Alert rules (folder *Live Commerce*), Contact points → `discord-critical` → **Test**, kiểm tra tin vào đúng #alert-critical và có tag @SRE On-call.

Sidecar gọi `/api/admin/.../reload` bằng tài khoản trong Secret `grafana`. Nếu đổi mật khẩu admin trên UI, reload trả 401: dashboard vẫn tự nạp (file provider quét định kỳ) nhưng **alert chỉ nạp khi Grafana khởi động lại**.

## Alert → Discord

Routing (`policies.yaml`), group theo `alertname, namespace`:

| Điều kiện (xét theo thứ tự) | Contact point → kênh | Ping | Lặp lại |
|---|---|---|---|
| `team=trust-safety` | `discord-trust-safety` → #alert-trust-safety | @Trust & Safety | 4h |
| `severity=critical` | `discord-critical` → #alert-critical | @SRE On-call | 30m |
| `severity=warning\|info` | `discord-warning` → #alert-warning | không | 4h |

Chỉ tag role khi alert đang firing, tin resolved không ping ai.

Tin nhắn có link Dashboard / Panel / Runbook / Silence. Link dùng `root_url` trong `values.yaml` (hiện là `http://localhost:3000` qua port-forward, đổi khi có domain).

| Nhóm | Rule | Severity | for |
|---|---|---|---|
| lc-app-availability | Deployment thiếu replica | critical | 5m |
| | Service DOWN hoàn toàn (0 replica available) | critical | 1m |
| | Pod restart liên tục (>2 lần/15m) | warning | 0s |
| | Container bị OOMKilled | warning | 0s |
| | Container kẹt Waiting | warning | 10m |
| | HPA chạm maxReplicas | warning | 10m |
| | CPU throttling > 25% | warning | 15m |
| | Memory > 90% limit | warning | 10m |
| lc-traffic-business (Loki) | Tỷ lệ 5xx qua Kong > 5% | critical | 3m |
| | Kong 502/503/504 | critical | 0s |
| | Ghi đơn hàng thất bại | critical | 0s |
| | Hoàn tồn kho thất bại | critical | 0s |
| | Lỗi gọi AWS IVS | warning | 0s |
| | Phiên live bị dừng do vi phạm | info | 0s |
| | Log lỗi tăng đột biến | warning | 5m |
| lc-infra | Node NotReady | critical | 3m |
| | Node CPU > 85% | warning | 10m |
| | Node memory > 90% | critical | 10m |
| | Đĩa / > 85% | warning | 10m |
| | Đĩa / sẽ đầy trong 24h | warning | 30m |
| | CPU requests worker > 85% | warning | 15m |
| | Mất scrape target | warning | 5m |
| | API server không phản hồi | critical | 3m |
| | kube-state-metrics mất dữ liệu | critical | 5m |

Rule Loki khớp chuỗi log nghiệp vụ: `Ghi đơn thất bại`, `Hoàn tồn kho thất bại` (order-service), `(PutMetadata|CreateStreamKey|ListStreams) lỗi` (live-manager), `STREAM_STOPPED` (moderation-service). Đổi message log trong code thì phải sửa `gen.py`.

## Runbook

<a id="deployment-thieu-replica"></a>
### Deployment thiếu replica / Service DOWN

1. `kubectl -n <ns> get pod -l app=<deployment>` → xem trạng thái.
2. `ImagePullBackOff`: kiểm tra tag image trên Harbor và secret pull.
3. `Pending`: node hết tài nguyên → dashboard 03, panel allocation. Cân nhắc scale nodegroup.
4. `CrashLoopBackOff`: `kubectl logs --previous`, kiểm tra env/secret (DB, Redis, AWS).

<a id="kong-5xx"></a>
### 5xx qua Kong

1. Dashboard 01 → "Top endpoint lỗi" để biết service/route.
2. 502/503/504 = upstream không có pod sẵn sàng hoặc timeout → kiểm tra Deployment ở trên.
3. 500 = lỗi ứng dụng → panel log lỗi của service.

<a id="order-fail"></a>
### Ghi đơn / hoàn tồn kho thất bại

Ảnh hưởng trực tiếp doanh thu và lệch tồn kho. Kiểm tra kết nối DB của order-service, đối soát đơn và tồn kho thủ công cho các `orderId` trong log.

## Tùy chọn: latency p95/p99

Log Kong hiện không có thời gian xử lý, nên panel latency trống. Bật định dạng log `lc_combined` (chỉ nối thêm `rt=`/`urt=`, query cũ vẫn chạy):

```bash
helm upgrade kong kong/kong -n kong --version 3.4.1 -f platform/kong/values.yaml -f platform/kong/values-access-log.yaml
```

## Việc nên làm tiếp

- Đổi `adminPassword: admin` sang `admin.existingSecret` (SealedSecret).
- Thêm PVC cho Grafana hoặc dùng DB ngoài.
- Thêm `prom-client` (`/metrics`) cho các service NestJS để có latency/RPS chính xác thay vì suy từ log.

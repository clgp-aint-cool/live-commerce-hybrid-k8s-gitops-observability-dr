# Migrate Live Commerce lên Kubernetes CMC Cloud (chưa có CD)

Hướng dẫn từng bước đưa toàn bộ ứng dụng (đang chạy bằng `docker compose` trong `live-commerce-app`) lên cluster Kubernetes của CMC Cloud.

- **Có:** CI build image và push lên Harbor `https://192.168.6.194` (VM cùng subnet với cluster).
- **Chưa có:** CD. Không dùng ArgoCD; ta tự `kubectl apply -k` từ repo này. Khi muốn bật ArgoCD sau, cấu trúc thư mục giữ nguyên, chỉ cần làm theo [argocd/install/README.md](../argocd/install/README.md).
- Môi trường đích: **dev** (namespace `live-commerce-dev`, `moderation-dev`). Prod làm y hệt với overlay `prod`.

> Quy ước trong tài liệu: `<NODE_IP>` = IP một worker node, `<TAG>` = tag image (8 ký tự đầu git SHA) do CI push. Các lệnh chạy từ **thư mục gốc repo `live-commerce-infra`**, trừ khi ghi khác.

---

## 0. Kiến trúc đích

```
Trình duyệt ─► Cloudflare ─► CMC ELB :443 (Service LoadBalancer) ─► HAProxy (TLS) ─► Kong   (thử nhanh: <NODE_IP>:32080 thẳng vào Kong)
                     │
                Kong (namespace kong, DB-less, Ingress Controller)
                     │  Ingress "kong-routes" (namespace live-commerce-dev)
   ┌───────────┬─────┴──────┬──────────────┬─────────────────────────────┬────────┐
/api/products /api/auth  /api/orders  /api/sellers|sessions|lives        /ws       /
   │           │            │              │                              │        │
product-svc auth-svc   order-svc     live-manager ──► AWS IVS        chat-svc    web
   │           │            │              │   (IVS_MODE=aws|mock)        │
   └───────────┴────────────┴──────────────┴──────────────┬───────────────┘
                                                          ▼
                     CMC Cloud DBaaS (ngoài cluster, private endpoint): PostgreSQL + Redis

moderation-dev: moderation-service ── NodePort 30184 (webhook từ AWS qua VPN, làm sau)
                                    └─► chat-service, live-manager (/internal), PostgreSQL DBaaS

Mọi node kéo image từ Harbor 192.168.6.194/live-commerce/<service>:<TAG> (imagePullSecret harbor-registry)
```

| Thành phần             | Image                                               | Cổng container | Namespace         |
| ---------------------- | --------------------------------------------------- | -------------- | ----------------- |
| product-service        | `192.168.6.194/live-commerce/product-service`       | 3002           | live-commerce-dev |
| auth-service           | `…/auth-service`                                    | 3006           | live-commerce-dev |
| order-service          | `…/order-service`                                   | 3003           | live-commerce-dev |
| chat-service           | `…/chat-service`                                    | 3001           | live-commerce-dev |
| live-manager           | `…/live-manager`                                    | 3005           | live-commerce-dev |
| web (nginx)            | `…/web`                                             | 80             | live-commerce-dev |
| moderation-service     | `…/moderation-service`                              | 3004           | moderation-dev    |
| PostgreSQL, Redis      | DBaaS CMC Cloud (không chạy trong cluster)          | 5432, 6379     | —                 |

Mọi Service K8s của ứng dụng mở **cổng 80** → `targetPort: http`, nên URL nội bộ không cần số cổng (`http://product-service`).

---

## 1. Chuẩn bị máy làm việc

Cài công cụ (macOS):

```bash
brew install kubectl helm kustomize kubeseal
```

Tải **kubeconfig** của cluster từ portal CMC Cloud, rồi:

```bash
export KUBECONFIG=~/.kube/cmc-live-commerce.yaml
kubectl get nodes -o wide
```

Ghi lại:

- **IP nội bộ của các node** (cột `INTERNAL-IP`). Phải cùng subnet hoặc định tuyến được tới `192.168.6.194`.
- **Kiến trúc CPU** của node. Image do CI build phải cùng kiến trúc, thường là `amd64`:
  ```bash
  kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.nodeInfo.architecture}{"\t"}{.status.nodeInfo.containerRuntimeVersion}{"\n"}{end}'
  ```
- **Dải IP node và VPC** của cluster: cần để allow-list trên DBaaS PostgreSQL/Redis (mục 5). Cluster không cần StorageClass vì không chạy DB.
- **metrics-server** (cần cho HPA). Nếu lệnh dưới báo lỗi thì HPA hiện `<unknown>`; app vẫn chạy, chỉ không tự scale.
  ```bash
  kubectl top nodes
  ```

---

## 2. Harbor: project, robot account, chứng chỉ

### 2.1 Kiểm tra image CI đã push

Harbor UI → project **`live-commerce`** phải có đủ **7 repository**: `product-service`, `auth-service`, `order-service`, `chat-service`, `live-manager`, `moderation-service`, `web`. Ghi lại tag mới nhất của từng repo, vì mỗi repo có thể có tag khác nhau.

- CI chỉ build những service có thay đổi (`dorny/paths-filter`). Nếu thiếu repo nào, push một commit nhỏ vào thư mục của service đó, hoặc build tay trên runner:
  ```bash
  # trên máy runner Linux amd64 đã docker login 192.168.6.194; chạy ở gốc live-commerce-app
  TAG=$(git rev-parse --short=8 HEAD)
  for c in product-service auth-service order-service chat-service live-manager moderation-service; do
    docker build -t 192.168.6.194/live-commerce/$c:$TAG services/$c && docker push 192.168.6.194/live-commerce/$c:$TAG
  done
  docker build -t 192.168.6.194/live-commerce/web:$TAG web && docker push 192.168.6.194/live-commerce/web:$TAG
  ```
- Trên Mac Apple Silicon, phải thêm `docker buildx build --platform linux/amd64 --push …`. Nếu không, pod báo `exec format error`.
- Biến `HARBOR_REGISTRY` của repo app trên GitHub phải là `192.168.6.194` (không có `https://`). Tên image và manifest dựa vào chuỗi này.

> **Job `update-manifests` trong `build-and-deploy.yml`** là phần CD: nó commit tag mới vào repo infra. Khi chưa cấu hình `INFRA_REPO`/`INFRA_REPO_TOKEN`, job này sẽ đỏ. Tạm tắt nó bằng cách thêm điều kiện vào job:
>
> ```yaml
> update-manifests:
>   if: ${{ vars.INFRA_REPO != '' }}
> ```

### 2.2 Robot account chỉ có quyền pull

Harbor UI → Projects → `live-commerce` → **Robot Accounts** → **New Robot Account**:

- Tên: `k8s-pull`. Harbor sẽ đặt tên đầy đủ là `robot$live-commerce+k8s-pull`.
- Quyền: chỉ **Pull Repository**.
- Hạn dùng: tùy chính sách. Hết hạn thì pod mới sẽ `ImagePullBackOff`.

Chép **secret** của robot vào file secret ở mục 7. Không dán vào chat hay commit.

### 2.3 Cho node tin chứng chỉ của Harbor (quan trọng nhất)

Harbor chạy HTTPS trên **IP** và thường dùng chứng chỉ tự ký. Image được kéo bởi **containerd trên từng node** (không phải bởi pod). Vì vậy **mỗi worker node** phải tin CA của Harbor, nếu không pod sẽ `ImagePullBackOff` với lỗi `x509: certificate signed by unknown authority`.

**a. Lấy CA:** Harbor UI → Projects → nút **Registry Certificate** (tải `ca.crt`), hoặc chép từ VM Harbor (file CA dùng khi cài Harbor).

**b. Kiểm tra chứng chỉ có IP SAN:** truy cập bằng IP thì chứng chỉ phải chứa `IP Address:192.168.6.194`.

```bash
openssl s_client -connect 192.168.6.194:443 </dev/null 2>/dev/null | openssl x509 -noout -ext subjectAltName
```

Nếu không có dòng `IP Address:192.168.6.194`, phải cấp lại chứng chỉ Harbor kèm `subjectAltName = IP:192.168.6.194`, rồi chạy lại `./prepare` và `docker compose up -d` trên VM Harbor. Nếu bỏ qua, containerd sẽ báo `x509: cannot validate certificate for 192.168.6.194 because it doesn't contain any IP SANs`.

**c. Cài CA lên từng worker node** (SSH vào node; nếu node pool tự sinh node mới thì phải đưa bước này vào user-data/cloud-init của node pool):

```bash
# Ubuntu/Debian
sudo cp ca.crt /usr/local/share/ca-certificates/harbor-192.168.6.194.crt
sudo update-ca-certificates
# RHEL/Rocky: sudo cp ca.crt /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust

# Cách riêng cho containerd (nếu containerd có config_path = "/etc/containerd/certs.d"):
sudo mkdir -p /etc/containerd/certs.d/192.168.6.194
sudo cp ca.crt /etc/containerd/certs.d/192.168.6.194/ca.crt
sudo tee /etc/containerd/certs.d/192.168.6.194/hosts.toml >/dev/null <<'EOF'
server = "https://192.168.6.194"

[host."https://192.168.6.194"]
  capabilities = ["pull", "resolve"]
  ca = "/etc/containerd/certs.d/192.168.6.194/ca.crt"
EOF

sudo systemctl restart containerd
```

Chỉ cần một trong hai cách. Cách hệ thống (`update-ca-certificates`) đơn giản và đủ cho phần lớn trường hợp.

**d. Kiểm tra từ node:**

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://192.168.6.194/v2/
# 401 = kết nối và TLS đều ổn (chưa đăng nhập)
# lỗi certificate = CA chưa đúng; timeout = security group/firewall chặn 443
sudo crictl pull --creds 'robot$live-commerce+k8s-pull:<ROBOT_SECRET>' 192.168.6.194/live-commerce/web:<TAG>
```

> Không SSH được vào node (cluster managed khóa node)? Hỏi CMC cách thêm CA tin cậy cho node pool. Hoặc đưa Harbor ra sau một **tên miền có chứng chỉ hợp lệ** (Let's Encrypt DNS-01), khi đó không cần bước này. Nếu đổi sang tên miền, thay `192.168.6.194` trong toàn bộ tài liệu bằng tên miền đó.

**e. Security group:** cho phép **node → 192.168.6.194:443**. Pod không cần gọi Harbor.

---

## 3. Namespace

```bash
kubectl create namespace live-commerce-dev
kubectl create namespace moderation-dev
```

---

## 4. Add-on nền: Sealed Secrets và Kong

Không có ArgoCD nên ta cài bằng Helm, dùng chính file values trong `platform/`. Luôn ghim phiên bản chart:

```bash
helm repo add sealed-secrets https://bitnami.github.io/sealed-secrets
helm repo add kong https://charts.konghq.com
helm repo update
helm search repo sealed-secrets/sealed-secrets --versions | head -3
helm search repo kong/kong --versions | head -3
```

Chọn phiên bản, ghi vào `<PIN_VERSION>` trong `argocd/apps/platform/*.yaml` để khi bật ArgoCD dùng đúng bản này.

### 4.1 Sealed Secrets controller

```bash
helm install sealed-secrets sealed-secrets/sealed-secrets --version <PIN_VERSION> \
  -n sealed-secrets --create-namespace -f platform/sealed-secrets/values.yaml
kubectl -n sealed-secrets rollout status deploy/sealed-secrets-controller
```

**Sao lưu ngay khóa bí mật của controller.** Mất khóa thì không giải mã lại được các file `*.sealed.yaml` đã commit. Lưu ở nơi an toàn, không commit:

```bash
kubectl -n sealed-secrets get secret -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > ~/lc-secrets/sealed-secrets-key.backup.yaml
chmod 600 ~/lc-secrets/sealed-secrets-key.backup.yaml
```

### 4.2 Kong (Ingress Controller, DB-less)

Giai đoạn thử: mở proxy Kong bằng **NodePort 32080** để truy cập thẳng `http://<NODE_IP>:32080`. Mục 10 mô tả cách đưa ra Internet qua HAProxy/LB.

```bash
helm install kong kong/kong --version <PIN_VERSION> -n kong --create-namespace \
  -f platform/kong/values.yaml \
  --set proxy.type=NodePort --set proxy.http.nodePort=32080 --set proxy.tls.nodePort=32443
kubectl -n kong get pods,svc
kubectl get ingressclass   # phải có "kong"
```

Kiểm tra security group cho phép máy của bạn truy cập `<NODE_IP>:32080`. Gọi thử `curl -i http://<NODE_IP>:32080/`; Kong chưa có route nên sẽ trả `404 no Route matched`, như vậy là đúng.

> Image Kong, Sealed Secrets, PostgreSQL, Redis kéo từ Docker Hub, nên node cần ra Internet. Nếu node không có Internet hoặc bị giới hạn tần suất pull của Docker Hub, tạo project **Proxy Cache** trên Harbor trỏ tới Docker Hub và đổi image sang `192.168.6.194/<proxy-project>/library/postgres:16-alpine`…

---

## 5. Tầng dữ liệu: PostgreSQL và Redis DBaaS của CMC Cloud

Không tự dựng container DB trong cluster. Dữ liệu cá nhân và giao dịch nằm trên **DBaaS của CMC Cloud (HCM-1)**, cluster chỉ giữ ứng dụng không trạng thái. Repo infra **không có** manifest cho DB; cluster chỉ cần biết endpoint qua `DATABASE_URL`/`REDIS_URL` trong Secret (mục 7).

Mỗi service tự tạo bảng khi khởi động (migration có advisory lock, nhiều replica không chạy chồng). product-service tự seed 3 sản phẩm. Vì vậy chỉ cần tạo **database rỗng** và user.

### 5.1 Tạo instance trên CMC Cloud Console

| Hạng mục | PostgreSQL | Redis |
| --- | --- | --- |
| Phiên bản | 14 trở lên (đã thử với 16) | 6/7, chế độ **standalone hoặc primary–replica** |
| Mạng | Cùng VPC/subnet với cluster, dùng **private endpoint** | Như PostgreSQL |
| Truy cập | Allow-list dải IP **node** của cluster (pod ra ngoài qua SNAT IP node) | Như PostgreSQL |
| Database / user | DB `livecommerce` (prod), `livecommerce_dev` (dev); user riêng cho từng môi trường, quyền owner trên DB đó | Bật mật khẩu (AUTH) |
| Sao lưu | Bật backup tự động + PITR (thay cho PVC/Velero của DB) | Bật persistence nếu có (tồn kho phiên live) |

Lưu ý tương thích với code:

- **Redis không dùng cluster mode.** Code dùng `new Redis(url)` (ioredis một node), gặp endpoint cluster sẽ lỗi `MOVED`.
- **TLS PostgreSQL** (hậu tố `?sslmode=` của `DATABASE_URL`, hoặc `PG_SSLMODE` trong file env ở mục 7.4): để trống nếu DBaaS không bật SSL. Dùng `no-verify` nếu bắt buộc SSL mà chứng chỉ do CA riêng của CMC ký. Chỉ dùng `verify-full` khi chứng chỉ được CA công khai ký. Driver `pg` hiện tại coi `require` như `verify-full`.
- **TLS Redis** (`REDIS_TLS=1` → `rediss://`): ioredis luôn kiểm tra chứng chỉ, nên chỉ bật khi chứng chỉ được CA công khai ký.
- Mật khẩu có ký tự đặc biệt phải được mã hóa URL khi ghép vào `DATABASE_URL`/`REDIS_URL` (hàm `urlenc` ở mục 7.2; `scripts/deploy.sh` tự làm).

### 5.2 Instance đang dùng (HCM-1, VPC `cmc-prod-hcm1-vpc` / `subnet-isolated`)

| | PostgreSQL `rds_postgres-stream-platform` | Redis `redis_production` |
| --- | --- | --- |
| Phiên bản / chế độ | PostgreSQL 15, Master–Slave, 3 node | Redis 6.0, Standalone |
| Endpoint dùng trong `DATABASE_URL` / `REDIS_URL` | **`192.168.7.167:5432` (write)** | `192.168.7.117:6379` |
| Endpoint không dùng | `192.168.7.166`, `192.168.7.252` (read-only replica) | |
| User / mật khẩu | Tab **Users** của instance | Đặt lúc tạo instance / Redis Configuration |
| Database | Tạo ở tab **Database**: `livecommerce_dev` (dev), `livecommerce` (prod), cấp quyền cho user | Không cần |

Lưu ý:

- **Phải dùng host write.** Ứng dụng ghi dữ liệu và tự chạy migration lúc khởi động; trỏ vào replica sẽ lỗi `cannot execute ... in a read-only transaction`.
- DBaaS nằm ở `subnet-isolated`: security group / Palo Alto phải cho **các worker node** đi tới `192.168.7.0/24` cổng 5432 và 6379.
- Dev và prod dùng chung hai instance này: tách **database** Postgres theo môi trường. Redis dùng chung thì key của dev và prod lẫn nhau (giỏ hàng, phiên live); chấp nhận được cho demo, còn lại nên tạo Redis riêng cho dev.

### 5.3 Kiểm tra từ máy làm việc (qua VPN)

```bash
nc -vz 192.168.7.167 5432
nc -vz 192.168.7.117 6379
```

Kiểm tra từ **trong cluster** (pod đi ra bằng IP node, nên đây mới là phép thử đúng). Dùng image product-service đã có trên Harbor, không cần kéo image từ Internet:

```bash
kubectl -n live-commerce-dev run netcheck --rm -i --restart=Never --image=192.168.6.194/live-commerce/product-service:<TAG> \
  --overrides='{"spec":{"imagePullSecrets":[{"name":"harbor-registry"}]}}' --command -- node -e '
  const net=require("net");
  for (const [h,p] of [["192.168.7.167",5432],["192.168.7.117",6379]]) {
    const s=net.connect({host:h,port:p,timeout:3000},()=>{console.log("ok  ",h,p);s.destroy()});
    s.on("timeout",()=>{console.log("FAIL",h,p,"timeout");s.destroy()}); s.on("error",e=>console.log("FAIL",h,p,e.code));
  }'
```

Cần Secret `harbor-registry` (mục 7.2) để kéo image. `scripts/deploy.sh` cũng chạy bước kiểm tra này tự động trước khi apply ứng dụng.

---

## 6. Bổ sung manifest ứng dụng còn thiếu

Repo infra hiện **chưa có `auth-service` và `live-manager`**, route Kong còn thiếu `/api/auth`, `/api/sellers`, `/api/sessions`, `/api/lives`, và còn placeholder `harbor.example.local` / `<REDIS_HOST>`. Làm các bước sau.

### 6.1 Đổi registry sang Harbor thật

```bash
grep -rl 'harbor.example.local' apps | xargs sed -i '' 's#harbor.example.local#192.168.6.194#g'   # Linux: sed -i (bỏ '')
grep -rn '192.168.6.194' apps | head
```

### 6.2 auth-service (`apps/live-commerce/base/auth-service/`)

**`kustomization.yaml`**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - deployment.yaml
  - service.yaml
  - configmap.yaml
```

**`configmap.yaml`**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: auth-service-config
data:
  PORT: "3006"
```

**`deployment.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: auth-service
  labels:
    app.kubernetes.io/name: auth-service
    app.kubernetes.io/part-of: live-commerce
spec:
  replicas: 2
  selector:
    matchLabels:
      app.kubernetes.io/name: auth-service
  template:
    metadata:
      labels:
        app.kubernetes.io/name: auth-service
        app.kubernetes.io/part-of: live-commerce
    spec:
      imagePullSecrets:
        - name: harbor-registry
      containers:
        - name: auth-service
          image: 192.168.6.194/live-commerce/auth-service:latest # tag thật đặt trong overlay (images:)
          ports:
            - name: http
              containerPort: 3006
          envFrom:
            - configMapRef:
                name: auth-service-config
            - secretRef:
                name: auth-service-secret # DATABASE_URL, JWT_SECRET
          readinessProbe:
            httpGet:
              path: /readyz
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 15
            periodSeconds: 20
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
```

> Băm mật khẩu bằng scrypt tốn CPU. Nếu đăng nhập chậm khi tải cao, tăng `limits.cpu` hoặc số replica trước.

**`service.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: auth-service
  labels:
    app.kubernetes.io/name: auth-service
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: auth-service
  ports:
    - name: http
      port: 80
      targetPort: http
```

### 6.3 live-manager (`apps/live-commerce/base/live-manager/`)

**`kustomization.yaml`**: như auth-service (deployment, service, configmap).

**`configmap.yaml`**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: live-manager-config
data:
  PORT: "3005"
  PRODUCT_SERVICE_URL: "http://product-service"
  CHAT_SERVICE_URL: "http://chat-service"
  # mock: không gọi AWS, mọi kênh phát luồng demo. aws: kênh IVS thật (cần AWS_* trong live-manager-secret).
  IVS_MODE: "mock"
  # Bắt buộc khi IVS_MODE=mock: trong code mặc định là rỗng (chỉ docker-compose mới có giá trị mặc định).
  MOCK_PLAYBACK_URL: "https://fcc3ddae59ed.us-west-2.playback.live-video.net/api/video/v1/us-west-2.893648527354.channel.DmumNckWFTqz.m3u8"
  AWS_REGION: "ap-northeast-1"
  IVS_RECORDING_CONFIGURATION_ARN: ""
```

**`deployment.yaml`**: giống auth-service, đổi các chỗ sau:

```yaml
metadata:
  name: live-manager
  # labels app.kubernetes.io/name: live-manager (cả selector và template)
...
- name: live-manager
  image: 192.168.6.194/live-commerce/live-manager:latest
  ports:
    - name: http
      containerPort: 3005
  envFrom:
    - configMapRef:
        name: live-manager-config
    - secretRef:
        name: live-manager-secret # DATABASE_URL, REDIS_URL, ADMIN_TOKEN, INTERNAL_API_TOKEN, [AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY]
```

**`service.yaml`**: giống auth-service, đổi tên và selector thành `live-manager`.

### 6.4 Sửa các ConfigMap có sẵn

Thông tin nhạy cảm (`REDIS_URL` có mật khẩu, `DATABASE_URL`) chuyển hết sang Secret, nên **xóa dòng `REDIS_URL: "redis://<REDIS_HOST>:6379"`** khỏi:

- `apps/live-commerce/base/chat-service/configmap.yaml` (còn `PORT: "3001"`)
- `apps/live-commerce/base/product-service/configmap.yaml` (còn `PORT: "3002"`; product-service không dùng Redis)
- `apps/live-commerce/base/order-service/configmap.yaml`: còn `PORT: "3003"` và thêm:
  ```yaml
  # true: khách chưa đăng nhập vẫn đặt được. Prod đặt "false".
  ALLOW_GUEST_ORDERS: "true"
  ```

`apps/moderation/base/moderation-service/configmap.yaml` thêm hai biến:

```yaml
data:
  PORT: "3004"
  CHAT_SERVICE_URL: "http://chat-service.live-commerce.svc.cluster.local"
  LIVE_MANAGER_URL: "http://live-manager.live-commerce.svc.cluster.local"
  CHAT_ROOM: "main"
```

Nên đổi `readinessProbe.httpGet.path` của chat, product, order và moderation từ `/healthz` sang **`/readyz`** (kiểm tra cả PostgreSQL/Redis), giữ `livenessProbe` ở `/healthz`:

```bash
for f in apps/live-commerce/base/{chat,product,order}-service/deployment.yaml apps/moderation/base/moderation-service/deployment.yaml; do
  perl -0pi -e 's#(readinessProbe:\n\s+httpGet:\n\s+path: )/healthz#$1/readyz#' "$f"
done
```

### 6.5 Thêm vào base kustomization và route Kong

**`apps/live-commerce/base/kustomization.yaml`**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - chat-service
  - product-service
  - order-service
  - auth-service
  - live-manager
  - web
  - kong-routes.yaml
```

**`apps/live-commerce/base/kong-routes.yaml`**: thay toàn bộ bằng:

```yaml
# Định tuyến của Kong (ingress class kong) tới các service cùng namespace. Giống deploy/kong/kong.yml
# của repo app (strip-path=false). KHÔNG route /internal của live-manager, product-service, chat-service.
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: kong-routes
  annotations:
    konghq.com/strip-path: "false"
spec:
  ingressClassName: kong
  rules:
    - host: live.example.com
      http:
        paths:
          - path: /api/products
            pathType: Prefix
            backend:
              service:
                name: product-service
                port:
                  number: 80
          - path: /api/auth
            pathType: Prefix
            backend:
              service:
                name: auth-service
                port:
                  number: 80
          - path: /api/orders
            pathType: Prefix
            backend:
              service:
                name: order-service
                port:
                  number: 80
          - path: /api/sellers
            pathType: Prefix
            backend:
              service:
                name: live-manager
                port:
                  number: 80
          - path: /api/sessions
            pathType: Prefix
            backend:
              service:
                name: live-manager
                port:
                  number: 80
          - path: /api/lives
            pathType: Prefix
            backend:
              service:
                name: live-manager
                port:
                  number: 80
          - path: /ws
            pathType: Prefix
            backend:
              service:
                name: chat-service
                port:
                  number: 80
          - path: /
            pathType: Prefix
            backend:
              service:
                name: web
                port:
                  number: 80
```

Kết nối chat WebSocket sống lâu, nên thêm timeout 1 giờ cho Kong vào **`apps/live-commerce/base/chat-service/service.yaml`** (annotation đặt trên Service, Kong Ingress Controller đọc):

```yaml
metadata:
  name: chat-service
  annotations:
    konghq.com/read-timeout: "3600000"
    konghq.com/write-timeout: "3600000"
```

### 6.6 Overlay dev

**`apps/live-commerce/overlays/dev/kustomization.yaml`**: thêm 2 image mới, điền tag thật, đổi host:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: live-commerce-dev
resources:
  - ../../base
  # - secrets/        # chỉ bỏ chú thích khi dùng Sealed Secrets (mục 7.5)
images:
  - name: 192.168.6.194/live-commerce/chat-service
    newTag: "<TAG>"
  - name: 192.168.6.194/live-commerce/product-service
    newTag: "<TAG>"
  - name: 192.168.6.194/live-commerce/order-service
    newTag: "<TAG>"
  - name: 192.168.6.194/live-commerce/auth-service
    newTag: "<TAG>"
  - name: 192.168.6.194/live-commerce/live-manager
    newTag: "<TAG>"
  - name: 192.168.6.194/live-commerce/web
    newTag: "<TAG>"
patches:
  # giữ nguyên các patch có sẵn (replicas 1, HPA 1-3); chỉ đổi host:
  - target:
      kind: Ingress
      name: kong-routes
    patch: |-
      - op: replace
        path: /spec/rules/0/host
        value: cmc-cloud-hcm1.cloud      # đổi thành tên miền thật của bạn
```

Mỗi `<TAG>` là tag mới nhất của **repo đó** trên Harbor (mục 2.1). Không dùng `latest`.

**`apps/moderation/overlays/dev/kustomization.yaml`**: đặt tag, thêm patch `LIVE_MANAGER_URL` cạnh patch `CHAT_SERVICE_URL` có sẵn:

```yaml
resources:
  - ../../base
  # - secrets/        # chỉ khi dùng Sealed Secrets (mục 7.5)
images:
  - name: 192.168.6.194/live-commerce/moderation-service
    newTag: "<TAG>"
patches:
  # ... patch nodePort 30184 có sẵn ...
  - target:
      kind: ConfigMap
      name: moderation-service-config
    patch: |-
      - op: replace
        path: /data/CHAT_SERVICE_URL
        value: "http://chat-service.live-commerce-dev.svc.cluster.local"
      - op: replace
        path: /data/LIVE_MANAGER_URL
        value: "http://live-manager.live-commerce-dev.svc.cluster.local"
```

Kiểm tra build trước khi làm tiếp:

```bash
kubectl kustomize apps/live-commerce/overlays/dev | grep -E '^kind:|image:' | sort | uniq -c
```

---

## 7. Secrets: đưa thông tin DBaaS vào cluster

Trong `docker compose`, mỗi service đọc biến từ file `.env`. Trên K8s, thay `.env` bằng **Secret**: mỗi Deployment nạp toàn bộ khóa trong Secret của nó thành biến môi trường (`envFrom: secretRef`, ví dụ `apps/live-commerce/base/auth-service/deployment.yaml`). Code không đổi: vẫn đọc `DATABASE_URL`, `REDIS_URL`… như cũ.

Có ba cách tạo cùng một bộ Secret, **chọn một** cho mỗi môi trường:

| Cách | Khi nào dùng | Mục |
| --- | --- | --- |
| Lệnh `kubectl create secret` gõ tay | Đơn giản nhất, hiểu rõ từng bước. **Khuyến nghị cho lần đầu** | 7.2 |
| File env + `scripts/deploy.sh` (`SECRETS_ONLY=1`) | Muốn lưu giá trị trong một file ngoài repo để chạy lại nhanh | 7.4 |
| Sealed Secrets (file `*.sealed.yaml` commit vào Git) | Khi chuyển sang ArgoCD/GitOps | 7.5 |

Hai cách đầu tạo **Secret thường** thẳng vào cluster, không có gì lên Git, nên giữ dòng `# - secrets/` trong overlay **ở dạng chú thích**. Chỉ bỏ chú thích khi dùng 7.5.

### 7.1 Danh sách secret

| Secret                              | Namespace                         | Khóa                                                                                                                                               |
| ----------------------------------- | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `harbor-registry` (docker-registry) | live-commerce-dev, moderation-dev | robot account Harbor                                                                                                                               |
| `product-service-secret`            | live-commerce-dev                 | `DATABASE_URL`, `INTERNAL_API_TOKEN`                                                                                                               |
| `auth-service-secret`               | live-commerce-dev                 | `DATABASE_URL`, `JWT_SECRET`                                                                                                                       |
| `order-service-secret`              | live-commerce-dev                 | `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET`                                                                                                          |
| `chat-service-secret`               | live-commerce-dev                 | `REDIS_URL`, `INTERNAL_API_TOKEN`                                                                                                                  |
| `live-manager-secret`               | live-commerce-dev                 | `DATABASE_URL`, `REDIS_URL`, `ADMIN_TOKEN`, `INTERNAL_API_TOKEN`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (2 khóa AWS chỉ khi `IVS_MODE=aws`) |
| `moderation-service-secret`         | moderation-dev                    | `DATABASE_URL`, `WEBHOOK_SHARED_SECRET`, `INTERNAL_API_TOKEN`                                                                                      |

Ràng buộc:

- `JWT_SECRET` **giống nhau** ở auth và order, dài ít nhất 32 ký tự.
- `INTERNAL_API_TOKEN` **giống nhau** ở product, chat, live-manager, moderation.
- `DATABASE_URL`/`REDIS_URL` trỏ tới **endpoint DBaaS** (mục 5.1). Dạng `postgres://<user>:<pass>@<host>:5432/<db>` và `redis://:<pass>@<host>:6379`; mật khẩu phải được mã hóa URL (7.2 bước 2).
- Thiếu biến bắt buộc thì service dừng ngay với log `Thiếu biến môi trường …`.

### 7.2 Tạo Secret bằng lệnh kubectl (thủ công)

Chạy trong **Terminal của bạn** (iTerm/Terminal.app), các bước nối tiếp nhau **trong cùng một cửa sổ**, vì biến shell chỉ sống trong phiên đó. Không có giá trị nào được ghi ra file hay lịch sử lệnh.

**Bước 1: nhập thông tin đăng nhập DBaaS** (mật khẩu nhập ẩn). Shell mặc định của macOS là **zsh**, cú pháp `read` khác bash:

```bash
# zsh (mặc định trên macOS)
read -r "PG_USER?PG user: "; read -rs "PG_PASS?PG password: "; echo; read -rs "REDIS_PASS?Redis password: "; echo
read -rs "HARBOR_TOKEN?Harbor robot token: "; echo

# bash (Linux, hoặc sau khi gõ `bash`)
# read -rp 'PG user: ' PG_USER; read -rsp 'PG password: ' PG_PASS; echo; read -rsp 'Redis password: ' REDIS_PASS; echo
# read -rsp 'Harbor robot token: ' HARBOR_TOKEN; echo

# Kiểm tra đã nhận (chỉ in user và độ dài mật khẩu)
echo "user=$PG_USER pg_pass_len=${#PG_PASS} redis_pass_len=${#REDIS_PASS} harbor_len=${#HARBOR_TOKEN}"
```

**Bước 2: ghép chuỗi kết nối.** Mật khẩu có `@ : / #`… phải mã hóa URL, nếu không chuỗi bị hỏng:

```bash
urlenc() { printf '%s' "$1" | perl -pe 's/([^A-Za-z0-9._~-])/sprintf("%%%02X", ord($1))/ge'; }

# Host GHI của RDS Postgres (mục 5.1). Không dùng host read (.166/.252): service ghi dữ liệu và chạy migration.
DB_URL="postgres://$(urlenc "$PG_USER"):$(urlenc "$PG_PASS")@192.168.7.167:5432/livecommerce_dev"
# Gặp lỗi bắt buộc SSL thì thêm ?sslmode=no-verify vào cuối DB_URL (mục 5.1)

# Redis standalone; dấu ":" trước mật khẩu nghĩa là không có username
REDIS_URL="redis://:$(urlenc "$REDIS_PASS")@192.168.7.117:6379"
```

**Bước 3: sinh token nội bộ của ứng dụng** (thay cho các giá trị tương ứng trong `.env` cũ; dùng chung giữa các service, xem ràng buộc ở 7.1):

```bash
JWT_SECRET=$(openssl rand -hex 32); INTERNAL_API_TOKEN=$(openssl rand -hex 32)
ADMIN_TOKEN=$(openssl rand -hex 32); WEBHOOK_SHARED_SECRET=$(openssl rand -hex 32)
```

**Bước 4: tạo Secret.** Dạng `--dry-run=client -o yaml | kubectl apply -f -` để chạy lại được (cập nhật thay vì báo "already exists"):

```bash
APP=live-commerce-dev MOD=moderation-dev   # prod: live-commerce / moderation

for ns in $APP $MOD; do
  kubectl create secret docker-registry harbor-registry -n $ns --docker-server=192.168.6.194 \
    --docker-username='robot$live-commerce+k8s-pull' --docker-password="$HARBOR_TOKEN" \
    --dry-run=client -o yaml | kubectl apply -f -
done

kubectl create secret generic product-service-secret -n $APP \
  --from-literal=DATABASE_URL="$DB_URL" --from-literal=INTERNAL_API_TOKEN="$INTERNAL_API_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic auth-service-secret -n $APP \
  --from-literal=DATABASE_URL="$DB_URL" --from-literal=JWT_SECRET="$JWT_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic order-service-secret -n $APP \
  --from-literal=DATABASE_URL="$DB_URL" --from-literal=REDIS_URL="$REDIS_URL" --from-literal=JWT_SECRET="$JWT_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic chat-service-secret -n $APP \
  --from-literal=REDIS_URL="$REDIS_URL" --from-literal=INTERNAL_API_TOKEN="$INTERNAL_API_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic live-manager-secret -n $APP \
  --from-literal=DATABASE_URL="$DB_URL" --from-literal=REDIS_URL="$REDIS_URL" \
  --from-literal=ADMIN_TOKEN="$ADMIN_TOKEN" --from-literal=INTERNAL_API_TOKEN="$INTERNAL_API_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -
# Khi IVS_MODE=aws: thêm --from-literal=AWS_ACCESS_KEY_ID=... --from-literal=AWS_SECRET_ACCESS_KEY=... vào lệnh trên

kubectl create secret generic moderation-service-secret -n $MOD \
  --from-literal=DATABASE_URL="$DB_URL" --from-literal=WEBHOOK_SHARED_SECRET="$WEBHOOK_SHARED_SECRET" \
  --from-literal=INTERNAL_API_TOKEN="$INTERNAL_API_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -
```

**Bước 5: kiểm tra** (chỉ xem tên khóa và kích thước, không in giá trị):

```bash
kubectl -n live-commerce-dev get secrets
kubectl -n live-commerce-dev describe secret order-service-secret   # phải có DATABASE_URL, REDIS_URL, JWT_SECRET
kubectl -n moderation-dev describe secret moderation-service-secret
```

Nếu ứng dụng đã chạy, pod chỉ đọc biến môi trường lúc khởi động nên phải restart:

```bash
kubectl -n live-commerce-dev rollout restart deploy && kubectl -n moderation-dev rollout restart deploy
kubectl -n live-commerce-dev logs deploy/product-service --tail=30   # thấy migration chạy là đã vào được DB
```

### 7.3 Đổi mật khẩu DBaaS sau này

Chỉ làm lại bước 1, 2, rồi tạo lại **các Secret chứa `DATABASE_URL`/`REDIS_URL`**, sau đó restart. **Không** chạy lại bước 3 ở cửa sổ terminal mới rồi apply toàn bộ: token mới làm mọi phiên đăng nhập (JWT) mất hiệu lực, và nếu chỉ cập nhật một phần service thì `INTERNAL_API_TOKEN` lệch nhau, gọi nội bộ bị 401. Muốn giữ token cũ, lấy lại từ cluster trước khi tạo Secret:

```bash
JWT_SECRET=$(kubectl -n live-commerce-dev get secret auth-service-secret -o jsonpath='{.data.JWT_SECRET}' | base64 -d)
INTERNAL_API_TOKEN=$(kubectl -n live-commerce-dev get secret product-service-secret -o jsonpath='{.data.INTERNAL_API_TOKEN}' | base64 -d)
ADMIN_TOKEN=$(kubectl -n live-commerce-dev get secret live-manager-secret -o jsonpath='{.data.ADMIN_TOKEN}' | base64 -d)
WEBHOOK_SHARED_SECRET=$(kubectl -n moderation-dev get secret moderation-service-secret -o jsonpath='{.data.WEBHOOK_SHARED_SECRET}' | base64 -d)
```

> Secret thường chỉ được mã hóa base64: ai có quyền `get secret` trong namespace đọc được. Giới hạn RBAC, không xuất Secret ra file, không dán giá trị vào chat/ticket.

### 7.4 Cách khác: file env + script (Secret thường)

Giá trị được lưu trong file ngoài repo, script ghép URL và tạo đúng các Secret như 7.2. File env **không** commit.


```bash
mkdir -p ~/lc-secrets && chmod 700 ~/lc-secrets
cat > ~/lc-secrets/dev.env <<EOF
PG_HOST=
PG_PORT=5432
PG_USER=
PG_PASSWORD=
PG_DB=livecommerce_dev
PG_SSLMODE=
REDIS_HOST=
REDIS_PORT=6379
REDIS_USER=
REDIS_PASSWORD=
REDIS_TLS=0
JWT_SECRET=$(openssl rand -hex 32)
INTERNAL_API_TOKEN=$(openssl rand -hex 32)
ADMIN_TOKEN=$(openssl rand -hex 32)
WEBHOOK_SHARED_SECRET=$(openssl rand -hex 32)
HARBOR_ROBOT_USER=
HARBOR_ROBOT_TOKEN=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
EOF
chmod 600 ~/lc-secrets/dev.env
```

Không muốn tự tạo thì chạy `SECRETS_ONLY=1 ENV=dev bash scripts/deploy.sh` một lần: script sinh file mẫu có chú thích rồi dừng. Prod dùng `~/lc-secrets/prod.env`.

Mở file bằng trình soạn thảo, điền:

- `PG_*`, `REDIS_*`: endpoint, user, mật khẩu lấy ở trang chi tiết instance DBaaS (mục 5).
- `HARBOR_ROBOT_USER='robot$live-commerce+k8s-pull'`. Phải dùng **nháy đơn**, vì `$` sẽ bị shell hiểu là biến.
- `HARBOR_ROBOT_TOKEN`.
- Hai khóa AWS: chỉ điền khi dùng `IVS_MODE=aws`, lấy từ IAM user theo `live-commerce-app/docs/iam-live-manager-policy.json`.

Tạo/cập nhật Secret (không cần TAG, không đụng workload), rồi restart như 7.2 bước 5:

```bash
SECRETS_ONLY=1 ENV=dev bash scripts/deploy.sh
```

### 7.5 Cho GitOps: Sealed Secrets

Sealed Secrets mã hóa Secret bằng khóa công khai của controller (mục 4.1) để file `*.sealed.yaml` commit được vào Git; chỉ controller trong cluster giải mã được. Chưa dùng ArgoCD thì **không bắt buộc**. Nếu trước đó đã tạo Secret thường cùng tên (7.2/7.4), xóa chúng trước khi apply SealedSecret, vì controller không ghi đè Secret không do nó quản lý. Sau khi seal, bỏ chú thích `- secrets/` trong overlay.

Script dưới đọc file env ở 7.4.

Lưu thành `scripts/seal-secrets.sh` (chạy ở gốc repo infra), rồi `SECRETS_FILE=~/lc-secrets/dev.env bash scripts/seal-secrets.sh`. Script không in giá trị nào ra màn hình.

```bash
#!/usr/bin/env bash
# Sinh SealedSecret cho môi trường dev từ file env ngoài repo. Chỉ file *.sealed.yaml (đã mã hóa) được commit.
set -euo pipefail
set -a; . "${SECRETS_FILE:?Đặt SECRETS_FILE=đường dẫn file env}"; set +a

APP=live-commerce-dev MOD=moderation-dev
A=apps/live-commerce/overlays/dev/secrets
M=apps/moderation/overlays/dev/secrets
mkdir -p "$A" "$M"

urlenc() { printf '%s' "$1" | perl -pe 's/([^A-Za-z0-9._~-])/sprintf("%%%02X", ord($1))/ge'; }
DB_URL="postgres://$(urlenc "$PG_USER"):$(urlenc "$PG_PASSWORD")@${PG_HOST}:${PG_PORT}/$(urlenc "$PG_DB")"
[ -n "${PG_SSLMODE:-}" ] && DB_URL="${DB_URL}?sslmode=${PG_SSLMODE}"
REDIS_SCHEME=redis; [ "${REDIS_TLS:-0}" = 1 ] && REDIS_SCHEME=rediss
REDIS_URL="${REDIS_SCHEME}://$(urlenc "${REDIS_USER:-}"):$(urlenc "$REDIS_PASSWORD")@${REDIS_HOST}:${REDIS_PORT}"

# seal <namespace> <file đích> <tham số cho "kubectl create secret">
seal() {
  local ns=$1 out=$2; shift 2
  kubectl create secret "$@" -n "$ns" --dry-run=client -o yaml \
    | kubeseal --controller-name sealed-secrets-controller --controller-namespace sealed-secrets --format yaml > "$out"
  echo "ok  $out"
}

for pair in "$APP:$A" "$MOD:$M"; do
  seal "${pair%%:*}" "${pair#*:}/harbor-registry.sealed.yaml" docker-registry harbor-registry \
    --docker-server=192.168.6.194 --docker-username="$HARBOR_ROBOT_USER" --docker-password="$HARBOR_ROBOT_TOKEN"
done

seal "$APP" "$A/product-service-secret.sealed.yaml" generic product-service-secret \
  --from-literal=DATABASE_URL="$DB_URL" --from-literal=INTERNAL_API_TOKEN="$INTERNAL_API_TOKEN"
seal "$APP" "$A/auth-service-secret.sealed.yaml" generic auth-service-secret \
  --from-literal=DATABASE_URL="$DB_URL" --from-literal=JWT_SECRET="$JWT_SECRET"
seal "$APP" "$A/order-service-secret.sealed.yaml" generic order-service-secret \
  --from-literal=DATABASE_URL="$DB_URL" --from-literal=REDIS_URL="$REDIS_URL" --from-literal=JWT_SECRET="$JWT_SECRET"
seal "$APP" "$A/chat-service-secret.sealed.yaml" generic chat-service-secret \
  --from-literal=REDIS_URL="$REDIS_URL" --from-literal=INTERNAL_API_TOKEN="$INTERNAL_API_TOKEN"

aws_args=()
if [ -n "${AWS_ACCESS_KEY_ID:-}" ]; then
  aws_args=(--from-literal=AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" --from-literal=AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY")
fi
seal "$APP" "$A/live-manager-secret.sealed.yaml" generic live-manager-secret \
  --from-literal=DATABASE_URL="$DB_URL" --from-literal=REDIS_URL="$REDIS_URL" \
  --from-literal=ADMIN_TOKEN="$ADMIN_TOKEN" --from-literal=INTERNAL_API_TOKEN="$INTERNAL_API_TOKEN" ${aws_args[@]+"${aws_args[@]}"}

seal "$MOD" "$M/moderation-service-secret.sealed.yaml" generic moderation-service-secret \
  --from-literal=DATABASE_URL="$DB_URL" --from-literal=WEBHOOK_SHARED_SECRET="$WEBHOOK_SHARED_SECRET" \
  --from-literal=INTERNAL_API_TOKEN="$INTERNAL_API_TOKEN"

# kustomization liệt kê mọi file trong từng thư mục secrets/
for dir in "$A" "$M"; do
  {
    echo "apiVersion: kustomize.config.k8s.io/v1beta1"
    echo "kind: Kustomization"
    echo "resources:"
    for f in "$dir"/*.sealed.yaml; do echo "  - $(basename "$f")"; done
  } > "$dir/kustomization.yaml"
done
```

SealedSecret gắn chặt với **namespace và tên** lúc seal, nên không đổi namespace của file sau khi tạo. Đổi giá trị nào thì sửa `dev.env`, chạy lại script, rồi `kubectl apply -k` overlay tương ứng và restart deployment (mục 11).

---

## 8. Triển khai

Điều kiện: đã có đủ Secret (mục 7, bằng bất kỳ cách nào) và đã cài Kong/HAProxy (mục 4.2 hoặc 10).

### 8.1 Cách nhanh: script (Secret thường, không cần Sealed Secrets/ArgoCD)

```bash
ENV=dev TAG=<TAG> DRY_RUN=1 bash scripts/deploy.sh      # kiểm tra với API server, không đổi workload
ENV=dev TAG=<TAG> NODE_IP=<IP worker> bash scripts/deploy.sh
```

Script dùng file env ở mục 7.4: kiểm tra cluster và IngressClass `kong` → đọc `~/lc-secrets/$ENV.env` → tạo namespace + 8 Secret (ghi đè Secret tạo tay ở 7.2) → ghi tag vào overlay → thử TCP từ pod tới DBaaS → apply ứng dụng rồi moderation → chờ rollout → smoke test qua Kong. Prod: `ENV=prod`, script hỏi xác nhận trước khi chạy. Chỉ đổi Secret/ConfigMap thì thêm `RESTART=1`.

### 8.2 Thủ công từng bước (đã tạo Secret ở mục 7.2, 7.4 hoặc 7.5)

Thứ tự: ứng dụng → moderation. Không có bước dữ liệu vì DB là DBaaS.

```bash
kubectl apply -k apps/live-commerce/overlays/dev
kubectl -n live-commerce-dev get secrets   # đủ harbor-registry và 5 *-service-secret
for d in product-service auth-service order-service chat-service live-manager web; do
  kubectl -n live-commerce-dev rollout status deploy/$d --timeout=5m
done

kubectl apply -k apps/moderation/overlays/dev
kubectl -n moderation-dev rollout status deploy/moderation-service --timeout=5m
```

Thứ tự khởi động trong ứng dụng không bắt buộc. Pod nào phụ thuộc service chưa sẵn sàng (ví dụ live-manager cần product/chat) có thể restart vài lần rồi tự ổn.

Commit mọi thay đổi của repo infra, gồm manifest và `*.sealed.yaml` (nếu dùng). **Không** commit `~/lc-secrets`. Khi bật ArgoCD sau này, nó sẽ đọc đúng những gì đang chạy.

---

## 9. Kiểm tra

```bash
kubectl -n live-commerce-dev get pods -o wide
kubectl -n live-commerce-dev get ingress kong-routes
kubectl -n live-commerce-dev logs deploy/live-manager --tail=30
```

Gọi qua Kong, dùng header Host vì route gắn với host:

```bash
H='Host: cmc-cloud-hcm1.cloud'; K=http://<NODE_IP>:32080
curl -s -H "$H" $K/api/products | head -c 300; echo   # 3 sản phẩm seed
curl -s -H "$H" $K/api/lives; echo                    # [] lúc đầu
curl -s -o /dev/null -w '%{http_code}\n' -H "$H" $K/  # 200 (trang web)
curl -s -o /dev/null -w '%{http_code}\n' -H "$H" $K/internal/channels/open-session   # 404: /internal không lộ ra ngoài
```

Mở bằng trình duyệt: thêm `<NODE_IP> cmc-cloud-hcm1.cloud` vào `/etc/hosts` của máy bạn, rồi vào `http://cmc-cloud-hcm1.cloud:32080`.

Tạo người bán thử. Lệnh này đọc `ADMIN_TOKEN` từ file, không in ra màn hình; token người bán được ghi vào file:

```bash
ADMIN_TOKEN=$(grep -E '^ADMIN_TOKEN=' ~/lc-secrets/dev.env | cut -d= -f2-)
curl -s -X POST -H "$H" -H "x-admin-token: $ADMIN_TOKEN" -H 'content-type: application/json' \
  -d '{"name":"Shop Demo"}' $K/api/sellers > ~/lc-secrets/seller-demo.json
```

Danh sách kiểm tra:

| #   | Thao tác                                                                         | Kỳ vọng                                                               |
| --- | -------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| 1   | Trang chủ                                                                        | Hiện giao diện, không lỗi console                                     |
| 2   | Đăng ký / đăng nhập (`#/login`)                                                  | Thành công, F5 vẫn còn đăng nhập (auth-service + PostgreSQL)          |
| 3   | `#/seller`, dán token người bán, thêm sản phẩm, mở phiên                         | Phiên xuất hiện ở trang chủ                                           |
| 4   | Vào phiên                                                                        | Video demo chạy (mock); chat gửi/nhận được (WebSocket qua Kong `/ws`) |
| 5   | Ghim sản phẩm từ trang người bán                                                 | Thẻ sản phẩm hiện ngay trên trang xem                                 |
| 6   | Mua hàng                                                                         | Tồn kho giảm; `#/orders` có đơn                                       |
| 7   | `kubectl -n live-commerce-dev delete pod -l app.kubernetes.io/name=chat-service` | Chat tự kết nối lại                                                   |
| 8   | `kubectl -n live-commerce-dev rollout restart deploy/auth-service`               | Sau khi pod lên lại, vẫn đăng nhập được, đơn vẫn còn (dữ liệu ở DBaaS) |

Chuyển sang IVS thật: điền khóa AWS vào `dev.env`, chạy lại script seal, sửa `IVS_MODE: "aws"` (và `IVS_RECORDING_CONFIGURATION_ARN` nếu dùng) trong `live-manager/configmap.yaml`, rồi apply và restart live-manager. Người bán tạo **sau** khi đổi chế độ mới có kênh IVS thật.

---

## 10. Đưa ra ngoài: CMC ELB → HAProxy → Kong, tên miền, TLS

```
Người dùng ─► Cloudflare (DNS proxied, WAF, WebSockets bật, SSL Full strict)
           ─► CMC ELB            tự tạo từ Service "haproxy-ingress-loadbalancer" (type LoadBalancer), :80/:443
                                 pool = mọi worker node, cloud controller tự cập nhật khi thêm/bớt node
           ─► HAProxy Ingress    ns haproxy-ingress, DaemonSet, pod bind 8080/8443 (NodePort 30080/30443 để thử qua VPN)
                                 kết thúc TLS bằng chứng chỉ Cloudflare Origin (secret cloudflare-origin-tls, chứng chỉ mặc định)
                                 HTTP ─► HTTPS, X-Forwarded-For, timeout tunnel 1h cho WebSocket
           ─► Ingress "edge"     ns kong, class haproxy, MỌI host ─► kong-kong-proxy:80
           ─► Kong               ns kong, ClusterIP, DB-less; Ingress "kong-routes" khớp host + 8 path
           ─► product / auth / order / live-manager / chat (/ws) / web
```

| File | Vai trò |
| --- | --- |
| [platform/haproxy-ingress/values.yaml](../platform/haproxy-ingress/values.yaml) | DaemonSet, NodePort 30080/30443, tắt cổng stat/admin/QUIC, ssl-redirect, timeout tunnel 3600s |
| [platform/kong/values.yaml](../platform/kong/values.yaml) | Proxy ClusterIP (chỉ HTTP), 2 replica, tin `X-Forwarded-For` từ dải nội bộ |
| [platform/edge/ingress.yaml](../platform/edge/ingress.yaml) | Ingress không gắn host: HAProxy chuyển mọi host vào Kong. Đổi tên miền chỉ sửa overlay |
| [platform/edge/service-lb.yaml](../platform/edge/service-lb.yaml) | Service `type: LoadBalancer` chọn pod HAProxy, `targetPort` http/https (8080/8443). CMC Cloud tự tạo ELB |
| [scripts/install-edge.sh](../scripts/install-edge.sh) | Cài/cập nhật cả chuỗi bằng Helm + kubectl, idempotent |

### 10.1 Chứng chỉ origin

Cloudflare → SSL/TLS → Origin Server → Create Certificate, hostname `*.<tên miền>` và `<tên miền>`, định dạng PEM. Lưu thành `~/lc-secrets/origin.pem` và `~/lc-secrets/origin.key` (`chmod 600`), **không** commit.

### 10.2 Cài trong cluster

```bash
DRY_RUN=1 bash scripts/install-edge.sh        # kiểm tra với API server
TLS_CERT=~/lc-secrets/origin.pem TLS_KEY=~/lc-secrets/origin.key \
  NODE_IP=<IP worker> HOST=cmc-cloud-hcm1.cloud bash scripts/install-edge.sh
```

Script: tạo secret `cloudflare-origin-tls` (kiểm tra cert/key khớp nhau) → `helm upgrade --install` Kong 3.4.1 (ClusterIP) → HAProxy 1.54.2 → `kubectl apply -k platform/edge` (Ingress edge + Service LoadBalancer) → chờ CMC cấp EXTERNAL-IP cho ELB → smoke test qua NodePort 30443, redirect 30080 và qua ELB :443. Nếu Kong đang chạy NodePort 32080 (cài nhanh bằng `deploy.sh INSTALL_KONG=1`), script chuyển về ClusterIP; sau đó `deploy.sh` tự smoke test qua 30443.

Thử từ máy làm việc trước khi trỏ DNS:

```bash
curl -sk --resolve cmc-cloud-hcm1.cloud:30443:<NODE_IP> https://cmc-cloud-hcm1.cloud:30443/api/products | head -c 200
```

### 10.3 Ngoài cluster

1. **CMC ELB**: không tạo tay. Khi apply `service-lb.yaml`, cloud controller của CKS tạo ELB và gắn mọi worker vào pool. Xem địa chỉ:
   ```bash
   kubectl -n haproxy-ingress get svc haproxy-ingress-loadbalancer   # cột EXTERNAL-IP
   ```
   `EXTERNAL-IP` kẹt ở `<pending>` thì xem `kubectl -n haproxy-ingress describe svc haproxy-ingress-loadbalancer` (Events): thường do hết quota LB/IP public hoặc thiếu annotation riêng của CMC (public/internal, gói băng thông) — điền vào `metadata.annotations` theo tài liệu CMC Cloud. Không dùng được thì tạo ELB tay: listener 443 → mọi worker :30443. Nếu ELB có idle timeout, đặt ≥ 3600s cho WebSocket. **Không xoá Service này** khi đang chạy thật: xoá Service thì ELB và IP public bị thu hồi.
2. **Palo Alto / Security Group**: ELB chỉ nhận 80/443 từ dải IP Cloudflare. NodePort 30080/30443/32080 chỉ mở trong VPN/VPC.
3. **Cloudflare**: bản ghi `A` `live`/`dev.live` → `EXTERNAL-IP` của ELB, **Proxied**; SSL/TLS mode **Full (strict)**; Network → **WebSockets: On**.
4. **Tên miền thật**: sửa `value:` của patch host trong `apps/live-commerce/overlays/{dev,prod}/kustomization.yaml`, rồi `kubectl apply -k` overlay. Không cần sửa `platform/edge`.

Ghi chú:

- Sau LB có SNAT nên HAProxy thấy IP của LB; IP người dùng thật nằm ở header `CF-Connecting-IP` của Cloudflare.
- Webhook moderation (NodePort `30184` dev / `30084` prod) chỉ mở cho dải IP của VPN tới AWS; **không** đi qua LB/HAProxy/Kong.

---

## 11. Cập nhật phiên bản thủ công (thay cho CD)

Khi CI push tag mới cho một service:

```bash
cd apps/live-commerce/overlays/dev       # moderation-service: apps/moderation/overlays/dev
kustomize edit set image 192.168.6.194/live-commerce/order-service=192.168.6.194/live-commerce/order-service:<TAG_MOI>
cd - && kubectl apply -k apps/live-commerce/overlays/dev
kubectl -n live-commerce-dev rollout status deploy/order-service
git commit -am "chore(dev): order-service <TAG_MOI>"
```

Đổi ConfigMap/Secret thì pod **không tự nạp lại**. Sau khi apply, cần restart:

```bash
kubectl -n live-commerce-dev rollout restart deploy/live-manager
```

Rollback nhanh: `kubectl -n live-commerce-dev rollout undo deploy/order-service`. Sau đó sửa tag trong overlay về bản cũ, để repo khớp với cluster.

Xem trước thay đổi trước khi apply: `kubectl diff -k apps/live-commerce/overlays/dev`.

---

## 12. Xử lý sự cố

| Triệu chứng                                                         | Nguyên nhân thường gặp                                         | Cách xử lý                                                                                                   |
| ------------------------------------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `ImagePullBackOff`, `x509: certificate signed by unknown authority` | Node chưa tin CA Harbor                                        | Mục 2.3c trên **mọi** node, rồi restart containerd                                                           |
| `x509: … doesn't contain any IP SANs`                               | Chứng chỉ Harbor thiếu IP SAN                                  | Cấp lại chứng chỉ có `IP:192.168.6.194` (2.3b)                                                               |
| `ImagePullBackOff`, `unauthorized` / `401`                          | Thiếu hoặc sai `harbor-registry`, robot hết hạn, sai namespace | `kubectl -n <ns> get secret harbor-registry`; seal lại. Phải có ở **cả** live-commerce-dev và moderation-dev |
| `ImagePullBackOff`, `not found`                                     | Sai tag, hoặc repo chưa được push                              | Đối chiếu tag trên Harbor UI (2.1)                                                                           |
| `ImagePullBackOff`, `i/o timeout`                                   | Security group chặn node → 192.168.6.194:443                   | Mở rule; thử `curl https://192.168.6.194/v2/` từ node                                                        |
| `exec format error`                                                 | Image build sai kiến trúc (arm64 từ Mac)                       | Build `--platform linux/amd64` hoặc trên runner                                                              |
| `CrashLoopBackOff`, log `Thiếu biến môi trường X`                   | Secret/ConfigMap thiếu khóa, hoặc biến shell rỗng lúc tạo Secret | Bảng 7.1; `kubectl -n <ns> describe secret <tên>`; tạo lại (7.2) rồi restart                                |
| `JWT_SECRET phải dài ít nhất 32 ký tự`                              | JWT_SECRET ngắn                                                | `openssl rand -hex 32`                                                                                       |
| SealedSecret có nhưng không sinh Secret                             | Seal sai namespace/tên, hoặc seal bằng khóa của cluster khác   | `kubectl -n <ns> describe sealedsecret <tên>`; seal lại trên đúng cluster                                    |
| Readiness fail, log `ETIMEDOUT` / `ECONNREFUSED` tới DBaaS          | Allow-list DBaaS chưa có IP node, sai endpoint (public thay vì private) | Mục 5.1; chạy lại `scripts/deploy.sh` để xem bước kiểm tra TCP                                        |
| Log `password authentication failed` / `NOAUTH` / `WRONGPASS`       | Sai user/mật khẩu DBaaS, mật khẩu chưa mã hóa URL              | Tạo lại Secret theo 7.3 (hoặc sửa file env, chạy lại script với `RESTART=1`)                                 |
| Log `database "livecommerce_dev" does not exist`                    | Chưa tạo database trên DBaaS                                   | Tab **Database** của RDS (mục 5.2), cấp quyền cho user, restart                                              |
| Log `cannot execute ... in a read-only transaction`                 | `DATABASE_URL` trỏ vào host read replica                       | Dùng host write `192.168.7.167` (mục 5.2)                                                                    |
| `read: -p: no coprocess` khi nhập mật khẩu                          | Chạy lệnh `read` của bash trong zsh                            | Dùng dòng zsh ở 7.2 bước 1, hoặc gõ `bash` trước                                                             |
| Log `self-signed certificate` / `The server does not support SSL`   | `PG_SSLMODE` không khớp cấu hình SSL của DBaaS                 | Mục 5.1: đổi giữa trống / `no-verify` / `verify-full`                                                        |
| Log `MOVED` / `CROSSSLOT` từ Redis                                  | Đang dùng endpoint Redis cluster mode                          | Dùng instance standalone/primary–replica                                                                     |
| Kong trả `404 no Route matched`                                     | Sai header Host, hoặc Ingress chưa được Kong nhận              | `kubectl get ingress -A`; `kubectl -n kong logs deploy/kong-kong -c ingress-controller`                      |
| Chat "Mất kết nối" / WebSocket đóng sau ~60 s                       | Thiếu annotation timeout, Cloudflare tắt WebSockets, LB idle timeout ngắn | Mục 6.5; `timeout-tunnel` HAProxy (mục 10); bật WebSockets trên Cloudflare; tăng idle timeout LB |
| Cloudflare lỗi 526 (Invalid SSL certificate)                        | HAProxy đang dùng chứng chỉ tự ký                              | Chạy lại `scripts/install-edge.sh` với `TLS_CERT`/`TLS_KEY` (mục 10.1)                                       |
| Cloudflare lỗi 522/521                                              | ELB chưa có/không tới được pod HAProxy, firewall chặn          | `kubectl -n haproxy-ingress get svc,endpoints haproxy-ingress-loadbalancer`; `curl -k --resolve <host>:443:<EXTERNAL-IP>` |
| `EXTERNAL-IP` mãi `<pending>`                                       | Hết quota LB/IP public, thiếu annotation của CMC               | `kubectl -n haproxy-ingress describe svc haproxy-ingress-loadbalancer` (Events); mục 10.3                     |
| Trang xem: "Phiên live chưa có playback URL"                        | Người bán tạo khi `MOCK_PLAYBACK_URL` rỗng                     | Đặt biến trong ConfigMap (6.3), restart, tạo người bán mới                                                   |
| HPA `<unknown>/70%`                                                 | Thiếu metrics-server                                           | Cài metrics-server hoặc bỏ qua ở dev                                                                         |

Lệnh hay dùng:

```bash
kubectl -n live-commerce-dev get events --sort-by=.lastTimestamp | tail -20
kubectl -n live-commerce-dev describe pod <pod>
kubectl -n live-commerce-dev logs <pod> --previous     # log lần chạy trước khi crash
kubectl -n live-commerce-dev exec deploy/order-service -- wget -qO- http://127.0.0.1:3003/readyz
```

---

## 13. Checklist tổng

- [ ] kubeconfig hoạt động; ghi lại IP node, kiến trúc CPU
- [ ] Harbor có đủ 7 image; `HARBOR_REGISTRY=192.168.6.194`; tạm tắt job `update-manifests`
- [ ] Robot account pull-only
- [ ] Chứng chỉ Harbor có IP SAN; **mọi node** tin CA; `curl https://192.168.6.194/v2/` từ node trả 401
- [ ] Namespace `live-commerce-dev`, `moderation-dev`
- [ ] Sealed Secrets controller đã cài; khóa đã sao lưu
- [ ] Kong đã cài, có IngressClass `kong`, NodePort 32080 mở
- [ ] DBaaS PostgreSQL (host write `192.168.7.167`) + Redis standalone (`192.168.7.117`); đã tạo database + user; pod kết nối TCP được (5.3)
- [ ] Đã thêm `auth-service`, `live-manager`; sửa ConfigMap; route Kong đủ 8 path; annotation timeout cho chat
- [ ] Overlay dev: tag thật, patch `LIVE_MANAGER_URL` cho moderation (`secrets/` chỉ bật khi dùng Sealed Secrets)
- [ ] Đủ 7 loại secret (bảng 7.1), tạo bằng lệnh kubectl (7.2), `scripts/deploy.sh` (7.4) hoặc seal (7.5); không giá trị bí mật nào nằm trong Git
- [ ] `kubectl apply -k` cho live-commerce → moderation; mọi pod `Running`/`Ready`
- [ ] Đã qua bảng kiểm tra mục 9
- [ ] Commit thay đổi repo infra
- [ ] `scripts/install-edge.sh`: HAProxy (30443) → Kong ClusterIP; smoke test qua 30443 đạt
- [ ] Service `haproxy-ingress-loadbalancer` có EXTERNAL-IP (ELB do CMC tạo); firewall chỉ mở 80/443 cho Cloudflare; Cloudflare Full (strict) + WebSockets
- [ ] (Sau) `IVS_MODE=aws`; ArgoCD

# Migrate Live Commerce lên Kubernetes CMC Cloud (chưa có CD)

Hướng dẫn từng bước đưa toàn bộ ứng dụng (đang chạy bằng `docker compose` trong `live-commerce-app`) lên cluster Kubernetes của CMC Cloud.

- **Có:** CI build image và push lên Harbor `https://192.168.6.194` (VM cùng subnet với cluster).
- **Chưa có:** CD. Không dùng ArgoCD; ta tự `kubectl apply -k` từ repo này. Khi muốn bật ArgoCD sau, cấu trúc thư mục giữ nguyên, chỉ cần làm theo [argocd/install/README.md](../argocd/install/README.md).
- Môi trường đích: **dev** (namespace `live-commerce-dev`, `moderation-dev`). Prod làm y hệt với overlay `prod`.

> Quy ước trong tài liệu: `<NODE_IP>` = IP một worker node, `<TAG>` = tag image (8 ký tự đầu git SHA) do CI push. Các lệnh chạy từ **thư mục gốc repo `live-commerce-infra`**, trừ khi ghi khác.

---

## 0. Kiến trúc đích

```
Trình duyệt ──► <NODE_IP>:32080 (NodePort Kong, giai đoạn thử)       sau này: CMC LB ─► HAProxy ─► Kong
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
                                 namespace data: postgres (StatefulSet) + redis (StatefulSet)

moderation-dev: moderation-service ── NodePort 30184 (webhook từ AWS qua VPN, làm sau)
                                    └─► chat-service, live-manager (/internal), postgres

Mọi node kéo image từ Harbor 192.168.6.194/live-commerce/<service>:<TAG> (imagePullSecret harbor-registry)
```

| Thành phần | Image | Cổng container | Namespace |
|---|---|---|---|
| product-service | `192.168.6.194/live-commerce/product-service` | 3002 | live-commerce-dev |
| auth-service | `…/auth-service` | 3006 | live-commerce-dev |
| order-service | `…/order-service` | 3003 | live-commerce-dev |
| chat-service | `…/chat-service` | 3001 | live-commerce-dev |
| live-manager | `…/live-manager` | 3005 | live-commerce-dev |
| web (nginx) | `…/web` | 80 | live-commerce-dev |
| moderation-service | `…/moderation-service` | 3004 | moderation-dev |
| PostgreSQL 16, Redis 7 | `postgres:16-alpine`, `redis:7-alpine` (Docker Hub) | 5432, 6379 | data |

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
- **StorageClass** (cần cho PostgreSQL và Redis):
  ```bash
  kubectl get storageclass
  ```
  Nếu không có class nào gắn `(default)`, ghi tên class sẽ dùng để điền vào mục 5.
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
> ```yaml
>   update-manifests:
>     if: ${{ vars.INFRA_REPO != '' }}
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
kubectl create namespace data
```

---

## 4. Add-on nền: Sealed Secrets và Kong

Không có ArgoCD nên ta cài bằng Helm, dùng chính file values trong `platform/`. Luôn ghim phiên bản chart:

```bash
helm repo add sealed-secrets https://bitnami-labs.github.io/sealed-secrets
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

## 5. Tầng dữ liệu: PostgreSQL và Redis (namespace `data`)

Một PostgreSQL dùng chung cho mọi service (mỗi service tự tạo bảng khi khởi động; product-service tự seed 3 sản phẩm), và một Redis cho tồn kho trong phiên, chat pub/sub và trạng thái mock IVS. Nếu CMC Cloud có dịch vụ DB/Redis managed thì có thể dùng thay. Khi đó bỏ mục này và chỉ đổi `DATABASE_URL`/`REDIS_URL` ở mục 7.

Tạo các file sau trong repo infra:

**`apps/data/kustomization.yaml`**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: data
resources:
  - postgres.yaml
  - redis.yaml
  - secrets/
```

**`apps/data/postgres.yaml`**

```yaml
# PostgreSQL một replica cho dev. Service headless: postgres.data.svc.cluster.local trỏ thẳng vào pod.
apiVersion: v1
kind: Service
metadata:
  name: postgres
  labels:
    app.kubernetes.io/name: postgres
spec:
  clusterIP: None
  selector:
    app.kubernetes.io/name: postgres
  ports:
    - name: pg
      port: 5432
      targetPort: pg
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
  labels:
    app.kubernetes.io/name: postgres
spec:
  serviceName: postgres
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: postgres
  template:
    metadata:
      labels:
        app.kubernetes.io/name: postgres
    spec:
      containers:
        - name: postgres
          image: postgres:16-alpine
          ports:
            - name: pg
              containerPort: 5432
          envFrom:
            - secretRef:
                name: postgres-secret        # POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
          env:
            - name: PGDATA                   # thư mục con: gốc volume có lost+found làm initdb lỗi
              value: /var/lib/postgresql/data/pgdata
          readinessProbe:
            exec:
              command: ["sh", "-c", "pg_isready -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\""]
            periodSeconds: 10
          livenessProbe:
            exec:
              command: ["sh", "-c", "pg_isready -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\""]
            initialDelaySeconds: 30
            periodSeconds: 20
          resources:
            requests:
              cpu: 250m
              memory: 512Mi
            limits:
              cpu: "1"
              memory: 1Gi
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        # storageClassName: <tên class>   # bỏ chú thích nếu cluster không có StorageClass mặc định
        resources:
          requests:
            storage: 10Gi
```

**`apps/data/redis.yaml`**

```yaml
# Redis một replica, có mật khẩu và AOF (giữ tồn kho phiên live khi pod khởi động lại).
apiVersion: v1
kind: Service
metadata:
  name: redis
  labels:
    app.kubernetes.io/name: redis
spec:
  clusterIP: None
  selector:
    app.kubernetes.io/name: redis
  ports:
    - name: redis
      port: 6379
      targetPort: redis
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis
  labels:
    app.kubernetes.io/name: redis
spec:
  serviceName: redis
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: redis
  template:
    metadata:
      labels:
        app.kubernetes.io/name: redis
    spec:
      containers:
        - name: redis
          image: redis:7-alpine
          args: ["--requirepass", "$(REDIS_PASSWORD)", "--appendonly", "yes"]
          env:
            - name: REDIS_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: redis-secret
                  key: REDIS_PASSWORD
          ports:
            - name: redis
              containerPort: 6379
          readinessProbe:
            exec:
              command: ["sh", "-c", "redis-cli -a \"$REDIS_PASSWORD\" --no-auth-warning ping | grep -q PONG"]
            periodSeconds: 10
          livenessProbe:
            exec:
              command: ["sh", "-c", "redis-cli -a \"$REDIS_PASSWORD\" --no-auth-warning ping | grep -q PONG"]
            initialDelaySeconds: 15
            periodSeconds: 20
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
          volumeMounts:
            - name: data
              mountPath: /data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        # storageClassName: <tên class>
        resources:
          requests:
            storage: 2Gi
```

Thư mục `apps/data/secrets/` sẽ được tạo ở mục 7.

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
          image: 192.168.6.194/live-commerce/auth-service:latest   # tag thật đặt trong overlay (images:)
          ports:
            - name: http
              containerPort: 3006
          envFrom:
            - configMapRef:
                name: auth-service-config
            - secretRef:
                name: auth-service-secret        # DATABASE_URL, JWT_SECRET
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
  AWS_REGION: "ap-southeast-1"
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
                name: live-manager-secret   # DATABASE_URL, REDIS_URL, ADMIN_TOKEN, INTERNAL_API_TOKEN, [AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY]
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

**`apps/live-commerce/overlays/dev/kustomization.yaml`**: bật `secrets/`, thêm 2 image mới, điền tag thật, đổi host:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: live-commerce-dev
resources:
  - ../../base
  - secrets/
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
        value: dev.live.example.com      # đổi thành tên miền thật của bạn
```

Mỗi `<TAG>` là tag mới nhất của **repo đó** trên Harbor (mục 2.1). Không dùng `latest`.

**`apps/moderation/overlays/dev/kustomization.yaml`**: bật `secrets/`, đặt tag, thêm patch `LIVE_MANAGER_URL` cạnh patch `CHAT_SERVICE_URL` có sẵn:

```yaml
resources:
  - ../../base
  - secrets/
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

Kiểm tra build trước khi làm tiếp. Lệnh sẽ báo thiếu `secrets/` cho tới khi xong mục 7, đó là điều bình thường:

```bash
kubectl kustomize apps/live-commerce/overlays/dev | grep -E '^kind:|image:' | sort | uniq -c
```

---

## 7. Secrets (Sealed Secrets)

### 7.1 Danh sách secret

| Secret | Namespace | Khóa |
|---|---|---|
| `harbor-registry` (docker-registry) | live-commerce-dev, moderation-dev | robot account Harbor |
| `postgres-secret` | data | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |
| `redis-secret` | data | `REDIS_PASSWORD` |
| `product-service-secret` | live-commerce-dev | `DATABASE_URL`, `INTERNAL_API_TOKEN` |
| `auth-service-secret` | live-commerce-dev | `DATABASE_URL`, `JWT_SECRET` |
| `order-service-secret` | live-commerce-dev | `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET` |
| `chat-service-secret` | live-commerce-dev | `REDIS_URL`, `INTERNAL_API_TOKEN` |
| `live-manager-secret` | live-commerce-dev | `DATABASE_URL`, `REDIS_URL`, `ADMIN_TOKEN`, `INTERNAL_API_TOKEN`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (2 khóa AWS chỉ khi `IVS_MODE=aws`) |
| `moderation-service-secret` | moderation-dev | `DATABASE_URL`, `WEBHOOK_SHARED_SECRET`, `INTERNAL_API_TOKEN` |

Ràng buộc:

- `JWT_SECRET` **giống nhau** ở auth và order, dài ít nhất 32 ký tự.
- `INTERNAL_API_TOKEN` **giống nhau** ở product, chat, live-manager, moderation.
- Mật khẩu PostgreSQL/Redis dùng **hex** (`openssl rand -hex`) để nhúng vào URL không cần mã hóa ký tự.
- Thiếu biến bắt buộc thì service dừng ngay với log `Thiếu biến môi trường …`.

### 7.2 File giá trị gốc (ngoài repo, không commit)

```bash
mkdir -p ~/lc-secrets && chmod 700 ~/lc-secrets
cat > ~/lc-secrets/dev.env <<EOF
POSTGRES_USER=live
POSTGRES_DB=livecommerce
POSTGRES_PASSWORD=$(openssl rand -hex 24)
REDIS_PASSWORD=$(openssl rand -hex 24)
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

Mở file bằng trình soạn thảo, điền:

- `HARBOR_ROBOT_USER='robot$live-commerce+k8s-pull'`. Phải dùng **nháy đơn**, vì `$` sẽ bị shell hiểu là biến.
- `HARBOR_ROBOT_TOKEN`.
- Hai khóa AWS: chỉ điền khi dùng `IVS_MODE=aws`, lấy từ IAM user theo `live-commerce-app/docs/iam-live-manager-policy.json`.

### 7.3 Tạo và seal toàn bộ secret

Lưu thành `scripts/seal-secrets.sh` (chạy ở gốc repo infra), rồi `SECRETS_FILE=~/lc-secrets/dev.env bash scripts/seal-secrets.sh`. Script không in giá trị nào ra màn hình.

```bash
#!/usr/bin/env bash
# Sinh SealedSecret cho môi trường dev từ file env ngoài repo. Chỉ file *.sealed.yaml (đã mã hóa) được commit.
set -euo pipefail
set -a; . "${SECRETS_FILE:?Đặt SECRETS_FILE=đường dẫn file env}"; set +a

APP=live-commerce-dev MOD=moderation-dev DATA=data
A=apps/live-commerce/overlays/dev/secrets
M=apps/moderation/overlays/dev/secrets
D=apps/data/secrets
mkdir -p "$A" "$M" "$D"

DB_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres.data.svc.cluster.local:5432/${POSTGRES_DB}"
REDIS_URL="redis://:${REDIS_PASSWORD}@redis.data.svc.cluster.local:6379"

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

seal "$DATA" "$D/postgres-secret.sealed.yaml" generic postgres-secret \
  --from-literal=POSTGRES_USER="$POSTGRES_USER" --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  --from-literal=POSTGRES_DB="$POSTGRES_DB"
seal "$DATA" "$D/redis-secret.sealed.yaml" generic redis-secret --from-literal=REDIS_PASSWORD="$REDIS_PASSWORD"

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
for dir in "$A" "$M" "$D"; do
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

Theo thứ tự: dữ liệu → ứng dụng → moderation.

```bash
# 1. PostgreSQL + Redis
kubectl apply -k apps/data
kubectl -n data rollout status statefulset/postgres --timeout=5m
kubectl -n data rollout status statefulset/redis --timeout=5m
kubectl -n data get pvc            # STATUS phải là Bound

# 2. Ứng dụng
kubectl apply -k apps/live-commerce/overlays/dev
kubectl -n live-commerce-dev get sealedsecrets,secrets   # SealedSecret phải sinh ra Secret cùng tên
for d in product-service auth-service order-service chat-service live-manager web; do
  kubectl -n live-commerce-dev rollout status deploy/$d --timeout=5m
done

# 3. Moderation
kubectl apply -k apps/moderation/overlays/dev
kubectl -n moderation-dev rollout status deploy/moderation-service --timeout=5m
```

Thứ tự khởi động trong ứng dụng không bắt buộc. Pod nào phụ thuộc service chưa sẵn sàng (ví dụ live-manager cần product/chat) có thể restart vài lần rồi tự ổn.

Commit mọi thay đổi của repo infra, gồm manifest mới và `*.sealed.yaml`. **Không** commit `~/lc-secrets`. Khi bật ArgoCD sau này, nó sẽ đọc đúng những gì đang chạy.

---

## 9. Kiểm tra

```bash
kubectl -n live-commerce-dev get pods -o wide
kubectl -n live-commerce-dev get ingress kong-routes
kubectl -n live-commerce-dev logs deploy/live-manager --tail=30
```

Gọi qua Kong, dùng header Host vì route gắn với host:

```bash
H='Host: dev.live.example.com'; K=http://<NODE_IP>:32080
curl -s -H "$H" $K/api/products | head -c 300; echo   # 3 sản phẩm seed
curl -s -H "$H" $K/api/lives; echo                    # [] lúc đầu
curl -s -o /dev/null -w '%{http_code}\n' -H "$H" $K/  # 200 (trang web)
curl -s -o /dev/null -w '%{http_code}\n' -H "$H" $K/internal/channels/open-session   # 404: /internal không lộ ra ngoài
```

Mở bằng trình duyệt: thêm `<NODE_IP> dev.live.example.com` vào `/etc/hosts` của máy bạn, rồi vào `http://dev.live.example.com:32080`.

Tạo người bán thử. Lệnh này đọc `ADMIN_TOKEN` từ file, không in ra màn hình; token người bán được ghi vào file:

```bash
ADMIN_TOKEN=$(grep -E '^ADMIN_TOKEN=' ~/lc-secrets/dev.env | cut -d= -f2-)
curl -s -X POST -H "$H" -H "x-admin-token: $ADMIN_TOKEN" -H 'content-type: application/json' \
  -d '{"name":"Shop Demo"}' $K/api/sellers > ~/lc-secrets/seller-demo.json
```

Danh sách kiểm tra:

| # | Thao tác | Kỳ vọng |
|---|---|---|
| 1 | Trang chủ | Hiện giao diện, không lỗi console |
| 2 | Đăng ký / đăng nhập (`#/login`) | Thành công, F5 vẫn còn đăng nhập (auth-service + PostgreSQL) |
| 3 | `#/seller`, dán token người bán, thêm sản phẩm, mở phiên | Phiên xuất hiện ở trang chủ |
| 4 | Vào phiên | Video demo chạy (mock); chat gửi/nhận được (WebSocket qua Kong `/ws`) |
| 5 | Ghim sản phẩm từ trang người bán | Thẻ sản phẩm hiện ngay trên trang xem |
| 6 | Mua hàng | Tồn kho giảm; `#/orders` có đơn |
| 7 | `kubectl -n live-commerce-dev delete pod -l app.kubernetes.io/name=chat-service` | Chat tự kết nối lại |
| 8 | `kubectl -n data delete pod postgres-0` | Sau khi pod lên lại, dữ liệu (tài khoản, đơn) vẫn còn (PVC) |

Chuyển sang IVS thật: điền khóa AWS vào `dev.env`, chạy lại script seal, sửa `IVS_MODE: "aws"` (và `IVS_RECORDING_CONFIGURATION_ARN` nếu dùng) trong `live-manager/configmap.yaml`, rồi apply và restart live-manager. Người bán tạo **sau** khi đổi chế độ mới có kênh IVS thật.

---

## 10. Đưa ra ngoài: tên miền, TLS

Thiết kế trong repo: **CMC Load Balancer → HAProxy Ingress (NodePort 30080/30443) → Kong (ClusterIP) → service**, TLS bằng chứng chỉ origin của Cloudflare ([platform/edge/ingress.yaml](../platform/edge/ingress.yaml)).

1. Cài HAProxy Ingress: `helm repo add haproxytech https://haproxytech.github.io/helm-charts`, rồi `helm install haproxy-ingress haproxytech/kubernetes-ingress --version <PIN_VERSION> -n haproxy-ingress --create-namespace -f platform/haproxy-ingress/values.yaml`.
2. Tạo SealedSecret `cloudflare-cert` (loại `tls`) trong namespace `kong`, đổi host thật trong `platform/edge/ingress.yaml`, rồi `kubectl apply -k platform/edge`.
3. Đưa Kong về ClusterIP: `helm upgrade kong kong/kong --version <PIN_VERSION> -n kong -f platform/kong/values.yaml`.
4. CMC Load Balancer: listener 443 → các node, cổng 30443 (health check TCP). Cloudflare: bản ghi DNS trỏ về IP LB, **bật WebSockets**, SSL mode *Full (strict)*.
5. Đổi `host` trong overlay dev thành tên miền thật, rồi apply lại.

Cách đơn giản hơn nếu chưa cần HAProxy: trỏ LB thẳng vào NodePort của Kong (32080/32443) và cấu hình TLS ở Kong hoặc LB.

Webhook moderation (NodePort `30184`) chỉ mở cho dải IP của VPN tới AWS; không đưa qua Kong.

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

| Triệu chứng | Nguyên nhân thường gặp | Cách xử lý |
|---|---|---|
| `ImagePullBackOff`, `x509: certificate signed by unknown authority` | Node chưa tin CA Harbor | Mục 2.3c trên **mọi** node, rồi restart containerd |
| `x509: … doesn't contain any IP SANs` | Chứng chỉ Harbor thiếu IP SAN | Cấp lại chứng chỉ có `IP:192.168.6.194` (2.3b) |
| `ImagePullBackOff`, `unauthorized` / `401` | Thiếu hoặc sai `harbor-registry`, robot hết hạn, sai namespace | `kubectl -n <ns> get secret harbor-registry`; seal lại. Phải có ở **cả** live-commerce-dev và moderation-dev |
| `ImagePullBackOff`, `not found` | Sai tag, hoặc repo chưa được push | Đối chiếu tag trên Harbor UI (2.1) |
| `ImagePullBackOff`, `i/o timeout` | Security group chặn node → 192.168.6.194:443 | Mở rule; thử `curl https://192.168.6.194/v2/` từ node |
| `exec format error` | Image build sai kiến trúc (arm64 từ Mac) | Build `--platform linux/amd64` hoặc trên runner |
| `CrashLoopBackOff`, log `Thiếu biến môi trường X` | Secret/ConfigMap thiếu khóa | Bảng 7.1; `kubectl -n <ns> describe pod`; seal lại rồi restart |
| `JWT_SECRET phải dài ít nhất 32 ký tự` | JWT_SECRET ngắn | `openssl rand -hex 32` |
| SealedSecret có nhưng không sinh Secret | Seal sai namespace/tên, hoặc seal bằng khóa của cluster khác | `kubectl -n <ns> describe sealedsecret <tên>`; seal lại trên đúng cluster |
| Pod `Pending`, PVC `Pending` | Không có StorageClass mặc định | Điền `storageClassName` ở mục 5 |
| Readiness fail, log `ECONNREFUSED postgres…` / `NOAUTH` | DB chưa lên, sai mật khẩu, hoặc sai tên host | `kubectl -n data get pods`; kiểm tra `DATABASE_URL`/`REDIS_URL` trỏ `*.data.svc.cluster.local` |
| Kong trả `404 no Route matched` | Sai header Host, hoặc Ingress chưa được Kong nhận | `kubectl get ingress -A`; `kubectl -n kong logs deploy/kong-kong -c ingress-controller` |
| Chat "Mất kết nối" / WebSocket đóng sau ~60 s | Thiếu annotation timeout, Cloudflare tắt WebSockets | Mục 6.5; bật WebSockets trên Cloudflare |
| Trang xem: "Phiên live chưa có playback URL" | Người bán tạo khi `MOCK_PLAYBACK_URL` rỗng | Đặt biến trong ConfigMap (6.3), restart, tạo người bán mới |
| HPA `<unknown>/70%` | Thiếu metrics-server | Cài metrics-server hoặc bỏ qua ở dev |

Lệnh hay dùng:

```bash
kubectl -n live-commerce-dev get events --sort-by=.lastTimestamp | tail -20
kubectl -n live-commerce-dev describe pod <pod>
kubectl -n live-commerce-dev logs <pod> --previous     # log lần chạy trước khi crash
kubectl -n live-commerce-dev exec deploy/order-service -- wget -qO- http://127.0.0.1:3003/readyz
```

---

## 13. Checklist tổng

- [ ] kubeconfig hoạt động; ghi lại IP node, kiến trúc CPU, StorageClass
- [ ] Harbor có đủ 7 image; `HARBOR_REGISTRY=192.168.6.194`; tạm tắt job `update-manifests`
- [ ] Robot account pull-only
- [ ] Chứng chỉ Harbor có IP SAN; **mọi node** tin CA; `curl https://192.168.6.194/v2/` từ node trả 401
- [ ] Namespace `live-commerce-dev`, `moderation-dev`, `data`
- [ ] Sealed Secrets controller đã cài; khóa đã sao lưu
- [ ] Kong đã cài, có IngressClass `kong`, NodePort 32080 mở
- [ ] `apps/data` (PostgreSQL, Redis) chạy, PVC `Bound`
- [ ] Đã thêm `auth-service`, `live-manager`; sửa ConfigMap; route Kong đủ 8 path; annotation timeout cho chat
- [ ] Overlay dev: tag thật, `secrets/` bật, patch `LIVE_MANAGER_URL` cho moderation
- [ ] Đã seal đủ 9 loại secret (bảng 7.1); `~/lc-secrets` không nằm trong Git
- [ ] `kubectl apply -k` cho data → live-commerce → moderation; mọi pod `Running`/`Ready`
- [ ] Đã qua bảng kiểm tra mục 9
- [ ] Commit thay đổi repo infra
- [ ] (Sau) HAProxy + LB + Cloudflare/TLS; `IVS_MODE=aws`; ArgoCD

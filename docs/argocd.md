# Hướng dẫn dùng ArgoCD cho Live Commerce

Tài liệu này có hai phần:
- **Phần A**: các thành phần trong repo `live-commerce-infra` và vai trò của từng phần.
- **Phần B**: cài ArgoCD, bootstrap, cách làm việc hằng ngày, chuyển từ `scripts/deploy.sh` sang ArgoCD, rollback và xử lý sự cố.

Đọc kèm: [gitops-flow.md](gitops-flow.md) (ai làm gì), [environments.md](environments.md) (dev/prod), [secrets.md](secrets.md) (Sealed Secrets), [migrate-cmc-k8s.md](migrate-cmc-k8s.md) (luồng thủ công hiện tại).

---

## A. Repo infra gồm những gì

### A.1 Bức tranh tổng thể

```
           repo live-commerce-app                          repo live-commerce-infra (repo này)
  code ─▶ CI build-and-deploy ─▶ Harbor 192.168.6.194     apps/…/overlays/dev  newTag: <sha>  ◀── CI ghi tag
                                         ▲                         │
                                         │ pull image              │ ArgoCD theo dõi nhánh main
                                         │                         ▼
                              Kubernetes CMC (HCM-01) ◀──── ArgoCD (namespace argocd)
                                                            render kustomize/helm rồi apply

  Luồng request: Cloudflare ─▶ CMC ELB ─▶ HAProxy Ingress (TLS) ─▶ Kong ─▶ service trong live-commerce(-dev)
```

Repo app chứa **code**. Repo infra chứa **trạng thái mong muốn của cluster**: chạy image nào, bao nhiêu replica, cấu hình gì, route nào. ArgoCD liên tục so sánh Git với cluster và kéo cluster về đúng như Git.

### A.2 Bảng thành phần

| Đường dẫn | Là gì | Ai/cái gì dùng |
|---|---|---|
| `argocd/install/values.yaml` | Values cho Helm chart `argo/argo-cd`: `server.insecure: true` (TLS kết thúc ở ingress), reconcile mỗi 180 s | Bạn, khi cài ArgoCD lần đầu |
| `argocd/install/README.md` | Các bước cài nhanh | Bạn |
| `argocd/projects/platform.yaml` | AppProject `platform`: được deploy vào mọi namespace, được tạo resource cấp cluster (CRD, ClusterRole…) | Các app hạ tầng |
| `argocd/projects/live-commerce.yaml` | AppProject `live-commerce`: chỉ được deploy vào namespace `live-commerce*` và `moderation*`. Resource cấp cluster duy nhất được phép là Namespace | Các app nghiệp vụ, giới hạn phạm vi ảnh hưởng |
| `argocd/bootstrap/root-app.yaml` | Application `root` theo mô hình **App of Apps**: đọc toàn bộ `argocd/apps/` (recurse) rồi tạo các Application con. Bật prune + selfHeal | Apply bằng tay **một lần duy nhất** |
| `argocd/apps/platform/*.yaml` | Mỗi file là một Application hạ tầng (bảng A.3) | `root` tạo ra |
| `argocd/apps/workloads/*.yaml` | 4 Application nghiệp vụ: `live-commerce-dev`, `live-commerce-prod`, `moderation-dev`, `moderation-prod` | `root` tạo ra |
| `apps/live-commerce/base/` | Manifest gốc của 6 service: chat, product, order, auth, live-manager, web. Mỗi service có deployment, service, configmap, và hpa nếu cần. Có thêm `kong-routes.yaml` (Ingress class `kong`, định tuyến `/api/...`, `/ws` tới service, không mở `/internal`) | Overlay kế thừa |
| `apps/live-commerce/overlays/dev/` | Phần riêng của dev: namespace `live-commerce-dev`, **tag image** (`images: newTag`), host `dev.cmc-cloud-hcm1.cloud`, 1 replica, HPA 1–3 | App `live-commerce-dev` |
| `apps/live-commerce/overlays/prod/` | Phần riêng của prod: namespace `live-commerce`, host prod, tắt guest order | App `live-commerce-prod` |
| `apps/*/overlays/*/secrets/` | Nơi đặt file `*.sealed.yaml`. Hiện **đang tắt** (dòng `# - secrets/` trong kustomization), vì Secret đang được tạo bằng `deploy.sh` | Khi chuyển sang Sealed Secrets |
| `apps/moderation/base`, `overlays/` | moderation-service (webhook nhận sự kiện IVS qua NodePort 30184 ở dev, 30084 ở prod) | App `moderation-dev/prod` |
| `platform/edge/` | Ingress `edge` (class haproxy) chuyển mọi host tới `kong-kong-proxy:80`, timeout tunnel 1 h cho WebSocket. Service `LoadBalancer` để CMC tự tạo ELB trỏ về HAProxy 8080/8443 | App `edge` |
| `platform/haproxy-ingress/values.yaml` | HAProxy chạy DaemonSet, NodePort 30080/30443, chuyển HTTP sang HTTPS, thêm X-Forwarded-For | App `haproxy-ingress`, `scripts/install-edge.sh` |
| `platform/kong/values.yaml` | Kong DB-less, chỉ ClusterIP, tin X-Forwarded-For từ dải nội bộ | App `kong`, `install-edge.sh` |
| `platform/sealed-secrets/values.yaml` | Đặt tên controller `sealed-secrets-controller`, khớp với lệnh `kubeseal` trong secrets.md | App `sealed-secrets` |
| `platform/monitoring/*-values.yaml` | kube-prometheus-stack (Prometheus, Grafana, Alertmanager), Loki (log), Alloy (thu log/metrics) | App cùng tên |
| `platform/velero/values.yaml` | Sao lưu tài nguyên K8s/PV | App `velero` |
| `platform/registry-trust/` | DaemonSet ghi CA của Harbor vào `containerd certs.d` trên mọi node, để node pull được image từ `192.168.6.194` | **Không** thuộc ArgoCD. Apply bằng tay: `kubectl apply -k platform/registry-trust` |
| `cloud/aws/`, `cloud/network/` | Ghi chú phần nằm ngoài K8s (IAM/IVS trên AWS, mạng, VPN, security group) | Tài liệu, ArgoCD không đụng tới |
| `scripts/deploy.sh` | Luồng **thủ công** hiện tại: tạo Secret (DB, Redis, Harbor, AWS, S3…) từ `~/lc-secrets/<env>.env`, ghi tag, rồi `kubectl apply -k`. `SECRETS_ONLY=1` chỉ tạo Secret | Đang dùng. Khi có ArgoCD, chỉ còn dùng phần Secret |
| `scripts/install-edge.sh` | Cài HAProxy + Kong + edge bằng Helm, tạo secret TLS Cloudflare Origin | Đang dùng. Có thể thay bằng các app platform |
| `scripts/promote.sh` | Chép tag image từ overlay này sang overlay khác (ví dụ dev sang prod) bằng `yq` và `kustomize edit` | Workflow `promote` |
| `.github/workflows/validate.yml` | Trên mỗi PR: chạy `make validate` (runner self-hosted) | Chặn manifest hỏng trước khi ArgoCD thấy |
| `.github/workflows/promote.yml` | Chạy tay (`workflow_dispatch`): `promote.sh dev prod`, sau đó mở PR | Lên prod |
| `Makefile` | `make validate` = yamllint + `kustomize build` 4 overlay và `platform/edge` + kubeconform | Chạy local trước khi push |
| `.yamllint`, `.gitignore` | Quy tắc lint. Bỏ qua file secret thô, `.env`, khóa | |
| `.github/CODEOWNERS` | Ai phải duyệt PR (prod, argocd). Vẫn còn placeholder `<ORG>/<team>` | GitHub |
| `docs/` | Tài liệu (file này, migrate, secrets, object storage, load testing…) | |

### A.3 Application và thứ tự sync (sync wave)

ArgoCD tạo app theo annotation `argocd.argoproj.io/sync-wave`. Wave nhỏ chạy trước, và wave sau chỉ bắt đầu khi wave trước đã Healthy.

| Wave | Application | Namespace | Nguồn | Vì sao ở wave này |
|---|---|---|---|---|
| 0 | `sealed-secrets` | sealed-secrets | chart bitnami 2.20.0 | Phải có controller trước thì SealedSecret của các app sau mới giải mã được |
| 1 | `haproxy-ingress` | haproxy-ingress | chart haproxytech 1.54.2 + values trong repo | Ingress controller, cần secret TLS `cloudflare-origin-tls` |
| 1 | `kong` | kong | chart kong 3.4.1 + values | API gateway |
| 1 | `kube-prometheus-stack`, `loki` | monitoring | chart, cần ghim `<PIN_VERSION>` | Tạo CRD ServiceMonitor… cho các app sau |
| 1 | `velero` | velero | chart, cần ghim `<PIN_VERSION>` | Sao lưu |
| 2 | `alloy` | monitoring | chart, cần ghim `<PIN_VERSION>` | Cần Loki/Prometheus có trước |
| 3 | `edge` | kong / haproxy-ingress | `platform/edge` (kustomize) | Cần cả HAProxy và Kong |
| 4 | `live-commerce-dev`, `moderation-dev` | live-commerce-dev, moderation-dev | overlay dev | Tự sync (prune + selfHeal) |
| 4 | `live-commerce-prod`, `moderation-prod` | live-commerce, moderation | overlay prod | Auto-sync khi merge PR promote (selfHeal, không prune) |

Các app Helm dùng **multi-source**: nguồn 1 là chart, nguồn 2 là repo này với `ref: values`, để chart đọc `$values/platform/.../values.yaml`. Cần ArgoCD ≥ 2.6.

### A.4 Ba khái niệm cần nhớ

- **Sync**: ArgoCD render manifest từ Git rồi apply vào cluster.
- **prune**: resource có trong cluster (do ArgoCD tạo) mà đã bị xóa khỏi Git thì ArgoCD xóa luôn. Resource ArgoCD chưa từng quản lý, ví dụ Secret do `deploy.sh` tạo, thì **không** bị xóa.
- **selfHeal**: ai sửa tay trên cluster (`kubectl edit/patch/scale/set image`) thì trong vài phút ArgoCD đưa về như Git. Ở dev, **mọi thay đổi phải qua commit**.

### A.5 Đối chiếu với cluster HCM-01 (kiểm tra ngày 2026-10-10)

| Thành phần | Trong repo | Trên cluster | Đánh giá |
|---|---|---|---|
| ArgoCD | `argocd/install/values.yaml` | Helm release `argocd`, chart `argo-cd-10.9.6` (ArgoCD v3.5.3), cài ngày 07/10. **Không** dùng values của repo: `server.insecure` chưa bật, reconcile 120 s, `argocd-server` là NodePort 31392/30538. `argocd-initial-admin-secret` vẫn còn | Đã cài. Bỏ qua bước cài ở B.2. Muốn áp values thì `helm upgrade` (B.2) |
| Application / AppProject | `root`, 12 app, project `platform` và `live-commerce` | Chưa có Application nào. Chỉ có project `default`. Chưa khai báo repo nào | Chưa bootstrap |
| Repo infra trên GitHub | | Truy cập ẩn danh trả 404, tức repo **private** | Bắt buộc làm B.3 |
| `sealed-secrets` | chart 2.20.0, `fullnameOverride: sealed-secrets-controller` | Release `sealed-secrets` 2.20.0, deployment `sealed-secrets-controller`. Chưa có SealedSecret nào | Khớp, ArgoCD tiếp quản được |
| `kong` | chart 3.4.1, release `kong` | Release `kong` 3.4.1 | Khớp |
| `haproxy-ingress` | chart 1.54.2. App truyền `defaultTLSSecret.secret=cloudflare-origin-tls` | Release 1.54.2 nhưng `defaultTLSSecret.secret: null`, dùng chứng chỉ tự ký `…-default-cert`. **Không có** secret `cloudflare-origin-tls` | **Lệch.** Sync app này sẽ trỏ HAProxy tới secret không tồn tại. Tạo secret trước (chạy `install-edge.sh` với `TLS_CERT`/`TLS_KEY`), hoặc xóa khối `parameters` trong app |
| `edge` | Ingress `edge` + Service LB | Ingress `edge` (class haproxy), Service `haproxy-ingress-loadbalancer` có EXTERNAL-IP 192.168.6.8 | Khớp |
| `loki` | `loki-values.yaml`: lưu `filesystem`, chưa ghim version | Release `loki` chart 7.3.0, lưu log vào **S3** bucket `cmc-live-loki-logs` | **Lệch nặng.** Bật app này sẽ ghi đè cấu hình S3. Tạm tắt, hoặc chép values thật vào repo (`helm -n monitoring get values loki`) và ghim `7.3.0` |
| `kube-prometheus-stack`, `alloy` | Có app | Không cài. Thay vào đó có `grafana` (chart 10.5.15) và `promtail` (Helm, ns `monitoring`), cùng `prometheus-server`, `kube-state-metrics`, `node-exporter` (ns `cmc-monitoring`) | Repo khác hẳn cluster. Tạm tắt hai app này. grafana và promtail chưa có trong repo |
| `velero` | Có app | Không cài | Tạm tắt cho đến khi có bucket và credential |
| `registry-trust` | `platform/registry-trust` (apply tay) | DaemonSet `harbor-ca-installer` 14/14 | Khớp, ngoài ArgoCD |
| `live-commerce-dev` | overlay dev | 6 deployment chạy đủ. Tag `e9111e15` (auth, live-manager, web) và `257fdce4` (còn lại). Có secret `object-storage` và recording ARN | **Khớp với bản local chưa commit**, nhưng **lệch với `origin/main`**. Xem cảnh báo dưới |
| `moderation-dev` | overlay dev | `moderation-service:257fdce4` | Khớp |
| prod | overlay prod: namespace `live-commerce` và `moderation`, tag `0000000` | Không có namespace `live-commerce`. Namespace `production` có nhưng trống | Chưa dùng. Tạm tắt app prod |

> **Cảnh báo:** nếu bootstrap ngay bây giờ, ArgoCD sẽ đọc `origin/main` (commit `d3ca26c`) và sync `live-commerce-dev` về:
> - `IVS_MODE: mock`, `AWS_REGION: ap-southeast-1`, xóa `IVS_RECORDING_CONFIGURATION_ARN`;
> - image auth-service, live-manager, web lùi về `257fdce4`;
> - bỏ `envFrom` secret `object-storage` (mất chức năng upload ảnh).
>
> Phải commit và push các thay đổi local trước (B.1 bước 1).

---

## B. Sử dụng

### B.1 Chuẩn bị repo (làm một lần, qua commit)

1. **Commit các thay đổi đang nằm local** trong repo infra: S3, recording ARN, docs… ArgoCD chỉ thấy những gì đã có trên `main` ở GitHub. Nếu không commit, lần sync đầu sẽ **xóa** cấu hình mới (ví dụ `IVS_RECORDING_CONFIGURATION_ARN` đang được patch tay trên cluster).
2. Thay `<ORG>` bằng `clgp-aint-cool`:
   ```bash
   grep -rl "<ORG>" argocd .github | xargs sed -i '' 's#<ORG>#clgp-aint-cool#g'
   ```
3. Ghim phiên bản chart thay cho `<PIN_VERSION>` trong `alloy`, `kube-prometheus-stack`, `loki`, `velero`. Lấy phiên bản bằng `helm search repo <chart> --versions | head`. Nếu chưa dùng các app này, xem B.5 để tắt.
4. Cập nhật tag image trong `apps/*/overlays/dev/kustomization.yaml` cho đúng tag đang chạy trên cluster.
5. `make validate`, rồi commit và push lên `main`.

### B.2 Cài ArgoCD

Máy làm việc cần bật VPN tới cluster.

HCM-01 **đã có** ArgoCD (chart `argo-cd` 10.9.6), xem A.5. Để áp `argocd/install/values.yaml` mà giữ nguyên phiên bản đang chạy:

```bash
helm repo add argo https://argoproj.github.io/argo-helm && helm repo update
```

```bash
helm upgrade argocd argo/argo-cd -n argocd -f argocd/install/values.yaml --version 10.9.6
```

Cluster mới thì dùng `helm upgrade --install … --create-namespace` với cùng phiên bản. Ghi phiên bản này vào `argocd/install/README.md` thay cho `<PIN_VERSION>`.

```bash
kubectl -n argocd rollout status deploy/argocd-server
```

Mở UI bằng port-forward (không cần lộ ArgoCD ra Internet):

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:80
```

Vào `http://localhost:8080`, user `admin`. Mật khẩu ban đầu lấy bằng lệnh dưới (tự chạy, đừng dán vào chat hay commit):

```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
```

Sau khi đăng nhập, đổi mật khẩu (*User Info > Update password*), rồi xóa secret `argocd-initial-admin-secret`.

CLI (tùy chọn): `brew install argocd`, rồi `argocd login localhost:8080 --plaintext --username admin`.

### B.3 Cho ArgoCD đọc repo

- Repo **public**: không cần làm gì.
- Repo **private**: tạo một fine-grained token chỉ có quyền *Contents: Read-only* trên `live-commerce-infra`, rồi khai báo trong UI *Settings > Repositories > Connect repo (HTTPS)*, hoặc dùng CLI:
  ```bash
  argocd repo add https://github.com/clgp-aint-cool/live-commerce-infra.git --username git --password <token>
  ```
  Token chỉ lưu trong ArgoCD (Secret trong namespace argocd), không ghi vào repo.

### B.4 Bootstrap

```bash
kubectl apply -f argocd/projects/
kubectl apply -f argocd/bootstrap/root-app.yaml
```

Sau đó UI hiện app `root` cùng các app con. Đây là **lần cuối** bạn `kubectl apply` thứ gì liên quan tới ArgoCD. Từ đây, muốn thêm hoặc sửa app thì sửa file trong `argocd/apps/` rồi push.

Nên bootstrap từng bước, xem B.5.

### B.5 Chuyển cluster đang chạy (deploy.sh, install-edge.sh) sang ArgoCD

Cluster HCM-01 đang có Kong và HAProxy cài bằng Helm, workload cài bằng `kubectl apply -k`, và Secret tạo bằng `deploy.sh`. Thứ tự an toàn:

1. **Secret giữ nguyên.** ArgoCD không quản lý chúng nên prune không xóa. Khi cần đổi Secret, vẫn chạy:
   ```bash
   ENV=dev SECRETS_ONLY=1 bash scripts/deploy.sh
   ```
   Không chạy `deploy.sh` đầy đủ nữa, vì bước `kubectl apply` của nó giành quyền với ArgoCD.
2. **Bắt đầu với workload dev.** Trước khi apply root, tạm chuyển các app sau ra khỏi `argocd/apps/` (ví dụ sang `argocd/disabled/`, thư mục root không đọc):
   - `kube-prometheus-stack`, `alloy`: cluster đang dùng grafana, promtail và prometheus của CMC (`cmc-monitoring`). Cài thêm sẽ trùng node-exporter.
   - `loki`: values trong repo dùng `filesystem`, trong khi release thật đang lưu log vào S3. Sync sẽ ghi đè.
   - `velero`: chưa cài.
   - `*-prod.yaml`: chưa có namespace `live-commerce` và DB prod.
3. **Kong và HAProxy.** `releaseName` trong app (`kong`, `haproxy-ingress`) và namespace đã khớp với `install-edge.sh`, nên ArgoCD sẽ nhận quản lý release đang chạy mà không cài lại. Riêng app `haproxy-ingress` truyền `defaultTLSSecret.secret=cloudflare-origin-tls`, mà secret này **chưa có** trên HCM-01 (A.5). Trước khi bật app này, tạo secret bằng `install-edge.sh` (với `TLS_CERT`/`TLS_KEY`), hoặc xóa khối `parameters`. Xem tab *Diff* trên UI trước khi bấm Sync lần đầu. Nếu thấy diff lớn (ví dụ Service đổi kiểu), tắt auto-sync của app đó, sửa values cho khớp rồi mới sync.
4. **Theo dõi lần sync đầu.** App dev sẽ báo `OutOfSync` nếu tag trong Git khác tag trên cluster, sau đó tự sync. Kiểm tra pod:
   ```bash
   kubectl -n live-commerce-dev get pods
   ```
5. Từ đây **không** dùng `kubectl set image`, `kubectl patch configmap` hay `kubectl scale` ở dev nữa, vì selfHeal sẽ đảo lại trong khoảng 3 phút.

`scripts/install-edge.sh` vẫn giữ được, dùng khi dựng lại cluster không có ArgoCD. Nhưng khi app `kong`/`haproxy-ingress` đã chạy thì đừng chạy script này song song.

### B.6 Làm việc hằng ngày

**Deploy code mới (dev)**
1. Push code lên repo app. Workflow `build-and-deploy` build image, đẩy lên Harbor, rồi commit `newTag: <sha>` vào `apps/<ns>/overlays/dev/kustomization.yaml` của repo này.
2. Trong tối đa 3 phút (hoặc bấm **Refresh**), ArgoCD thấy commit mới và sync. Pod mới chạy theo rolling update.

Nếu muốn ghi tag bằng tay thay CI:

```bash
cd apps/live-commerce/overlays/dev
kustomize edit set image 192.168.6.194/live-commerce/web=192.168.6.194/live-commerce/web:<tag>
cd - && make validate && git commit -am "dev: web <tag>" && git push
```

**Đổi cấu hình** (ConfigMap, replica, resource, route Kong): sửa file trong `apps/.../base` hoặc overlay, mở PR (CI `validate` chạy), rồi merge. Lưu ý: Deployment không tự restart khi chỉ ConfigMap thay đổi. Sau khi sync, bấm **Restart** trên resource Deployment trong UI, hoặc chạy `kubectl rollout restart` (restart không làm OutOfSync).

**Lên prod**
1. GitHub, tab *Actions*, chạy workflow **promote** (from `dev`, to `prod`, `components` = `all` hoặc danh sách như `order-service,web`). Workflow mở PR chỉ chép tag của các service đó sang `overlays/prod`.
2. Review (theo CODEOWNERS) rồi merge. **Merge = deploy**: app prod bật auto-sync (`selfHeal: true`, `prune: false`), duyệt PR là cổng duy nhất.
3. Theo dõi `live-commerce-prod` trên UI và Discord #alert-critical. Vì `prune: false`, resource bị xóa khỏi Git vẫn còn trên cluster: xóa tay hoặc *Sync* với *Prune* trên UI.

**Rollback**
- Cách chuẩn: `git revert <commit>` rồi push, ArgoCD sync về bản cũ.
- Khẩn cấp: UI, *History and Rollback*, chọn revision cũ. Lúc này app dev sẽ phải **tắt auto-sync**, nếu không ArgoCD lại kéo về `main`. Sau đó vẫn phải revert trong Git rồi bật lại auto-sync.

**Secret**: xem [secrets.md](secrets.md). Khi chuyển hẳn sang Sealed Secrets: tạo `*.sealed.yaml` trong `overlays/<env>/secrets/`, bỏ comment dòng `- secrets/` trong kustomization, rồi push. Sao lưu khóa của controller.

### B.7 Đọc trạng thái trên UI

| Trạng thái | Nghĩa | Làm gì |
|---|---|---|
| Synced + Healthy | Cluster khớp Git, pod chạy tốt | Không cần làm gì |
| OutOfSync | Git khác cluster: có commit mới, hoặc ai đó sửa tay | Xem *Diff*. Dev sẽ tự sync |
| Progressing | Đang rolling update hoặc chờ pod Ready | Đợi. Nếu kéo dài, xem *Events* của pod |
| Degraded | Pod lỗi (CrashLoopBackOff, ImagePullBackOff…) | Bấm vào pod, xem tab *Logs*/*Events* |
| Missing | Resource có trong Git nhưng chưa có trên cluster | Thường là đang chờ wave trước |
| Unknown / ComparisonError | ArgoCD không render được: YAML sai, không đọc được repo, chart không tồn tại | Xem *App conditions* |

### B.8 Sự cố thường gặp

| Triệu chứng | Nguyên nhân | Cách xử lý |
|---|---|---|
| `repository not found` / `authentication required` | Repo private chưa khai báo credential, hoặc còn `<ORG>` | B.1 bước 2 và B.3 |
| `chart ... version <PIN_VERSION> not found` | Chưa ghim phiên bản chart | B.1 bước 3, hoặc chuyển app đó ra khỏi `argocd/apps` |
| `namespace ... is not permitted in project live-commerce` | Overlay deploy vào namespace ngoài `live-commerce*`/`moderation*` | Sửa `destinations` trong `argocd/projects/live-commerce.yaml` |
| Pod `ImagePullBackOff` | Tag chưa có trên Harbor, thiếu Secret `harbor-registry` trong namespace, hoặc node chưa tin CA Harbor | Kiểm tra tag trên Harbor. Chạy `SECRETS_ONLY=1 deploy.sh`. `kubectl apply -k platform/registry-trust` |
| Pod `CreateContainerConfigError` | Thiếu Secret được tham chiếu (DB, Redis, AWS) | `ENV=dev SECRETS_ONLY=1 bash scripts/deploy.sh` |
| Sửa tay xong vài phút sau bị mất | selfHeal | Commit thay đổi vào Git |
| App kẹt ở wave 1, các wave sau không chạy | Một app platform không Healthy (ví dụ chart lỗi) | Sửa app đó, hoặc tạm chuyển nó ra khỏi `argocd/apps` |
| Xóa app trên UI làm mất cả workload | `root` có finalizer `resources-finalizer`: xóa app cha là xóa mọi resource con | Muốn gỡ ArgoCD mà giữ workload: xóa finalizer trước, hoặc dùng `argocd app delete --cascade=false` |
| OutOfSync mãi ở HPA hoặc trường `replicas` | HPA đổi số replica nên luôn lệch với Git | Thêm `ignoreDifferences` cho `/spec/replicas` của Deployment có HPA |

### B.9 Tóm tắt lệnh

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:80      # mở UI
```

```bash
argocd app list                                              # trạng thái tất cả app
```

```bash
argocd app diff live-commerce-dev                            # Git khác cluster chỗ nào
```

```bash
argocd app sync live-commerce-prod                           # sync tay (prod)
```

```bash
argocd app history live-commerce-dev                         # các lần sync
```

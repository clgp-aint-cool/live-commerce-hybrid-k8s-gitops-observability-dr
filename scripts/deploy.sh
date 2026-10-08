#!/usr/bin/env bash
# Triển khai thủ công lên K8s CMC Cloud bằng kubectl apply -k (không ArgoCD, không Sealed Secrets).
# PostgreSQL và Redis dùng DBaaS của CMC Cloud, KHÔNG chạy trong cluster.
# Secret thường được tạo thẳng vào cluster từ file env ngoài repo; KHÔNG commit file env.
#
#   ENV=dev TAG=<sha8> NODE_IP=<ip worker> bash scripts/deploy.sh
#
# Biến:
#   ENV           dev (mặc định) | prod
#   TAG           (bắt buộc) tag image chung cho 7 service trên Harbor 192.168.6.194
#   SECRETS_FILE  file env (mặc định ~/lc-secrets/$ENV.env). Chưa có thì script sinh mẫu rồi dừng để bạn điền.
#   NODE_IP       (tùy chọn) IP một worker node để smoke test (HAProxy 30443 nếu đã chạy install-edge.sh, không thì Kong 32080)
#   INSTALL_KONG=1  cài nhanh Kong NodePort 32080 (thử nghiệm). Luồng chuẩn LB -> HAProxy -> Kong: scripts/install-edge.sh
#   DRY_RUN=1     chỉ kiểm tra với API server (server-side dry-run). Vẫn tạo namespace nếu chưa có.
#   RESTART=1     rollout restart sau khi apply (cần khi chỉ đổi Secret/ConfigMap)
#   SKIP_NETCHECK=1 bỏ bước thử kết nối từ pod tới DBaaS
#   SECRETS_ONLY=1  chỉ tạo/cập nhật Secret từ file env rồi dừng (không cần TAG, không đụng workload)
# Chạy ở gốc repo live-commerce-infra. Tương thích bash 3.2 (macOS).
set -euo pipefail

ENV=${ENV:-dev}
REG=192.168.6.194
case $ENV in
  dev)  APP_NS=live-commerce-dev MOD_NS=moderation-dev HOST=cmc-cloud-hcm1.cloud ;;
  prod) APP_NS=live-commerce     MOD_NS=moderation     HOST=live.example.com ;;
  *) echo "ENV phải là dev hoặc prod" >&2; exit 1 ;;
esac
SECRETS_FILE=${SECRETS_FILE:-$HOME/lc-secrets/$ENV.env}
APP_DIR=apps/live-commerce/overlays/$ENV MOD_DIR=apps/moderation/overlays/$ENV
APP_DEPLOYS="product-service auth-service order-service chat-service live-manager web"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }
# Mã hóa một chuỗi để nhúng vào URL (mật khẩu DBaaS có thể chứa @ : / ...)
urlenc() { printf '%s' "$1" | perl -pe 's/([^A-Za-z0-9._~-])/sprintf("%%%02X", ord($1))/ge'; }

[ -f apps/live-commerce/base/kustomization.yaml ] || die "Chạy script ở gốc repo live-commerce-infra"
command -v kubectl >/dev/null || die "Thiếu kubectl"
[ "${SECRETS_ONLY:-0}" = 1 ] || : "${TAG:?Đặt TAG=<tag image trên Harbor>, ví dụ TAG=a1b2c3d4}"

APPLY="kubectl apply"
[ "${DRY_RUN:-0}" = 1 ] && { APPLY="kubectl apply --dry-run=server"; warn "DRY_RUN=1: không thay đổi workload"; }

# ---------- 0. Preflight ----------
say "Môi trường: $ENV ($APP_NS, $MOD_NS)"
say "Cluster: $(kubectl config current-context)"
[ "$ENV" = prod ] && [ "${DRY_RUN:-0}" != 1 ] && { printf 'Gõ "prod" để triển khai lên PROD: '; read -r ans; [ "$ans" = prod ] || die "Đã hủy"; }
kubectl get nodes -o wide >/dev/null || die "Không kết nối được cluster (kiểm tra KUBECONFIG)"
kubectl get nodes -o jsonpath='{range .items[*]}  {.metadata.name}  {.status.addresses[?(@.type=="InternalIP")].address}  {.status.nodeInfo.architecture}{"\n"}{end}'
kubectl get nodes -o jsonpath='{.items[*].status.nodeInfo.architecture}' | tr ' ' '\n' | grep -qv amd64 \
  && warn "Có node không phải amd64: image phải build đúng kiến trúc"

if [ "${SECRETS_ONLY:-0}" != 1 ] && ! kubectl get ingressclass kong >/dev/null 2>&1; then
  if [ "${INSTALL_KONG:-0}" = 1 ]; then
    command -v helm >/dev/null || die "Thiếu helm"
    say "Cài Kong (DB-less, NodePort 32080/32443)"
    helm repo add kong https://charts.konghq.com >/dev/null 2>&1 || true
    helm repo update kong >/dev/null
    helm upgrade --install kong kong/kong ${KONG_CHART_VERSION:+--version "$KONG_CHART_VERSION"} -n kong --create-namespace \
      -f platform/kong/values.yaml --set proxy.type=NodePort --set proxy.http.nodePort=32080 --set proxy.tls.nodePort=32443 --wait
  else
    die "Chưa có IngressClass kong. Chạy scripts/install-edge.sh (LB -> HAProxy -> Kong), hoặc INSTALL_KONG=1 để thử nhanh qua NodePort 32080"
  fi
fi

# ---------- 1. File secret ----------
if [ ! -f "$SECRETS_FILE" ]; then
  mkdir -p "$(dirname "$SECRETS_FILE")"; chmod 700 "$(dirname "$SECRETS_FILE")"
  cat > "$SECRETS_FILE" <<ENV
# ---- PostgreSQL DBaaS (CMC Cloud): lấy ở trang chi tiết instance. Dùng private endpoint trong VPC.
PG_HOST=
PG_PORT=5432
PG_USER=
PG_PASSWORD=
PG_DB=livecommerce
# Để trống: không TLS. no-verify: TLS, không kiểm tra chứng chỉ. verify-full: TLS + kiểm tra (chỉ khi CA công khai).
PG_SSLMODE=
# ---- Redis DBaaS (CMC Cloud): endpoint standalone/primary, KHÔNG dùng endpoint cluster mode.
REDIS_HOST=
REDIS_PORT=6379
# Chỉ điền khi Redis bật ACL user (mặc định chỉ có mật khẩu).
REDIS_USER=
REDIS_PASSWORD=
# 1: dùng rediss:// (TLS). Chứng chỉ phải được CA công khai ký, ioredis không tắt kiểm tra qua URL được.
REDIS_TLS=0
# ---- Token nội bộ của ứng dụng (đã sinh ngẫu nhiên)
JWT_SECRET=$(openssl rand -hex 32)
INTERNAL_API_TOKEN=$(openssl rand -hex 32)
ADMIN_TOKEN=$(openssl rand -hex 32)
WEBHOOK_SHARED_SECRET=$(openssl rand -hex 32)
# ---- Harbor robot account (pull-only). Giữ nháy đơn vì có ký tự \$.
HARBOR_ROBOT_USER='robot\$live-commerce+k8s-pull'
HARBOR_ROBOT_TOKEN=
# ---- Chỉ khi IVS_MODE=aws
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
ENV
  chmod 600 "$SECRETS_FILE"
  die "Đã sinh $SECRETS_FILE. Điền thông tin DBaaS (PG_*, REDIS_*) và HARBOR_ROBOT_TOKEN rồi chạy lại"
fi
set -a; . "$SECRETS_FILE"; set +a
for v in PG_HOST PG_PORT PG_USER PG_PASSWORD PG_DB REDIS_HOST REDIS_PORT REDIS_PASSWORD \
         JWT_SECRET INTERNAL_API_TOKEN ADMIN_TOKEN WEBHOOK_SHARED_SECRET HARBOR_ROBOT_USER HARBOR_ROBOT_TOKEN; do
  [ -n "${!v:-}" ] || die "Thiếu $v trong $SECRETS_FILE"
done
[ ${#JWT_SECRET} -ge 32 ] || die "JWT_SECRET phải dài ít nhất 32 ký tự"

DB_URL="postgres://$(urlenc "$PG_USER"):$(urlenc "$PG_PASSWORD")@${PG_HOST}:${PG_PORT}/$(urlenc "$PG_DB")"
[ -n "${PG_SSLMODE:-}" ] && DB_URL="${DB_URL}?sslmode=${PG_SSLMODE}"
REDIS_SCHEME=redis; [ "${REDIS_TLS:-0}" = 1 ] && REDIS_SCHEME=rediss
REDIS_URL="${REDIS_SCHEME}://$(urlenc "${REDIS_USER:-}"):$(urlenc "$REDIS_PASSWORD")@${REDIS_HOST}:${REDIS_PORT}"

# ---------- 2. Namespace + Secret ----------
say "Namespace"
for ns in $APP_NS $MOD_NS; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done

# sec <namespace> <tham số cho "kubectl create secret">; idempotent, không in giá trị
sec() { local ns=$1; shift; kubectl create secret "$@" -n "$ns" --dry-run=client -o yaml | $APPLY -f - >/dev/null; echo "  ok  $ns/$2${DRY_RUN:+$( [ "$DRY_RUN" = 1 ] && echo " (dry-run, chưa tạo)")}"; }

say "Secret"
for ns in $APP_NS $MOD_NS; do
  sec "$ns" docker-registry harbor-registry --docker-server=$REG \
    --docker-username="$HARBOR_ROBOT_USER" --docker-password="$HARBOR_ROBOT_TOKEN"
done
sec $APP_NS generic product-service-secret --from-literal=DATABASE_URL="$DB_URL" --from-literal=INTERNAL_API_TOKEN="$INTERNAL_API_TOKEN"
sec $APP_NS generic auth-service-secret --from-literal=DATABASE_URL="$DB_URL" --from-literal=JWT_SECRET="$JWT_SECRET"
sec $APP_NS generic order-service-secret --from-literal=DATABASE_URL="$DB_URL" --from-literal=REDIS_URL="$REDIS_URL" \
  --from-literal=JWT_SECRET="$JWT_SECRET"
sec $APP_NS generic chat-service-secret --from-literal=REDIS_URL="$REDIS_URL" --from-literal=INTERNAL_API_TOKEN="$INTERNAL_API_TOKEN"
aws_args=()
[ -n "${AWS_ACCESS_KEY_ID:-}" ] && aws_args=(--from-literal=AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" --from-literal=AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY")
sec $APP_NS generic live-manager-secret --from-literal=DATABASE_URL="$DB_URL" --from-literal=REDIS_URL="$REDIS_URL" \
  --from-literal=ADMIN_TOKEN="$ADMIN_TOKEN" --from-literal=INTERNAL_API_TOKEN="$INTERNAL_API_TOKEN" ${aws_args[@]+"${aws_args[@]}"}
sec $MOD_NS generic moderation-service-secret --from-literal=DATABASE_URL="$DB_URL" \
  --from-literal=WEBHOOK_SHARED_SECRET="$WEBHOOK_SHARED_SECRET" --from-literal=INTERNAL_API_TOKEN="$INTERNAL_API_TOKEN"

if [ "${SECRETS_ONLY:-0}" = 1 ]; then
  say "SECRETS_ONLY xong. Pod đang chạy chỉ nhận Secret mới sau: kubectl -n $APP_NS rollout restart deploy ; kubectl -n $MOD_NS rollout restart deploy"
  exit 0
fi

# ---------- 3. Ghi tag vào overlay ----------
say "Tag image = $TAG"
perl -pi -e "s/newTag: \"[^\"]*\"/newTag: \"$TAG\"/" "$APP_DIR/kustomization.yaml" "$MOD_DIR/kustomization.yaml"

if [ "${DRY_RUN:-0}" = 1 ]; then
  for k in $APP_DIR $MOD_DIR; do say "dry-run $k"; kubectl kustomize "$k" | $APPLY -f - ; done
  say "DRY_RUN xong"; exit 0
fi

# ---------- 4. Thử kết nối từ trong cluster tới DBaaS ----------
# Dùng chính image product-service (có node) để khỏi kéo image từ Docker Hub. Chỉ mở TCP, không gửi mật khẩu.
if [ "${SKIP_NETCHECK:-0}" != 1 ]; then
  say "Kiểm tra pod -> PostgreSQL $PG_HOST:$PG_PORT, Redis $REDIS_HOST:$REDIS_PORT"
  JS='const net=require("net");const a=process.argv.slice(1);let bad=0,left=a.length/2;
for(let i=0;i<a.length;i+=2){const h=a[i],p=+a[i+1];const s=net.connect(p,h);s.setTimeout(5000);
const done=(ok,why)=>{console.log((ok?"  ok   ":"  FAIL ")+h+":"+p+(why?" ("+why+")":""));if(!ok)bad=1;s.destroy();if(--left===0)process.exit(bad);};
s.once("connect",()=>done(true));s.once("timeout",()=>done(false,"timeout"));s.once("error",e=>done(false,e.code));}'
  kubectl -n $APP_NS run "netcheck-$$" --rm -i --quiet --restart=Never --pod-running-timeout=2m \
    --image=$REG/live-commerce/product-service:$TAG \
    --overrides='{"apiVersion":"v1","spec":{"imagePullSecrets":[{"name":"harbor-registry"}]}}' \
    --command -- node -e "$JS" "$PG_HOST" "$PG_PORT" "$REDIS_HOST" "$REDIS_PORT" \
    || die "Pod không tới được DBaaS: kiểm tra security group/ACL của DBaaS cho dải IP node/pod, hoặc dùng private endpoint"
fi

# ---------- 5. Apply: app -> moderation ----------
say "Ứng dụng ($APP_NS)"
kubectl apply -k $APP_DIR
[ "${RESTART:-0}" = 1 ] && for d in $APP_DEPLOYS; do kubectl -n $APP_NS rollout restart deploy/$d; done
fail=0
for d in $APP_DEPLOYS; do kubectl -n $APP_NS rollout status deploy/$d --timeout=5m || fail=1; done

say "Moderation ($MOD_NS)"
kubectl apply -k $MOD_DIR
[ "${RESTART:-0}" = 1 ] && kubectl -n $MOD_NS rollout restart deploy/moderation-service
kubectl -n $MOD_NS rollout status deploy/moderation-service --timeout=5m || fail=1

kubectl -n $APP_NS get pods -o wide
kubectl -n $MOD_NS get pods -o wide
if [ $fail = 1 ]; then
  warn "Có deployment chưa Ready. Xem: kubectl -n $APP_NS get events --sort-by=.lastTimestamp | tail -20"
  warn "và bảng xử lý sự cố ở docs/migrate-cmc-k8s.md mục 12"
  exit 1
fi

# ---------- 6. Smoke test ----------
# Có HAProxy (scripts/install-edge.sh) thì đi qua https://<NODE_IP>:30443, không thì qua Kong NodePort 32080.
if kubectl -n haproxy-ingress get svc haproxy-ingress-kubernetes-ingress >/dev/null 2>&1; then
  BASE="https://$HOST:30443" RESOLVE="$HOST:30443"
else
  BASE="http://$HOST:32080" RESOLVE="$HOST:32080"
fi
if [ -n "${NODE_IP:-}" ]; then
  say "Smoke test $BASE -> $NODE_IP"
  check() { local want=$1 path=$2 got; got=$(curl -sk -o /dev/null -w '%{http_code}' --resolve "$RESOLVE:$NODE_IP" "$BASE$path" || echo 000)
            [ "$got" = "$want" ] && echo "  ok   $path -> $got" || { echo "  FAIL $path -> $got (muốn $want)"; fail=1; }; }
  check 200 /
  check 200 /api/products
  check 200 /api/lives
  # /internal không có route riêng: rơi vào "/" (web SPA, 200 HTML) hoặc Kong 404. Lỗi khi backend trả JSON khác 404.
  r=$(curl -sk -o /dev/null -w '%{http_code} %{content_type}' --resolve "$RESOLVE:$NODE_IP" "$BASE/internal/channels/open-session" || echo 000)
  case "$r" in 404*|*html*) echo "  ok   /internal không tới backend ($r)" ;; *) echo "  FAIL /internal lộ ra ngoài ($r)"; fail=1 ;; esac
  [ $fail = 0 ] || exit 1
fi
say "Xong. Mở $BASE (thêm '<NODE_IP> $HOST' vào /etc/hosts)"

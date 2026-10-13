#!/usr/bin/env bash
# Dựng luồng vào: Cloudflare -> CMC ELB (tự tạo từ Service type LoadBalancer) -> HAProxy Ingress (kết thúc TLS)
#                 -> Kong (ClusterIP, định tuyến host/path) -> service của live-commerce / live-commerce-dev.
# Chạy bằng Helm + kubectl, không cần ArgoCD. Idempotent: chạy lại để cập nhật chart/values/chứng chỉ.
#
#   TLS_CERT=~/lc-secrets/origin.pem TLS_KEY=~/lc-secrets/origin.key NODE_IP=<ip worker> HOST=cmc-cloud-hcm1.cloud \
#     bash scripts/install-edge.sh
#
# Biến:
#   TLS_CERT, TLS_KEY  (khuyến nghị) chứng chỉ Cloudflare Origin (PEM). Bỏ trống: HAProxy dùng chứng chỉ tự ký (chỉ để thử).
#   NODE_IP            (tùy chọn) IP một worker node để smoke test qua NodePort 30443
#   HOST               (tùy chọn) host dùng khi smoke test, mặc định cmc-cloud-hcm1.cloud
#   DRY_RUN=1          chỉ render/kiểm tra với API server, không thay đổi cluster
#   LB_WAIT            số giây chờ CMC cấp EXTERNAL-IP cho ELB (mặc định 300)
#   HAPROXY_CHART_VERSION, KONG_CHART_VERSION  ghim phiên bản chart (mặc định 1.54.2 / 3.4.1)
# Chạy ở gốc repo live-commerce-infra. Tương thích bash 3.2 (macOS).
set -euo pipefail

HAPROXY_CHART_VERSION=${HAPROXY_CHART_VERSION:-1.54.2}
KONG_CHART_VERSION=${KONG_CHART_VERSION:-3.4.1}
HAP_NS=haproxy-ingress KONG_NS=kong
HAP_SVC=haproxy-ingress-kubernetes-ingress
TLS_SECRET=cloudflare-origin-tls
HOST=${HOST:-cmc-cloud-hcm1.cloud}

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ -f platform/edge/ingress.yaml ] || die "Chạy script ở gốc repo live-commerce-infra"
command -v kubectl >/dev/null || die "Thiếu kubectl"
command -v helm >/dev/null || die "Thiếu helm"

HELM_DRY=() APPLY="kubectl apply"
if [ "${DRY_RUN:-0}" = 1 ]; then
  HELM_DRY=(--dry-run=server) APPLY="kubectl apply --dry-run=server"
  warn "DRY_RUN=1: không thay đổi cluster"
fi

say "Cluster: $(kubectl config current-context)"
kubectl get nodes >/dev/null || die "Không kết nối được cluster (kiểm tra KUBECONFIG)"

say "Helm repo"
helm repo add haproxytech https://haproxytech.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add kong https://charts.konghq.com >/dev/null 2>&1 || true
helm repo update haproxytech kong >/dev/null

for ns in $HAP_NS $KONG_NS; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | $APPLY -f - >/dev/null
done

# ---------- 1. Chứng chỉ origin ----------
hap_tls_args=()
if [ -n "${TLS_CERT:-}" ] || [ -n "${TLS_KEY:-}" ]; then
  [ -f "${TLS_CERT:-}" ] && [ -f "${TLS_KEY:-}" ] || die "Cần cả TLS_CERT và TLS_KEY trỏ tới file có thật"
  command -v openssl >/dev/null || die "Thiếu openssl"
  [ "$(openssl x509 -noout -pubkey -in "$TLS_CERT" | openssl sha256)" = "$(openssl pkey -pubout -in "$TLS_KEY" | openssl sha256)" ] \
    || die "TLS_CERT và TLS_KEY không khớp nhau"
  say "Chứng chỉ: $(openssl x509 -noout -subject -enddate -in "$TLS_CERT" | tr '\n' ' ')"
  kubectl create secret tls $TLS_SECRET -n $HAP_NS --cert="$TLS_CERT" --key="$TLS_KEY" --dry-run=client -o yaml \
    | $APPLY -f - >/dev/null
  echo "  ok  $HAP_NS/$TLS_SECRET"
  hap_tls_args=(--set controller.defaultTLSSecret.secret=$TLS_SECRET)
elif kubectl -n $HAP_NS get secret $TLS_SECRET >/dev/null 2>&1; then
  say "Dùng lại secret $HAP_NS/$TLS_SECRET đã có"
  hap_tls_args=(--set controller.defaultTLSSecret.secret=$TLS_SECRET)
else
  warn "Chưa có chứng chỉ origin: HAProxy dùng chứng chỉ tự ký. Cloudflare 'Full (strict)' sẽ từ chối, chỉ dùng để thử"
fi

# ---------- 2. Kong: ClusterIP sau HAProxy ----------
if kubectl -n $KONG_NS get svc kong-kong-proxy -o jsonpath='{.spec.type}' 2>/dev/null | grep -q NodePort; then
  warn "Kong đang là NodePort (32080/32443). Bước này chuyển về ClusterIP: truy cập qua 32080 sẽ ngừng, dùng 30443 của HAProxy"
fi
say "Kong $KONG_CHART_VERSION (ClusterIP)"
helm upgrade --install kong kong/kong --version "$KONG_CHART_VERSION" -n $KONG_NS \
  -f platform/kong/values.yaml --wait --timeout 5m ${HELM_DRY[@]+"${HELM_DRY[@]}"} >/dev/null

# ---------- 3. HAProxy Ingress: NodePort 30080/30443 ----------
say "HAProxy Ingress $HAPROXY_CHART_VERSION (NodePort 30080/30443)"
helm upgrade --install haproxy-ingress haproxytech/kubernetes-ingress --version "$HAPROXY_CHART_VERSION" -n $HAP_NS \
  -f platform/haproxy-ingress/values.yaml ${hap_tls_args[@]+"${hap_tls_args[@]}"} \
  --wait --timeout 15m ${HELM_DRY[@]+"${HELM_DRY[@]}"} >/dev/null

if [ "${DRY_RUN:-0}" = 1 ]; then
  kubectl kustomize platform/edge | $APPLY -f -
  say "DRY_RUN xong"; exit 0
fi

# Nếu secret được tạo/đổi sau khi HAProxy đã chạy thì nạp lại cho chắc
[ ${#hap_tls_args[@]} -gt 0 ] && kubectl -n $HAP_NS rollout restart daemonset/$HAP_SVC >/dev/null
kubectl -n $HAP_NS rollout status daemonset/$HAP_SVC --timeout=15m

# ---------- 4. Ingress edge + Service LoadBalancer ----------
say "Ingress edge (haproxy -> kong-kong-proxy) + Service LoadBalancer (CMC ELB -> HAProxy)"
kubectl apply -k platform/edge

kubectl get ingressclass
kubectl -n $HAP_NS get svc
kubectl -n $KONG_NS get svc kong-kong-proxy

say "Chờ CMC Cloud cấp ELB cho Service haproxy-ingress-loadbalancer (tối đa ${LB_WAIT:-300}s)"
LB_ADDR="" waited=0
while [ $waited -lt "${LB_WAIT:-300}" ]; do
  LB_ADDR=$(kubectl -n $HAP_NS get svc haproxy-ingress-loadbalancer \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
  [ -n "$LB_ADDR" ] && break
  sleep 10; waited=$((waited + 10))
done
if [ -n "$LB_ADDR" ]; then
  say "ELB: $LB_ADDR  (trỏ bản ghi DNS trên Cloudflare về địa chỉ này)"
else
  warn "Chưa có EXTERNAL-IP. Xem: kubectl -n $HAP_NS describe svc haproxy-ingress-loadbalancer (phần Events)"
  warn "Cluster chưa bật cloud controller cho LB thì tạo ELB bằng tay: listener 443 -> worker :30443"
fi

# ---------- 5. Smoke test ----------
if [ -n "${NODE_IP:-}" ]; then
  say "Smoke test https://$HOST:30443 -> $NODE_IP (bỏ qua kiểm tra chứng chỉ: origin cert chỉ Cloudflare tin)"
  sleep 3
  fail=0
  check() { local want=$1 url=$2 got
            got=$(curl -sk -o /dev/null -w '%{http_code}' --resolve "$HOST:30443:$NODE_IP" --resolve "$HOST:30080:$NODE_IP" "$url" || echo 000)
            echo "$got" | grep -Eq "^($want)\$" && echo "  ok   $url -> $got" || { echo "  FAIL $url -> $got (muốn $want)"; fail=1; }; }
  check 200 "https://$HOST:30443/"
  check 200 "https://$HOST:30443/api/products"
  # /internal: web SPA (200 HTML) hoặc Kong 404 là đúng; JSON khác 404 nghĩa là backend lộ ra ngoài
  r=$(curl -sk -o /dev/null -w '%{http_code} %{content_type}' --resolve "$HOST:30443:$NODE_IP" "https://$HOST:30443/internal/channels/open-session" || echo 000)
  case "$r" in 404*|*html*) echo "  ok   /internal không tới backend ($r)" ;; *) echo "  FAIL /internal lộ ra ngoài ($r)"; fail=1 ;; esac
  check "301|302|308" "http://$HOST:30080/"   # ssl-redirect
  if [ -n "$LB_ADDR" ]; then
    say "Smoke test qua ELB https://$HOST -> $LB_ADDR"
    got=$(curl -sk -o /dev/null -w '%{http_code}' --resolve "$HOST:443:$LB_ADDR" "https://$HOST/api/products" || echo 000)
    [ "$got" = 200 ] && echo "  ok   https://$HOST/api/products -> $got" || { echo "  FAIL qua ELB -> $got (muốn 200)"; fail=1; }
  fi
  [ $fail = 0 ] || { warn "Xem: kubectl -n $HAP_NS logs ds/$HAP_SVC --tail=50 ; kubectl -n $KONG_NS logs deploy/kong-kong -c proxy --tail=50"; exit 1; }
fi

say "Xong. Bước tiếp theo ngoài cluster (docs/migrate-cmc-k8s.md mục 10):"
echo "  - ELB do CMC tạo từ Service haproxy-ingress-loadbalancer: ${LB_ADDR:-<chưa có>}"
echo "  - Palo Alto/SG: chỉ cho ELB/Cloudflare vào 80/443; NodePort 30080/30443 chỉ mở trong VPN"
echo "  - Cloudflare: DNS proxied -> ${LB_ADDR:-IP ELB}, SSL Full (strict), bật WebSockets"

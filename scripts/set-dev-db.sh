#!/usr/bin/env bash
# Trỏ dev sang PostgreSQL và/hoặc Redis riêng; DB/Redis cũ giữ lại cho prod.
#   ./scripts/set-dev-db.sh
# Gõ URL khi được hỏi (không hiện trên màn hình, không lưu vào history):
#   postgres://<user>:<pass>@<host>:5432/<db>
# Phải chạy scripts/copy-secrets.sh TRƯỚC, để prod giữ URL DB cũ.
set -euo pipefail

APP_NS=live-commerce-dev MOD_NS=moderation-dev
DB_SECRETS="$APP_NS/auth-service-secret $APP_NS/order-service-secret $APP_NS/product-service-secret $APP_NS/live-manager-secret $MOD_NS/moderation-service-secret"
REDIS_SECRETS="$APP_NS/order-service-secret $APP_NS/live-manager-secret $APP_NS/chat-service-secret"

kubectl -n live-commerce get secret auth-service-secret >/dev/null 2>&1 \
  || { echo "Chưa có Secret ở ns live-commerce. Chạy ./scripts/copy-secrets.sh trước."; exit 1; }

read -rsp "DATABASE_URL mới cho dev (Enter = giữ nguyên): " DB_URL; echo
[ -z "$DB_URL" ] || [[ "$DB_URL" == postgres*://* ]] || { echo "URL phải bắt đầu bằng postgres:// hoặc postgresql://"; exit 1; }
read -rsp "REDIS_URL mới cho dev (Enter = giữ nguyên): " REDIS_URL; echo
[ -z "$REDIS_URL" ] || [[ "$REDIS_URL" == redis*://* ]] || { echo "URL phải bắt đầu bằng redis:// hoặc rediss://"; exit 1; }
[ -n "$DB_URL$REDIS_URL" ] || { echo "Không có gì để đổi."; exit 0; }

# Giá trị đi qua stdin, không xuất hiện trong danh sách tiến trình.
patch() { # <ns/secret> <key> <value>
  printf '%s' "$3" \
    | python3 -c 'import base64,json,sys; print(json.dumps({"data":{sys.argv[1]:base64.b64encode(sys.stdin.buffer.read()).decode()}}))' "$2" \
    | kubectl -n "${1%/*}" patch secret "${1#*/}" --type merge --patch-file /dev/stdin
}

if [ -n "$DB_URL" ]; then
  for s in $DB_SECRETS; do patch "$s" DATABASE_URL "$DB_URL"; done
fi
if [ -n "$REDIS_URL" ]; then
  for s in $REDIS_SECRETS; do patch "$s" REDIS_URL "$REDIS_URL"; done
fi

# Service tự CREATE TABLE IF NOT EXISTS khi khởi động → DB mới trống vẫn chạy được.
kubectl -n "$APP_NS" rollout restart deploy
kubectl -n "$MOD_NS" rollout restart deploy
kubectl -n "$APP_NS" rollout status deploy --timeout=180s
kubectl -n "$MOD_NS" rollout status deploy --timeout=180s
echo "Xong. Đổi Redis thì kho của phiên live demo trống: chạy lại seed. Nạp dữ liệu demo cho dev: xem đầu file live-commerce-app/scripts/seed-demo.cjs."

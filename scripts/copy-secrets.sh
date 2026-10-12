#!/usr/bin/env bash
# Sao chép Secret từ namespace dev sang prod (prod dùng chung DB/Redis/S3 với dev).
#   ./scripts/copy-secrets.sh            # live-commerce-dev → live-commerce, moderation-dev → moderation
# Chạy lại được (kubectl apply). Không in giá trị secret ra màn hình.
# Khi tách DB/Redis riêng cho prod: tạo lại Secret prod bằng scripts/deploy.sh (ENV=prod) thay vì chạy script này.
set -euo pipefail

copy() {
  local from=$1 to=$2
  kubectl create ns "$to" --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n "$from" get secret -o json | python3 -c '
import json, sys
src, dst = sys.argv[1], sys.argv[2]
out = []
for s in json.load(sys.stdin)["items"]:
    if s["type"] not in ("Opaque", "kubernetes.io/dockerconfigjson"):
        continue  # bỏ token SA, helm release...
    out.append({"apiVersion": "v1", "kind": "Secret", "type": s["type"], "data": s["data"],
                "metadata": {"name": s["metadata"]["name"], "namespace": dst,
                             "labels": {"app.kubernetes.io/part-of": "live-commerce", "copied-from": src}}})
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": out}))
' "$from" "$to" | kubectl apply -f -
}

copy live-commerce-dev live-commerce
copy moderation-dev moderation

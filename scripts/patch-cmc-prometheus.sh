#!/usr/bin/env bash
# Gộp job scrape Kong + recording rule (platform/monitoring/cmc-prometheus/) vào Prometheus CMC rồi restart.
#   ./scripts/patch-cmc-prometheus.sh
# Prometheus CMC (bitnami, ns cmc-monitoring) không có sidecar reload, data là emptyDir (retention 1d):
# restart = mất số liệu cũ, Grafana trống vài phút. Chạy lại được; CMC nâng cấp Prometheus thì chạy lại.
# Cần yq v4 (mikefarah).
set -euo pipefail
cd "$(dirname "$0")/.."

NS=cmc-monitoring CM=prometheus-server DIR=platform/monitoring/cmc-prometheus
RULES_PATH=/opt/bitnami/prometheus/conf/rules.yaml   # ConfigMap mount ở /opt/bitnami/prometheus/conf

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
kubectl -n "$NS" get cm "$CM" -o jsonpath='{.data.prometheus\.yaml}' > "$tmp/prometheus.yaml"
cp "$tmp/prometheus.yaml" "$tmp/prometheus.yaml.bak"

# Bỏ job cũ (kong-metrics gắn nhãn tay, kong của lần chạy trước), thêm job mới, khai báo file rule.
JOB="$DIR/kong-scrape.yaml" RULES_PATH="$RULES_PATH" yq -i '
  .scrape_configs |= map(select(.job_name != "kong-metrics" and .job_name != "kong")) |
  .scrape_configs += [load(strenv(JOB))] |
  .rule_files = ((.rule_files // []) + [strenv(RULES_PATH)] | unique)
' "$tmp/prometheus.yaml"

echo "Thay đổi prometheus.yaml:"
diff "$tmp/prometheus.yaml.bak" "$tmp/prometheus.yaml" || true

python3 - "$tmp/prometheus.yaml" "$DIR/rules.yaml" > "$tmp/patch.json" <<'EOF'
import json, sys
print(json.dumps({"data": {"prometheus.yaml": open(sys.argv[1]).read(), "rules.yaml": open(sys.argv[2]).read()}}))
EOF
kubectl -n "$NS" patch cm "$CM" --type merge --patch-file "$tmp/patch.json"
kubectl -n "$NS" rollout restart deploy/prometheus-server
kubectl -n "$NS" rollout status deploy/prometheus-server --timeout=180s
echo "Kiểm tra: Prometheus → Status > Targets (job kong, mỗi pod Kong một target) và Status > Rules (kong-hpa)."

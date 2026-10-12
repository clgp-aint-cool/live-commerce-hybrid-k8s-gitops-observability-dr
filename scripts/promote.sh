#!/usr/bin/env bash
# Sao chép tag image từ overlay nguồn sang overlay đích.
#   ./scripts/promote.sh dev prod                                  # mọi service
#   ./scripts/promote.sh dev prod "order-service,web"              # chỉ các service liệt kê
# Tên service = phần cuối tên image (auth-service, chat-service, live-manager, order-service,
# product-service, web, moderation-service).
# Yêu cầu: kustomize, yq (mikefarah).
set -euo pipefail

FROM="${1:?Thiếu môi trường nguồn (vd: dev)}"
TO="${2:?Thiếu môi trường đích (vd: prod)}"
COMPONENTS="${3:-all}"

command -v kustomize >/dev/null || { echo "Cần cài kustomize"; exit 1; }
command -v yq >/dev/null || { echo "Cần cài yq"; exit 1; }

# "a, b ,c" -> " a b c " để so khớp nguyên từ.
want=" $(echo "$COMPONENTS" | tr ',' ' ' | xargs) "
selected() { [ "$want" = " all " ] || [[ "$want" == *" $1 "* ]]; }

found=""
for app in live-commerce moderation; do
  src="apps/${app}/overlays/${FROM}/kustomization.yaml"
  dst="apps/${app}/overlays/${TO}"
  [ -f "$src" ] || { echo "Bỏ qua ${app}: không có ${src}"; continue; }
  # Overlay chỉ đặt newTag (không có newName) → mặc định newName = name, nếu không `read` sẽ lệch cột.
  while read -r name newname newtag; do
    [ -n "$name" ] || continue
    svc="${name##*/}"
    found="$found $svc"
    selected "$svc" || continue
    [ -n "$newtag" ] || { echo "Lỗi: ${name} trong ${src} không có newTag"; exit 1; }
    echo "${app}: ${svc} -> ${newname}:${newtag}"
    (cd "$dst" && kustomize edit set image "${name}=${newname}:${newtag}")
  done < <(yq -r '.images[]? | .name + " " + (.newName // .name) + " " + (.newTag // "")' "$src")
done

# Báo lỗi tên gõ sai thay vì lặng lẽ không promote gì.
if [ "$want" != " all " ]; then
  for c in $want; do
    [[ " $found " == *" $c "* ]] || { echo "Lỗi: không có service '${c}'. Có:${found}"; exit 1; }
  done
fi

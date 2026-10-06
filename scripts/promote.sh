#!/usr/bin/env bash
# Sao chép tag image từ overlay nguồn sang overlay đích. Ví dụ: ./scripts/promote.sh dev prod
# Yêu cầu: kustomize, yq (mikefarah).
set -euo pipefail

FROM="${1:?Thiếu môi trường nguồn (vd: dev)}"
TO="${2:?Thiếu môi trường đích (vd: prod)}"

command -v kustomize >/dev/null || { echo "Cần cài kustomize"; exit 1; }
command -v yq >/dev/null || { echo "Cần cài yq"; exit 1; }

for app in live-commerce moderation; do
  src="apps/${app}/overlays/${FROM}/kustomization.yaml"
  dst="apps/${app}/overlays/${TO}"
  [ -f "$src" ] || { echo "Bỏ qua ${app}: không có ${src}"; continue; }
  yq -r '.images[]? | .name + " " + .newName + " " + .newTag' "$src" | while read -r name newname newtag; do
    echo "${app}: ${name} -> ${newtag}"
    (cd "$dst" && kustomize edit set image "${name}=${newname}:${newtag}")
  done
done

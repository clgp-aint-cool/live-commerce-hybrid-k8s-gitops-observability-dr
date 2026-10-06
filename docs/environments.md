# Môi trường

| | dev | prod |
|---|---|---|
| Namespace | `live-commerce-dev`, `moderation-dev` | `live-commerce`, `moderation` |
| Host | `dev.live.example.com` | `live.example.com` |
| Replica | 1 (HPA 1-3) | theo base (HPA chat 2-20, order 2-10) |
| ArgoCD sync | Tự động (prune + selfHeal) | Thủ công sau khi merge PR promote |
| NodePort webhook moderation | 30184 | 30084 |

Giả định: dev và prod chạy chung một cluster, tách bằng namespace. Nếu dùng hai cluster riêng, đăng ký thêm cluster vào ArgoCD, đổi `destination.server` trong `argocd/apps/workloads/*` và có thể bỏ hậu tố `-dev`.

Kong và ingress HAProxy đặt ở namespace `kong` dùng chung; route của từng môi trường nằm cạnh service (resource `kong-routes`). Đây là lựa chọn thiết kế, có thể đổi sang Kong riêng cho từng namespace nếu cần.

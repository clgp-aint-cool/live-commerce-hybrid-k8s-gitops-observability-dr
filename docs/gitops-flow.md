# GitOps flow

| Việc | Ở đâu | Ai làm |
|---|---|---|
| Viết code, PR, CI | repo `live-commerce-app` | Team dev |
| Build image, đẩy Harbor, ghi tag dev | workflow `build-and-deploy` (repo app) | CI bot |
| Cấu hình K8s, thêm service, đổi tài nguyên | repo này, qua PR | DevOps |
| Đồng bộ vào cluster | ArgoCD | Tự động (dev), thủ công (prod) |
| Lên prod | workflow `promote` (repo này) -> PR -> merge -> Sync | DevOps + dev lead |
| Rollback | `git revert` commit trong repo này, ArgoCD sync lại | DevOps |

Nguyên tắc: **cluster không được sửa tay**. Mọi thay đổi đi qua Git; ArgoCD bật `selfHeal` ở dev để kéo cluster về đúng trạng thái trong Git.

## Thêm một service mới
1. Tạo thư mục `apps/<ns>/base/<ten-service>/` (deployment, service, configmap, kustomization) rồi thêm vào `base/kustomization.yaml`.
2. Thêm tên image vào khối `images:` của các overlay dev và prod.
3. Thêm route trong `kong-routes.yaml` nếu service cần truy cập từ ngoài.
4. Bên repo app: thêm bộ lọc đường dẫn vào các workflow và case ánh xạ overlay trong `build-and-deploy.yml`.

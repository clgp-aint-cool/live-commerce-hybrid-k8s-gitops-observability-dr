# Secrets (prod)

Không commit secret thường. Tạo SealedSecret rồi lưu file `*.sealed.yaml` vào thư mục này, sau đó bỏ chú thích dòng `- secrets/` trong `kustomization.yaml` của overlay (cần thêm `secrets/kustomization.yaml` liệt kê các file).

Secret cần có (moderation):
- `harbor-registry`
- `moderation-service-secret` (`DATABASE_URL` tới DBaaS, `WEBHOOK_SHARED_SECRET`, `INTERNAL_API_TOKEN`)

Chi tiết cách tạo: xem `docs/secrets.md` ở thư mục gốc repo.

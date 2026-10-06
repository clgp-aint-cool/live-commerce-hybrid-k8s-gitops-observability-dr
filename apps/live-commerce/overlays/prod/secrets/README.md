# Secrets (prod)

Không commit secret thường. Tạo SealedSecret rồi lưu file `*.sealed.yaml` vào thư mục này, sau đó bỏ chú thích dòng `- secrets/` trong `kustomization.yaml` của overlay (cần thêm `secrets/kustomization.yaml` liệt kê các file).

Secret cần có (live-commerce):
- `harbor-registry` (docker-registry)
- `chat-service-secret`, `product-service-secret`, `order-service-secret` (DATABASE_URL, REDIS_PASSWORD...)

Chi tiết cách tạo: xem `docs/secrets.md` ở thư mục gốc repo.

# Secrets (prod)

Không commit secret thường. Tạo SealedSecret rồi lưu file `*.sealed.yaml` vào thư mục này, sau đó bỏ chú thích dòng `- secrets/` trong `kustomization.yaml` của overlay (cần thêm `secrets/kustomization.yaml` liệt kê các file).

Secret cần có (live-commerce):
- `harbor-registry` (docker-registry)
- `product-service-secret`, `auth-service-secret`, `order-service-secret`, `chat-service-secret`, `live-manager-secret`
  (`DATABASE_URL`/`REDIS_URL` trỏ tới DBaaS PostgreSQL/Redis của CMC Cloud, cùng các token; bảng đầy đủ ở `docs/migrate-cmc-k8s.md` mục 7.1)

Cách nhanh không cần SealedSecret: `scripts/deploy.sh` tạo Secret thường thẳng vào cluster.

Chi tiết cách tạo: xem `docs/secrets.md` ở thư mục gốc repo.

Hiện tại prod **dùng chung DB/Redis/S3 với dev**: `scripts/copy-secrets.sh` sao chép Secret từ `live-commerce-dev`/`moderation-dev` sang (chạy lại khi đổi secret ở dev).

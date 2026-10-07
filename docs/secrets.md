# Quản lý secret (Sealed Secrets)

Secret được mã hóa bằng khóa công khai của cluster, nên file `*.sealed.yaml` commit an toàn vào Git.

```bash
# Ví dụ: secret cho order-service ở dev
kubectl create secret generic order-service-secret \
  -n live-commerce-dev \
  --from-literal=DATABASE_URL="postgres://<user>:<pass>@<PG_HOST DBaaS>:5432/<db>" \
  --dry-run=client -o yaml \
| kubeseal --controller-name sealed-secrets-controller --controller-namespace sealed-secrets --format yaml \
> apps/live-commerce/overlays/dev/secrets/order-service-secret.sealed.yaml
```

Secret registry cho Harbor (cần ở mỗi namespace ứng dụng):

```bash
kubectl create secret docker-registry harbor-registry \
  -n live-commerce-dev --docker-server=192.168.6.194 \
  --docker-username=<robot> --docker-password=<token> --dry-run=client -o yaml \
| kubeseal --controller-name sealed-secrets-controller --controller-namespace sealed-secrets --format yaml \
> apps/live-commerce/overlays/dev/secrets/harbor-registry.sealed.yaml
```

Các secret khác cần tạo: `cloudflare-cert` (namespace `kong`), `grafana-admin` (`monitoring`), `velero-credentials` (`velero`).

**Sao lưu khóa bí mật của Sealed Secrets controller** ở nơi an toàn: nếu mất khóa, không giải mã lại được các file đã commit.
Phương án thay thế nếu nhóm muốn: SOPS + age hoặc External Secrets.

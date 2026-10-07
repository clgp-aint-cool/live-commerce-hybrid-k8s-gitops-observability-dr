# live-commerce-infra

Repo GitOps: **nguồn sự thật** cho mọi thứ chạy trên Kubernetes của dự án Interactive Live Commerce. ArgoCD đọc repo này và đồng bộ vào cluster.

## Cấu trúc

```
live-commerce-infra/
├─ argocd/
│  ├─ install/         # cách cài ArgoCD + bootstrap
│  ├─ projects/        # AppProject: platform, live-commerce
│  ├─ bootstrap/       # root-app.yaml (App of Apps)
│  └─ apps/
│     ├─ platform/     # Application: sealed-secrets, haproxy, kong, monitoring, loki, alloy, velero, edge
│     └─ workloads/    # Application: live-commerce-{dev,prod}, moderation-{dev,prod}
├─ platform/           # values Helm + manifest của các add-on dùng chung
│  ├─ edge/            # Ingress HAProxy -> Kong
│  ├─ haproxy-ingress/ ├─ kong/ ├─ monitoring/ ├─ velero/ └─ sealed-secrets/
├─ apps/               # workload của dự án (Kustomize: base + overlays)
│  ├─ live-commerce/{base,overlays/{dev,prod}}   # chat, product, order, web, kong-routes
│  └─ moderation/{base,overlays/{dev,prod}}      # moderation-service + webhook NodePort
├─ cloud/              # IaC ngoài K8s: aws/ (CDK), network/ (Palo Alto, VPN)
├─ scripts/promote.sh  # sao chép tag dev -> prod
├─ docs/               # gitops-flow, environments, secrets
└─ .github/workflows/  # validate.yml (PR), promote.yml (thủ công)
```

## Bắt đầu

> Triển khai thủ công lên Kubernetes CMC Cloud (chưa có CD, image từ Harbor 192.168.6.194): làm theo [docs/migrate-cmc-k8s.md](docs/migrate-cmc-k8s.md).

1. Thay `<ORG>` bằng tên GitHub org/user: `grep -R "<ORG>" .`
2. Thay `harbor.example.local` và `live.example.com` bằng giá trị thật.
3. Ghim phiên bản: tìm `<PIN_VERSION>` trong `argocd/apps/platform/*` và `platform/velero/values.yaml`.
4. Làm theo [argocd/install/README.md](argocd/install/README.md) để cài ArgoCD và bootstrap.
5. Tạo các secret theo [docs/secrets.md](docs/secrets.md).

## Kiểm tra cục bộ

```bash
make validate     # yamllint + kustomize build + kubeconform (cần cài 3 công cụ này)
```

Xem thêm: [docs/gitops-flow.md](docs/gitops-flow.md), [docs/environments.md](docs/environments.md).

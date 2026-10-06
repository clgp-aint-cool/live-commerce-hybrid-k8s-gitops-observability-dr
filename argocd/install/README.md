# Cài ArgoCD và bootstrap

1. Cài ArgoCD (một lần, bằng tay):
   ```bash
   helm repo add argo https://argoproj.github.io/argo-helm
   helm install argocd argo/argo-cd -n argocd --create-namespace -f argocd/install/values.yaml --version <PIN_VERSION>
   ```
   Cần ArgoCD >= 2.6 để dùng Application nhiều nguồn (`sources` + `ref: values`).
2. Cho ArgoCD quyền đọc repo infra (nếu repo private): thêm deploy key SSH hoặc token trong *Settings > Repositories*.
3. Áp dụng Project và root app:
   ```bash
   kubectl apply -f argocd/projects/
   kubectl apply -f argocd/bootstrap/root-app.yaml
   ```
4. Từ đây mọi thứ do Git điều khiển: sửa file trong repo, merge, ArgoCD tự đồng bộ.

Thay `<ORG>` trong các file `argocd/**` bằng tên organization/user GitHub của bạn (tìm: `grep -R "<ORG>" .`).

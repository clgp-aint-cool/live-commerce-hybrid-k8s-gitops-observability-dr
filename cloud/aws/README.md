# Hạ tầng AWS (không do ArgoCD quản lý)

Nơi đặt code CDK/Terraform cho phần AWS: kênh IVS, recording configuration (thumbnail), S3, rules engine moderation
(từ `aws-samples/aws-ai-content-moderation-livestream`), VGW, Customer Gateway, VPN Connection và Lambda cầu nối webhook.

ArgoCD chỉ đọc các thư mục `argocd/`, `platform/`, `apps/`, nên thư mục này an toàn để chứa code IaC khác.

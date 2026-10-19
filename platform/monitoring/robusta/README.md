# Robusta

Robusta (chart `robusta/robusta` 0.53.0, ns `robusta`) gửi alert pod/deployment vào Discord #alert-critical, kèm log, event, kết quả điều tra pod và link runbook.

Nguồn alert:
- **Kubernetes trực tiếp**: builtin playbook của Robusta (pod crash, OOMKilled, ImagePullBackOff; 1 lần/giờ mỗi pod).
  Không thêm playbook riêng cho chúng (sinh thêm tin INFO "Update to pod"); link runbook do `alert-bot` gắn vào ticket.
- **Grafana**: 6 rule có nhãn `route=robusta` (`ROBUSTA_RULES` trong `grafana/gen.py`), route `robusta-rca` gửi webhook tới `robusta-runner:80/api/alerts`. Các rule này không gửi thẳng Discord.

`playbooks.yaml` sinh từ `grafana/gen.py`: mỗi alert một playbook, gắn link `https://runbooklive-e-commerce.vercel.app/#<uid>`
(Discord sink của Robusta không hiển thị annotation `runbook_url` thành nút). Trigger khớp theo **tên alert = title rule**: đổi title thì chạy lại `gen.py`.

Webhook Discord (`sinksConfig`) chỉ nằm trong Helm release, không commit. Áp dụng playbook, giữ nguyên các values còn lại:

```bash
helm upgrade robusta robusta/robusta -n robusta --version 0.53.0 --reuse-values -f platform/monitoring/robusta/playbooks.yaml
```

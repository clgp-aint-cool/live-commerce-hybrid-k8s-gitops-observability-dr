# alert-bot – nhận ticket cho alert Discord

Webhook của Robusta/Grafana không tag được role theo nội dung alert, cũng không có nút bấm.
Bot này đọc các kênh alert và trả lời mỗi alert mới bằng 1 tin **ticket**:

```
🎫 Ticket @oncall
Alert: `Crashing pod payment-xyz in namespace live-commerce-dev`
Trạng thái: 🔴 Chưa có người nhận            [🙋 Nhận xử lý] [✅ Đã xử lý]
   → 🟡 Đang xử lý: @an (nhận 2 phút trước)  [↩️ Trả ticket] [✅ Đã xử lý]
   → ✅ Đã xử lý bởi @an 5 phút trước
```

| Sự kiện | Bot làm gì |
|---|---|
| Alert Robusta firing | Ticket + tag `DISCORD_ROLE_ONCALL` (alert có chữ `moderation` → `DISCORD_ROLE_TRUST_SAFETY`) |
| Alert Grafana firing | Ticket, **không** tag lại (contact point Grafana đã tag role) |
| 🙋 Nhận xử lý | Ghi người nhận vào ticket, mở thread trên tin alert, react 👀. Người khác bấm sẽ được báo "đang do @X xử lý" |
| ↩️ Trả ticket | Chỉ người nhận (hoặc người có quyền Manage Messages) |
| ✅ Đã xử lý (alert Grafana / Robusta qua Grafana) | Ticket → 🟢 **Chờ xác nhận**. Alert RESOLVED về → đóng thật. Quá `VERIFY_SECONDS` (10 phút) chưa RESOLVED → **mở lại**, giao lại và tag người đã bấm, ghi "alert chưa hết" |
| ✅ Đã xử lý (Robusta crash/OOM/ImagePull – Kubernetes event, không bao giờ RESOLVED) | Đóng ngay |
| Alert RESOLVED | Đóng ticket, react ✅ lên alert, archive thread |
| Alert lặp lại khi ticket còn mở | Đã có người nhận/đang chờ xác nhận → xóa tin lặp; chưa ai nhận → giữ tối đa 1 tin / 2 phút. Số lần lặp ghi trên ticket |

Trạng thái nằm trong chính tin ticket (không có DB), nút là persistent view nên restart bot không mất ticket.

## Cài đặt

1. Discord Developer Portal → **New Application** → tab *Bot*: lấy token, bật **Message Content Intent**.
2. Mời bot vào server (OAuth2 → URL Generator, scope `bot`) với quyền: View Channels, Send Messages,
   Read Message History, Add Reactions, Create Public Threads, Send Messages in Threads, Manage Threads.
   Cấp quyền đó trên các kênh `#alert-critical`, `#alert-warning`, kênh trust-safety
   (tên kênh khai báo ở env `ALERT_CHANNELS` trong `deployment.yaml`).
3. Thêm token vào `~/.config/live-commerce/discord.env` (dòng `export DISCORD_BOT_TOKEN=...`), rồi tạo Secret
   (không commit, không dán token vào chat):

   ```bash
   source ~/.config/live-commerce/discord.env
   kubectl -n monitoring create secret generic alert-bot-discord \
     --from-literal=DISCORD_BOT_TOKEN="$DISCORD_BOT_TOKEN" \
     --from-literal=DISCORD_ROLE_ONCALL="$DISCORD_ROLE_ONCALL" \
     --from-literal=DISCORD_ROLE_TRUST_SAFETY="$DISCORD_ROLE_TRUST_SAFETY" \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

4. `kubectl apply -k platform/monitoring/alert-bot` rồi `kubectl -n monitoring logs deploy/alert-bot` phải thấy
   `logged in as ...`.

Pod cài `discord.py` từ PyPI khi khởi động (image `python:3.12-slim`, không cần build/push Harbor).
Giữ `replicas: 1`: 2 bản sẽ tạo 2 ticket cho mỗi alert.

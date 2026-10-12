# Discord – server nhận alert Grafana (Live Commerce)

`bootstrap.py` dựng toàn bộ server từ một server trống, chạy lại nhiều lần được, và tạo **Server Template** để nhân bản (staging, đội khác).

## Cấu trúc

```
📌 THÔNG TIN        (mọi role đọc, không ai chat)
   #readme              ghim: giới thiệu + quy ước xử lý alert
   #announcements
🚨 ALERTS           (chỉ webhook Grafana đăng; người dùng react + thread)
   #alert-critical      severity=critical  · tag @SRE On-call · Business Viewer xem được
   #alert-warning       severity=warning/info
   #alert-trust-safety  team=trust-safety  · tag @Trust & Safety
🔥 INCIDENT         (On-call, Platform, T&S)
   #incident-war-room   mỗi sự cố một thread
   #postmortem
   🔊 War Room
🛠 PLATFORM         (On-call, Platform)
   #platform-general
   #deployments
```

| Role              | Thấy                                     | Ghi chú                                                              |
| ----------------- | ---------------------------------------- | -------------------------------------------------------------------- |
| SRE On-call       | tất cả                                   | Bị tag khi có critical. Gán cho người đang trực (xoay ca theo tuần). |
| Platform Engineer | tất cả                                   |                                                                      |
| Trust & Safety    | Thông tin, #alert-trust-safety, Incident | Bị tag khi live bị moderation dừng.                                  |
| Business Viewer   | Thông tin, #alert-critical (chỉ đọc)     | Cho PO/vận hành biết sự cố đang diễn ra.                             |
| @everyone         | chỉ #readme, #announcements              | Người mới vào chưa có role không thấy alert.                         |

Cài đặt server: thông báo mặc định **chỉ khi được mention**, xác minh tài khoản mức Medium, lọc nội dung với mọi thành viên.

Quy ước ack (ghim trong mỗi kênh alert): 👀 đang xử lý (người react đầu tiên là owner), ✅ xong, 🔕 đã silence. Thảo luận trong thread của tin alert.

## Cách dựng

1. **Tạo server** (Discord → _Add a Server → Create My Own_), đặt tên ví dụ `Live Commerce Ops`.
2. **Tạo bot tạm:** <https://discord.com/developers/applications> → New Application → Bot → _Reset Token_, copy token.
   Không cần bật Privileged Intents.
3. **Mời bot vào server** với quyền Administrator (chỉ dùng để dựng, xong sẽ hạ quyền ở bước 6):
   `https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot&permissions=8`
4. **Lấy Server ID:** User Settings → Advanced → bật _Developer Mode_, chuột phải server → _Copy Server ID_.
5. **Chạy script** (gõ token ở terminal của bạn, đừng dán vào chat/commit):

   ```bash
   read -rs DISCORD_BOT_TOKEN && export DISCORD_BOT_TOKEN
   python3 platform/monitoring/discord/bootstrap.py --guild <SERVER_ID> --dry-run   # xem trước
   python3 platform/monitoring/discord/bootstrap.py --guild <SERVER_ID>
   unset DISCORD_BOT_TOKEN
   ```

   Kết quả: `~/.config/live-commerce/discord.env` (chmod 600) chứa 3 webhook + 2 role ID, và link `https://discord.new/<code>` của template.

6. **Hạ quyền bot, KHÔNG kick:** webhook do bot tạo sẽ **bị Discord xóa khi bot rời server**. Server Settings → Roles → role của bot → tắt hết quyền (bỏ Administrator). Sau đó vào Developer Portal → Bot → *Reset Token* và không lưu token mới. Bot còn trong server nhưng không có quyền và không ai dùng được.
   Không muốn giữ bot: tạo webhook bằng tay (Channel Settings → Integrations → Webhooks → *New Webhook*, đặt tên `Grafana`) cho 3 kênh alert, rồi thay URL trong `discord.env`. Webhook tạo tay thuộc về người tạo, không phụ thuộc bot.
7. **Gán role** cho thành viên. Bật **2FA requirement for moderation** (Server Settings → Safety Setup; chỉ owner bật được).
8. **Nối với Grafana:** làm tiếp từ bước 1 trong [`../grafana/README.md`](../grafana/README.md#triển-khai-đúng-thứ-tự).

## Vận hành

- **Alert không về Discord, Grafana báo `Unknown Webhook` (10015):** webhook đã bị xóa (thường do kick bot). Tạo lại webhook (bước 5 hoặc 6), sửa `discord.env`, rồi tạo lại Secret theo bước 1 của `../grafana/README.md`.
- **Lộ webhook URL** (ai có URL đều đăng được vào kênh): xóa webhook _Grafana_ trong Channel Settings → Integrations. Chạy lại `bootstrap.py` để tạo webhook mới, rồi tạo lại Secret `grafana-alert-contact-points`.
- **Server mới từ template:** mở link `discord.new/...`. Template chỉ chứa role, kênh và quyền, không chứa webhook và tin ghim. Sau đó chạy `bootstrap.py` trên server mới để có webhook và ghim tin.
- **Đổi cấu trúc:** sửa `LAYOUT` trong `bootstrap.py` rồi chạy lại. Script đồng bộ quyền, topic và template (không xóa kênh thừa).

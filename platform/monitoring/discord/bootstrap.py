#!/usr/bin/env python3
"""Dựng server Discord nhận alert Grafana cho Live Commerce (role, channel, quyền, webhook, template).

Chạy lại nhiều lần được: role/channel/webhook đã có (theo tên) thì dùng lại, không tạo trùng.

    export DISCORD_BOT_TOKEN=...   # bot tạm, xem README.md
    python3 bootstrap.py --guild <SERVER_ID>             # tạo thật
    python3 bootstrap.py --guild <SERVER_ID> --dry-run   # chỉ in kế hoạch

Kết quả (webhook URL, role ID) ghi vào --out (mặc định ~/.config/live-commerce/discord.env, chmod 600),
KHÔNG in ra màn hình và không nằm trong repo. Chỉ dùng thư viện chuẩn.
"""
import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

API = "https://discord.com/api/v10"
UA = "DiscordBot (https://github.com/clgp-aint-cool/live-commerce-infra, 1.0)"

# Bit quyền Discord
VIEW, SEND, REACT, HISTORY = 1 << 10, 1 << 11, 1 << 6, 1 << 16
THREAD_CREATE, THREAD_SEND = 1 << 35, 1 << 38
CONNECT, SPEAK = 1 << 20, 1 << 21
EMBED, ATTACH = 1 << 14, 1 << 15

READ = VIEW | HISTORY
FEED = READ | REACT | THREAD_CREATE | THREAD_SEND       # đọc feed alert, ack bằng reaction, thảo luận trong thread
CHAT = READ | REACT | SEND | EMBED | ATTACH | THREAD_CREATE | THREAD_SEND
VOICE = VIEW | CONNECT | SPEAK

ROLES = {
    # key: (tên, màu, hiện riêng trong danh sách thành viên, cho phép @mention)
    "oncall": ("SRE On-call", 0xE74C3C, True, True),
    "platform": ("Platform Engineer", 0x3498DB, True, True),
    "ts": ("Trust & Safety", 0x9B59B6, True, True),
    "biz": ("Business Viewer", 0x95A5A6, False, False),
}

ACK = ("**Quy ước xử lý alert**\n"
       "• 👀 = đang xử lý (người react đầu tiên là owner) · ✅ = đã xử lý xong · 🔕 = đã silence trên Grafana\n"
       "• Thảo luận **trong thread** của tin alert, không chat thẳng vào kênh.\n"
       "• Sự cố ảnh hưởng người dùng → mở thread ở #incident-war-room, ghi timeline; xong thì viết #postmortem.\n")

# Cấu trúc server. "access": role -> quyền allow; @everyone luôn bị ẩn trừ khi public=True.
LAYOUT = [
    {"category": "📌 THÔNG TIN", "public": True, "access": {"oncall": READ, "platform": READ, "ts": READ, "biz": READ},
     "channels": [
         {"name": "readme", "topic": "Giới thiệu server, quy ước, link Grafana & runbook.",
          "pin": "**Live Commerce · Ops Alerting**\n"
                 "Server nhận cảnh báo từ Grafana (cluster CMC HCM-01).\n\n"
                 "• #alert-critical – sự cố ảnh hưởng người dùng/doanh thu, tag @SRE On-call, phải ack ≤ 5 phút.\n"
                 "• #alert-warning – cần xem trong giờ làm việc.\n"
                 "• #alert-trust-safety – phiên live bị dừng do vi phạm, đội Trust & Safety xem xét.\n"
                 "• Runbook: platform/monitoring/grafana/README.md trong repo live-commerce-infra.\n\n" + ACK},
         {"name": "announcements", "topic": "Thông báo bảo trì, thay đổi lớn."},
     ]},
    {"category": "🚨 ALERTS", "access": {"oncall": FEED, "platform": FEED},
     "channels": [
         {"name": "alert-critical", "webhook": "DISCORD_WEBHOOK_CRITICAL", "extra": {"biz": READ},
          "topic": "Grafana · severity=critical · ack ≤ 5 phút bằng 👀 · nhắc lại mỗi 30 phút đến khi resolved", "pin": ACK},
         {"name": "alert-warning", "webhook": "DISCORD_WEBHOOK_WARNING",
          "topic": "Grafana · severity=warning/info · xử lý trong giờ làm việc · nhắc lại mỗi 4h", "pin": ACK},
         {"name": "alert-trust-safety", "webhook": "DISCORD_WEBHOOK_TRUST_SAFETY", "extra": {"ts": FEED},
          "topic": "Grafana · team=trust-safety · phiên live bị moderation dừng", "pin": ACK},
     ]},
    {"category": "🔥 INCIDENT", "access": {"oncall": CHAT | VOICE, "platform": CHAT | VOICE, "ts": CHAT | VOICE},
     "channels": [
         {"name": "incident-war-room", "topic": "Mỗi sự cố một thread: [SEV1] tóm tắt – owner – timeline."},
         {"name": "postmortem", "topic": "Postmortem không đổ lỗi: nguyên nhân, tác động, action item."},
         {"name": "War Room", "voice": True},
     ]},
    {"category": "🛠 PLATFORM", "access": {"oncall": CHAT, "platform": CHAT},
     "channels": [
         {"name": "platform-general", "topic": "Trao đổi chung của đội platform."},
         {"name": "deployments", "topic": "Thông báo deploy / ArgoCD sync."},
     ]},
]


class Discord:
    def __init__(self, token, dry):
        self.token, self.dry = token, dry

    def call(self, method, path, body=None):
        if self.dry and method != "GET":
            print(f"  [dry-run] {method} {path} {json.dumps(body, ensure_ascii=False)[:120] if body else ''}")
            return {"id": f"dry-{path.rsplit('/', 1)[-1]}", "token": "x", "code": "dry"}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": f"Bot {self.token}", "User-Agent": UA, "Content-Type": "application/json"})
        for _ in range(5):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(float(json.loads(e.read()).get("retry_after", 1)) + 0.2)
                    continue
                sys.exit(f"Discord API {method} {path} -> {e.code}: {e.read().decode()[:300]}")
        sys.exit(f"Discord API {method} {path}: bị rate limit quá nhiều lần")


def overwrites(guild_id, role_ids, access, public):
    ow = [{"id": guild_id, "type": 0, "allow": str(READ if public else 0),
           "deny": str((SEND | THREAD_CREATE) if public else VIEW)}]
    for key, allow in access.items():
        deny = 0 if allow & SEND else SEND                # kênh feed: chỉ webhook được đăng
        ow.append({"id": role_ids[key], "type": 0, "allow": str(allow), "deny": str(deny)})
    return ow


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--guild", required=True, help="Server ID (bật Developer Mode → chuột phải server → Copy Server ID)")
    ap.add_argument("--out", default="~/.config/live-commerce/discord.env")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-template", action="store_true", help="không tạo/đồng bộ Server Template")
    a = ap.parse_args()

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token and not a.dry_run:
        sys.exit("Thiếu biến môi trường DISCORD_BOT_TOKEN")
    dc, g = Discord(token, a.dry_run), a.guild
    dry_read = a.dry_run and not token                    # dry-run không token: giả định server trống

    guild = {"name": "(dry-run)"} if dry_read else dc.call("GET", f"/guilds/{g}")
    print(f"Server: {guild['name']}")

    # Cài đặt server: chỉ thông báo khi được mention, xác minh email + tài khoản > 5 phút, lọc nội dung.
    dc.call("PATCH", f"/guilds/{g}", {"default_message_notifications": 1, "verification_level": 2,
                                      "explicit_content_filter": 2})

    # Role
    existing = {} if dry_read else {r["name"]: r["id"] for r in dc.call("GET", f"/guilds/{g}/roles")}
    role_ids, out = {}, {}
    for key, (name, color, hoist, mention) in ROLES.items():
        if name in existing:
            role_ids[key] = existing[name]
            print(f"role  = {name}")
        else:
            role_ids[key] = dc.call("POST", f"/guilds/{g}/roles", {"name": name, "color": color, "hoist": hoist,
                                                                   "mentionable": mention, "permissions": "0"})["id"]
            print(f"role  + {name}")
    out["DISCORD_ROLE_ONCALL"] = role_ids["oncall"]
    out["DISCORD_ROLE_TRUST_SAFETY"] = role_ids["ts"]

    # Category + channel
    chans = [] if dry_read else dc.call("GET", f"/guilds/{g}/channels")
    by_name = {(c["name"], c.get("parent_id"), c["type"]): c for c in chans}
    for cat in LAYOUT:
        ow = overwrites(g, role_ids, cat["access"], cat.get("public", False))
        c = by_name.get((cat["category"], None, 4))
        if c:
            cat_id = c["id"]
            dc.call("PATCH", f"/channels/{cat_id}", {"permission_overwrites": ow})
            print(f"cat   = {cat['category']}")
        else:
            cat_id = dc.call("POST", f"/guilds/{g}/channels", {"name": cat["category"], "type": 4,
                                                               "permission_overwrites": ow})["id"]
            print(f"cat   + {cat['category']}")

        for ch in cat["channels"]:
            ctype = 2 if ch.get("voice") else 0
            access = {**cat["access"], **ch.get("extra", {})}
            ch_ow = overwrites(g, role_ids, access, cat.get("public", False))
            c = by_name.get((ch["name"], cat_id, ctype))
            new = c is None
            if c:
                ch_id = c["id"]
                body = {"permission_overwrites": ch_ow}
                if "topic" in ch:
                    body["topic"] = ch["topic"]
                dc.call("PATCH", f"/channels/{ch_id}", body)
                print(f"  #   = {ch['name']}")
            else:
                body = {"name": ch["name"], "type": ctype, "parent_id": cat_id, "permission_overwrites": ch_ow}
                if "topic" in ch:
                    body["topic"] = ch["topic"]
                ch_id = dc.call("POST", f"/guilds/{g}/channels", body)["id"]
                print(f"  #   + {ch['name']}")

            if new and ch.get("pin"):
                msg = dc.call("POST", f"/channels/{ch_id}/messages",
                              {"content": ch["pin"], "allowed_mentions": {"parse": []}})
                dc.call("PUT", f"/channels/{ch_id}/pins/{msg['id']}")

            if ch.get("webhook"):
                hooks = [] if dry_read or new and a.dry_run else dc.call("GET", f"/channels/{ch_id}/webhooks")
                hook = next((h for h in hooks if h.get("name") == "Grafana" and h.get("token")), None)
                if hook:
                    print("      webhook = Grafana")
                else:
                    hook = dc.call("POST", f"/channels/{ch_id}/webhooks", {"name": "Grafana"})
                    print("      webhook + Grafana")
                out[ch["webhook"]] = f"https://discord.com/api/webhooks/{hook['id']}/{hook['token']}"

    # Server Template: chụp lại role + channel + quyền để dựng server giống hệt (staging, đội khác…).
    if not a.no_template:
        tpls = [] if dry_read else dc.call("GET", f"/guilds/{g}/templates")
        if tpls:
            code = dc.call("PUT", f"/guilds/{g}/templates/{tpls[0]['code']}")["code"]
        else:
            code = dc.call("POST", f"/guilds/{g}/templates", {
                "name": "Live Commerce Ops Alerting",
                "description": "Kênh alert Grafana theo severity, incident, quyền theo role on-call/platform/T&S"})["code"]
        print(f"\nServer Template: https://discord.new/{code}")

    if a.dry_run:
        print("\n[dry-run] không ghi file env.")
        return
    path = pathlib.Path(a.out).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text("".join(f"export {k}='{v}'\n" for k, v in out.items()))
    print(f"\nĐã ghi {', '.join(out)} vào {path} (chmod 600).")
    print("Bước tiếp theo: README.md mục 'Nối với Grafana'.")
    print("KHÔNG kick bot: Discord xóa webhook do bot tạo khi bot rời server. Chỉ bỏ quyền của bot + reset token.")


if __name__ == "__main__":
    main()

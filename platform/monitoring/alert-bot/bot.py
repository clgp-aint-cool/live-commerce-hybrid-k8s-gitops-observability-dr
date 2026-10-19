"""Bot "nhận ticket" cho các kênh alert Discord (Robusta + Grafana).

Robusta/Grafana gửi alert qua webhook, webhook không tag được role theo nội dung và không có nút bấm.
Bot đọc mỗi alert mới và trả lời bằng 1 tin "ticket":
  - tag role on-call (alert Robusta; namespace moderation* → role Trust & Safety). Alert Grafana đã tự tag role nên không tag lại.
  - nút 🙋 Nhận xử lý / ↩️ Trả ticket / ✅ Đã xử lý; người nhận được ghi vào ticket + mở thread trên alert để trao đổi.
  - alert lặp lại khi ticket còn mở → không tạo ticket mới. Đã có người nhận: xóa tin lặp lại; chưa ai nhận: giữ
    tối đa 1 tin / REPEAT_EVERY_SECONDS (mặc định 2 phút), còn lại xóa. Số lần lặp ghi trên ticket.
  - tin Robusta mức INFO/DEBUG (vd "Update to pod …") → xóa, không tạo ticket.
  - ✅ Đã xử lý: alert có gửi RESOLVED (Grafana, Robusta qua Grafana) → ticket chuyển "🟢 Chờ xác nhận"; RESOLVED về
    thì đóng thật, quá VERIFY_SECONDS (mặc định 10 phút) chưa có RESOLVED → mở lại, tag người đã bấm.
    Alert Kubernetes của Robusta (crash/OOM, không bao giờ RESOLVED) → đóng ngay.
  - alert RESOLVED → ticket tự đóng.
Trạng thái ticket nằm ngay trong nội dung tin nhắn ticket, bot không cần DB; restart không mất gì.
"""
import asyncio
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass

import discord

log = logging.getLogger("alert-bot")

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
ROLE_ONCALL = os.environ.get("DISCORD_ROLE_ONCALL", "")
ROLE_TRUST_SAFETY = os.environ.get("DISCORD_ROLE_TRUST_SAFETY", "")
CHANNELS = {c.strip() for c in os.environ.get(
    "ALERT_CHANNELS", "alert-critical,alert-warning,alert-trust-safety").split(",") if c.strip()}
HISTORY = int(os.environ.get("TICKET_LOOKBACK", "200"))   # số tin gần nhất để tìm ticket đang mở
REPEAT_EVERY = int(os.environ.get("REPEAT_EVERY_SECONDS", "120"))
# Phải lớn hơn thời gian Grafana gửi RESOLVED: 1 chu kỳ đánh giá + group_interval (warning 5m, critical 2m).
VERIFY_SECONDS = int(os.environ.get("VERIFY_SECONDS", "600"))
RUNBOOK_BASE = os.environ.get("RUNBOOK_BASE", "https://runbooklive-e-commerce.vercel.app/")
# Alert builtin của Robusta không mang link runbook → gắn theo tiêu đề.
RUNBOOK_BY_TITLE = [(re.compile(r"crash", re.I), "lc-pod-crashloop"), (re.compile(r"oom", re.I), "lc-pod-oom"),
                    (re.compile(r"image.?pull|imagepull", re.I), "lc-container-waiting")]

STATUS_PREFIX = "Trạng thái:"
REPEAT_PREFIX = "Lặp lại:"
SOURCE_PREFIX = "Nguồn:"
SRC_ALERT = "alert (có RESOLVED)"
SRC_EVENT = "Kubernetes event (không có RESOLVED)"
OPEN = "🔴 Chưa có người nhận"
KEY_RE = re.compile(r"^Alert: `([^`]+)`", re.M)
CLAIMER_RE = re.compile(r"Đang xử lý: <@(\d+)>")
VERIFY_RE = re.compile(r"Chờ xác nhận: <@(\d+)>.*?<t:(\d+):R>")
USER_PINGS = discord.AllowedMentions(everyone=False, users=True, roles=False)
ROLE_PINGS = discord.AllowedMentions(everyone=False, users=False, roles=True)
NO_PINGS = discord.AllowedMentions.none()


@dataclass
class Alert:
    source: str      # robusta | grafana
    key: str         # tên alert, dùng để ghép firing ↔ resolved ↔ ticket
    firing: bool
    text: str        # toàn bộ chữ của alert (để chọn role)
    runbook: str = ""
    resolvable: bool = True   # nguồn có gửi RESOLVED không


NOISE = "noise"      # tin cần xóa: Robusta INFO/DEBUG


def alert_text(msg: discord.Message) -> str:
    parts = [msg.content]
    for e in msg.embeds:
        parts += [e.title or "", e.description or ""] + [f"{f.name} {f.value}" for f in e.fields]
    return "\n".join(parts)


def parse_alert(msg: discord.Message):
    if msg.author.name == "Robusta":
        # Header Robusta: "🔥 firing - 🔴 HIGH - **<title>**" / "✅ resolved - ... - **<title>**".
        # Tin chỉ có file đính kèm (log) không có header → bỏ qua.
        first = msg.content.splitlines()[0] if msg.content else ""
        m = re.search(r"\*\*(.+?)\*\*", first)
        if not m:
            return None
        if re.search(r" - .*\b(INFO|DEBUG) - ", first):   # vd "Update to pod ..." – chỉ là thông tin
            return NOISE
        return with_runbook(Alert("robusta", m.group(1).strip(), " resolved - " not in first, alert_text(msg)))
    title = msg.embeds[0].title if msg.embeds else None
    if not title:
        return None
    # Title Grafana (template lc.discord.title): "🔴 [CRITICAL] <alertname> ×N" / "✅ [RESOLVED] <alertname>".
    key = re.sub(r"\s×\d+$", "", re.sub(r"^.*?\]\s*", "", title)).strip()
    return with_runbook(Alert("grafana", key, "[RESOLVED]" not in title, alert_text(msg)))


def with_runbook(alert: Alert) -> Alert:
    m = re.search(r"\((%s[^)\s]*)\)" % re.escape(RUNBOOK_BASE), alert.text) or \
        re.search(r"(%s\S*#[\w-]+)" % re.escape(RUNBOOK_BASE), alert.text)
    if m:
        alert.runbook = m.group(1)
    else:
        # Robusta không kèm runbook = finding builtin từ Kubernetes event (crash/OOM…), không bao giờ có RESOLVED.
        alert.resolvable = alert.source != "robusta"
        alert.runbook = next((f"{RUNBOOK_BASE}#{uid}" for rx, uid in RUNBOOK_BY_TITLE if rx.search(alert.key)), "")
    return alert


def role_for(alert: Alert) -> str:
    if alert.source != "robusta":
        return ""
    role = ROLE_TRUST_SAFETY if "moderation" in alert.text and ROLE_TRUST_SAFETY else ROLE_ONCALL
    return f"<@&{role}> " if role else ""


def set_line(content: str, prefix: str, value: str) -> str:
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{prefix} {value}"
            return "\n".join(lines)
    return "\n".join(lines + [f"{prefix} {value}"])


def set_status(content: str, status: str) -> str:
    return set_line(content, STATUS_PREFIX, status)


def repeat_count(content: str) -> int:
    m = re.search(r"^%s (\d+)" % REPEAT_PREFIX, content, re.M)
    return int(m.group(1)) if m else 0


def is_open(content: str) -> bool:
    status = next((line for line in content.splitlines() if line.startswith(STATUS_PREFIX)), "")
    return "✅" not in status


def claimer_of(content: str):
    m = CLAIMER_RE.search(content)
    return int(m.group(1)) if m else None


def verifying(content: str):
    """(user id, thời điểm bấm Đã xử lý) nếu ticket đang chờ RESOLVED, ngược lại None."""
    m = VERIFY_RE.search(content)
    return (int(m.group(1)), int(m.group(2))) if m else None


def holder_of(content: str):
    v = verifying(content)
    return v[0] if v else claimer_of(content)


def resolvable(content: str) -> bool:
    return SRC_ALERT in content     # ticket cũ không có dòng "Nguồn:" → đóng ngay như trước


class TicketView(discord.ui.View):
    """Persistent view: custom_id cố định nên nút vẫn bấm được sau khi bot restart."""

    def __init__(self, state: str = "all"):
        super().__init__(timeout=None)
        if state == "open":
            self.remove_item(self.release)
        elif state == "claimed":
            self.remove_item(self.claim)

    @discord.ui.button(label="Nhận xử lý", emoji="🙋", style=discord.ButtonStyle.primary, custom_id="lc-ticket:claim")
    async def claim(self, interaction: discord.Interaction, _button):
        await bot.on_ticket_action(interaction, "claim")

    @discord.ui.button(label="Trả ticket", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="lc-ticket:release")
    async def release(self, interaction: discord.Interaction, _button):
        await bot.on_ticket_action(interaction, "release")

    @discord.ui.button(label="Đã xử lý", emoji="✅", style=discord.ButtonStyle.success, custom_id="lc-ticket:resolve")
    async def resolve(self, interaction: discord.Interaction, _button):
        await bot.on_ticket_action(interaction, "resolve")


class AlertBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True   # cần bật "Message Content Intent" trong Developer Portal
        super().__init__(intents=intents)
        self.locks = defaultdict(asyncio.Lock)
        self.last_shown = {}        # key alert → lần cuối một tin alert được giữ lại trong kênh
        self.deleted_at = {}        # channel id → lần cuối xóa tin Robusta (để xóa luôn tin file log đi kèm)

    async def setup_hook(self):
        self.add_view(TicketView())

    async def on_ready(self):
        log.info("logged in as %s, watching channels %s", self.user, sorted(CHANNELS))
        # Hẹn giờ kiểm tra nằm trong RAM: sau restart, dựng lại từ các ticket đang "Chờ xác nhận".
        for guild in self.guilds:
            for ch in guild.text_channels:
                if ch.name not in CHANNELS:
                    continue
                async for m in ch.history(limit=HISTORY):
                    v = m.author.id == self.user.id and verifying(m.content)
                    if v:
                        self.schedule_verify(m, v[1])

    def schedule_verify(self, ticket: discord.Message, since: int):
        async def check():
            await asyncio.sleep(max(0, since + VERIFY_SECONDS - time.time()))
            await self.verify_expired(ticket.channel, ticket.id, since)
        asyncio.create_task(check())

    async def verify_expired(self, channel, ticket_id: int, since: int):
        """Hết hạn chờ RESOLVED: nếu ticket vẫn chờ đúng lần bấm này → mở lại cho người đã bấm."""
        async with self.locks[ticket_id]:
            try:
                ticket = await channel.fetch_message(ticket_id)
            except discord.HTTPException:
                return
            v = verifying(ticket.content)
            if not v or v[1] != since:
                return
            uid, now, mins = v[0], int(time.time()), VERIFY_SECONDS // 60
            await ticket.edit(content=set_status(ticket.content, f"🟡 Đang xử lý: <@{uid}> (mở lại <t:{now}:R>, "
                                                                  f"⚠️ alert chưa hết)"),
                              view=TicketView("claimed"), allowed_mentions=NO_PINGS)
            note = (f"⚠️ <@{uid}> alert **chưa hết** sau {mins} phút kể từ khi bấm Đã xử lý "
                    f"→ ticket mở lại, vẫn giao cho bạn.")
            await ticket.reply(note, allowed_mentions=USER_PINGS, mention_author=False)
            _, thread = await self.thread_of(ticket)
            if thread:
                await safe(thread.edit(archived=False))
                await safe(thread.send(note, allowed_mentions=NO_PINGS))
            log.info("verify failed, reopened %s for %s", ticket.id, uid)

    async def find_open_ticket(self, channel, key):
        marker = f"Alert: `{key}`"
        async for m in channel.history(limit=HISTORY):
            if m.author.id == self.user.id and marker in m.content and is_open(m.content):
                return m
        return None

    async def on_message(self, msg: discord.Message):
        if msg.webhook_id is None or getattr(msg.channel, "name", None) not in CHANNELS:
            return
        alert = parse_alert(msg)
        if alert is NOISE:
            return await self.drop(msg)
        if alert is None:
            # Robusta gửi file log thành tin riêng ngay sau tin chính: tin chính bị xóa thì xóa luôn file.
            if msg.author.name == "Robusta" and time.time() - self.deleted_at.get(msg.channel.id, 0) < 15:
                await self.drop(msg)
            return
        alert.key = alert.key.replace("`", "'")[:200]
        async with self.locks[f"{msg.channel.id}:{alert.key}"]:
            ticket = await self.find_open_ticket(msg.channel, alert.key)
            if alert.firing:
                if ticket:
                    return await self.on_repeat(msg, alert, ticket)
                runbook = f"\n📕 Runbook: <{alert.runbook}>" if alert.runbook else ""
                source = f"\n{SOURCE_PREFIX} {SRC_ALERT if alert.resolvable else SRC_EVENT}"
                await msg.reply(
                    f"🎫 **Ticket** {role_for(alert)}\nAlert: `{alert.key}`{runbook}{source}\n{STATUS_PREFIX} {OPEN}",
                    view=TicketView("open"), allowed_mentions=ROLE_PINGS, mention_author=False)
                self.last_shown[alert.key] = time.time()
                log.info("ticket opened: %s", alert.key)
            elif ticket:
                who = holder_of(ticket.content)
                status = f"✅ Alert đã hết <t:{int(time.time())}:R>" + (f" · người xử lý <@{who}>" if who else "")
                await ticket.edit(content=set_status(ticket.content, status), view=None, allowed_mentions=NO_PINGS)
                await self.close_thread(ticket, "✅ Alert đã RESOLVED, ticket tự đóng.")
                log.info("ticket auto-closed: %s", alert.key)

    async def on_repeat(self, msg, alert, ticket):
        """Alert lặp lại khi ticket còn mở: đếm trên ticket; đã có người nhận → xóa, chưa → tối đa 1 tin / REPEAT_EVERY."""
        now = time.time()
        n = repeat_count(ticket.content) + 1
        await safe(ticket.edit(content=set_line(ticket.content, REPEAT_PREFIX, f"{n} lần (gần nhất <t:{int(now)}:R>)"),
                               allowed_mentions=NO_PINGS))
        last = self.last_shown.get(alert.key, ticket.created_at.timestamp())
        if holder_of(ticket.content) is None and now - last >= REPEAT_EVERY:
            self.last_shown[alert.key] = now
            await safe(msg.add_reaction("🔁"))
        else:
            await self.drop(msg)
        log.info("repeat #%d of %s (%s)", n, alert.key, "kept" if self.last_shown.get(alert.key) == now else "deleted")

    async def drop(self, msg):
        self.deleted_at[msg.channel.id] = time.time()
        await safe(msg.delete())

    async def on_ticket_action(self, interaction: discord.Interaction, action: str):
        async with self.locks[interaction.message.id]:
            ticket = await interaction.channel.fetch_message(interaction.message.id)   # đọc lại: tránh 2 người bấm cùng lúc
            user, now = interaction.user, int(time.time())
            claimer = claimer_of(ticket.content)
            if not is_open(ticket.content):
                return await interaction.response.send_message("Ticket này đã đóng.", ephemeral=True)

            if action == "claim":
                if claimer:
                    return await interaction.response.send_message(
                        f"Ticket đang do <@{claimer}> xử lý.", ephemeral=True, allowed_mentions=NO_PINGS)
                await interaction.response.edit_message(
                    content=set_status(ticket.content, f"🟡 Đang xử lý: <@{user.id}> (nhận <t:{now}:R>)"),
                    view=TicketView("claimed"), allowed_mentions=NO_PINGS)
                await self.open_thread(ticket, user)
            elif action == "release":
                if claimer != user.id and not interaction.permissions.manage_messages:
                    return await interaction.response.send_message(
                        "Chỉ người đang nhận ticket mới trả được.", ephemeral=True)
                await interaction.response.edit_message(
                    content=set_status(ticket.content, OPEN), view=TicketView("open"), allowed_mentions=NO_PINGS)
                await self.post_in_thread(ticket, f"↩️ <@{user.id}> trả ticket, cần người khác nhận.")
            elif action == "resolve" and resolvable(ticket.content):
                mins = VERIFY_SECONDS // 60
                await interaction.response.edit_message(
                    content=set_status(ticket.content, f"🟢 Chờ xác nhận: <@{user.id}> báo đã xử lý <t:{now}:R>, "
                                                       f"chờ alert RESOLVED (tối đa {mins} phút)"),
                    view=None, allowed_mentions=NO_PINGS)
                await self.post_in_thread(ticket, f"🟢 <@{user.id}> báo đã xử lý, đang chờ alert RESOLVED "
                                                  f"(quá {mins} phút chưa hết sẽ mở lại).")
                self.schedule_verify(ticket, now)
            elif action == "resolve":
                await interaction.response.edit_message(
                    content=set_status(ticket.content, f"✅ Đã xử lý bởi <@{user.id}> <t:{now}:R>"),
                    view=None, allowed_mentions=NO_PINGS)
                await self.close_thread(ticket, f"✅ <@{user.id}> đánh dấu đã xử lý.")
            log.info("%s %s by %s", action, ticket.id, user)

    async def alert_message(self, ticket: discord.Message):
        ref = ticket.reference
        if not ref or not ref.message_id:
            return None
        try:
            return ref.cached_message or await ticket.channel.fetch_message(ref.message_id)
        except discord.HTTPException:
            return None

    async def thread_of(self, ticket: discord.Message):
        alert = await self.alert_message(ticket)
        if not alert:
            return None, None
        thread = ticket.guild.get_thread(alert.id)   # thread tạo từ tin nhắn có id = id tin nhắn
        if thread is None:
            try:
                thread = await ticket.guild.fetch_channel(alert.id)
            except discord.HTTPException:
                thread = None
        return alert, thread

    async def open_thread(self, ticket: discord.Message, user):
        alert, thread = await self.thread_of(ticket)
        if not alert:
            return
        key = KEY_RE.search(ticket.content)
        try:
            if thread is None:
                thread = await alert.create_thread(name=f"🎫 {key.group(1) if key else 'ticket'}"[:95],
                                                   auto_archive_duration=1440)
            elif thread.archived:
                await thread.edit(archived=False)
            await thread.add_user(user)
            await thread.send(f"🙋 <@{user.id}> đã nhận xử lý ticket này.", allowed_mentions=NO_PINGS)
        except discord.HTTPException as e:
            log.warning("cannot open thread for %s: %s", ticket.id, e)
        await safe(alert.add_reaction("👀"))

    async def post_in_thread(self, ticket: discord.Message, text: str):
        _, thread = await self.thread_of(ticket)
        if thread:
            await safe(thread.send(text, allowed_mentions=NO_PINGS))

    async def close_thread(self, ticket: discord.Message, text: str):
        alert, thread = await self.thread_of(ticket)
        if alert:
            await safe(alert.add_reaction("✅"))
        if thread:
            await safe(thread.send(text, allowed_mentions=NO_PINGS))
            await safe(thread.edit(archived=True))


async def safe(coro):
    try:
        await coro
    except discord.HTTPException as e:
        log.warning("discord call failed: %s", e)


bot = AlertBot()

if __name__ == "__main__":
    discord.utils.setup_logging(level=logging.INFO)
    bot.run(TOKEN, log_handler=None)

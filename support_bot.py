"""
Rodas Friend Zone Bingo - SUPPORT BOT (separate bot, separate file)

How it works
  * A player writes to the support bot (text, photo, voice, file ... anything).
  * The bot forwards it to the admin(s) with the player's name and ID.
  * The admin REPLIES to that message in Telegram, and the bot sends the
    answer back to the player.

Environment variables
  SUPPORT_BOT_TOKEN   token of the NEW support bot (create it in @BotFather)
  SUPPORT_ADMIN_IDS   Telegram ID(s) of the support people, separated by commas
                      (if missing, ADMIN_ID is used)
  MAIN_BOT_USERNAME   optional, e.g. @RodasBingoBot  (shown in the welcome text)
  PORT                set automatically by Render (Web Service). Not needed for
                      a Background Worker.
"""
import asyncio, html, os, re, time
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (Message, BotCommand, BotCommandScopeChat)
print([k for k in os.environ if "TOKEN" in k.upper() or "BOT" in k.upper()])
TOKEN = os.environ["SUPPORT_BOT_TOKEN"]
_raw_admins = os.getenv("SUPPORT_ADMIN_IDS") or os.getenv("ADMIN_ID") or ""
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", _raw_admins.strip()) if x.isdigit()}
MAIN_BOT = os.getenv("MAIN_BOT_USERNAME", "")
PORT = int(os.getenv("PORT", "0"))

if not ADMIN_IDS:
    raise SystemExit("Set SUPPORT_ADMIN_IDS (or ADMIN_ID) to your Telegram ID.")

BRAND = "© 2026 Rodas Friend Zone Bingo"
FLOOD_LIMIT = 15          # most messages one player can send ...
FLOOD_WINDOW = 60         # ... in this many seconds
ACK_EVERY = 600           # "message received" reply at most once per 10 minutes
MAP_LIMIT = 5000          # how many relayed messages are remembered in memory

START_TEXT = (
    "🛟 Rodas Friend Zone Support\n\n"
    "ሰላም! ጥያቄዎን ወይም ችግርዎን እዚህ ይጻፉ። ድጋፍ ሰጪዎቻችን በተቻለ ፍጥነት ይመልሱልዎታል።\n"
    "ፎቶ ወይም የድምጽ መልዕክትም መላክ ይችላሉ።\n\n"
    "Hello! Write your question or problem here and our support team will "
    "reply as soon as possible. You can also send photos or voice messages.\n\n"
    + ("🎮 Game bot: " + MAIN_BOT + "\n\n" if MAIN_BOT else "")
    + BRAND)

ACK_TEXT = ("✅ መልዕክትዎ ደርሶናል። በቅርቡ እንመልሳለን።\n"
            "Message received. We will reply soon.")

dp = Dispatcher()

relay_map = {}            # (admin chat id, message id) -> player id
banned = set()            # blocked players (memory only: cleared when the bot restarts)
flood = {}                # player id -> list of recent message times
last_ack = {}             # player id -> time of the last "received" reply


def remember(chat_id, msg_id, uid):
    relay_map[(chat_id, msg_id)] = uid
    while len(relay_map) > MAP_LIMIT:                 # forget the oldest
        relay_map.pop(next(iter(relay_map)))


def flooding(uid):
    now = time.time()
    lst = [t for t in flood.get(uid, []) if now - t < FLOOD_WINDOW]
    lst.append(now)
    flood[uid] = lst
    return len(lst) > FLOOD_LIMIT


def target_uid(m: Message):
    """Which player is this admin message an answer to?"""
    r = m.reply_to_message
    if not r:
        return None
    uid = relay_map.get((m.chat.id, r.message_id))
    if uid:
        return uid
    # after a restart the memory is empty: read the ID from the header message
    mt = re.search(r"ID:\s*(\d{5,})", r.text or r.caption or "")
    return int(mt.group(1)) if mt else None


def who(uid, u=None):
    if u:
        return u.full_name + (f" (@{u.username})" if u.username else "")
    return str(uid)


# ---------- admin commands (registered first so they win over the relay) ----------
ADMIN_HELP = (
    "🛠 Support admin\n\n"
    "• Reply to any player's message to answer them (text, photo, voice, file).\n"
    "• /ban <id>  (or reply to a message with /ban) : block a player\n"
    "• /unban <id>  (or reply with /unban) : unblock\n"
    "• /help : this text\n\n"
    "Tip: always reply to the message that came from the bot with the player's name.")


@dp.message(CommandStart(), F.from_user.id.in_(ADMIN_IDS))
@dp.message(Command("help"), F.from_user.id.in_(ADMIN_IDS))
async def admin_help(m: Message):
    await m.answer(ADMIN_HELP)


@dp.message(Command("ban"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_ban(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    uid = int(arg) if arg.isdigit() else target_uid(m)
    if not uid:
        await m.answer("Send /ban <id>, or reply to a player's message with /ban")
        return
    if uid in ADMIN_IDS:
        await m.answer("You can't block an admin.")
        return
    banned.add(uid)
    await m.answer(f"🚫 {uid} is blocked. (This list is cleared when the bot restarts.)")


@dp.message(Command("unban"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_unban(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    uid = int(arg) if arg.isdigit() else target_uid(m)
    if not uid:
        await m.answer("Send /unban <id>, or reply to a player's message with /unban")
        return
    banned.discard(uid)
    await m.answer(f"✅ {uid} is unblocked.")


@dp.message(F.chat.type == "private", F.from_user.id.in_(ADMIN_IDS))
async def admin_message(m: Message):
    """Admin replies to a relayed message -> goes back to the player."""
    if m.text and m.text.startswith("/"):
        await m.answer("Unknown command. Send /help")
        return
    uid = target_uid(m)
    if not uid:
        await m.answer("↩️ To answer a player, REPLY to their message.\n"
                       "If you replied and see this, reply to the message that "
                       "shows the player's name and ID.")
        return
    try:
        await m.copy_to(uid)
        await m.answer("✅ Sent")
    except Exception as e:
        await m.answer(f"❌ Could not send (the player may have blocked the bot).\n{html.escape(repr(e))[:200]}")


# ---------- players ----------
@dp.message(CommandStart())
async def user_start(m: Message):
    await m.answer(START_TEXT)


@dp.message(F.chat.type == "private")
async def user_message(m: Message):
    u = m.from_user
    if u.id in banned:
        return                                         # silently ignored
    if flooding(u.id):
        if len([t for t in flood[u.id]]) == FLOOD_LIMIT + 1:
            await m.answer("⏳ እባክዎ ትንሽ ይጠብቁ። / Please slow down a little.")
        return
    head = (f"📩 <b>{html.escape(u.full_name)}</b>"
            + (f" (@{html.escape(u.username)})" if u.username else "")
            + f"\nID: <code>{u.id}</code>")
    sent = 0
    for aid in ADMIN_IDS:
        try:
            h = await m.bot.send_message(aid, head, parse_mode="HTML")
            c = await m.copy_to(aid, reply_to_message_id=h.message_id)
            remember(aid, h.message_id, u.id)
            remember(aid, c.message_id, u.id)
            sent += 1
        except Exception as e:
            print("could not reach admin", aid, repr(e))
    if not sent:
        await m.answer("⚠️ ድጋፍ ጊዜያዊ አይገኝም። እባክዎ ቆይተው ይሞክሩ።\n"
                       "Support is not available right now. Please try again later.")
        return
    now = time.time()
    if now - last_ack.get(u.id, 0) > ACK_EVERY:
        last_ack[u.id] = now
        await m.answer(ACK_TEXT)


# ---------- tiny web server (only so Render sees a "Web Service" as alive) ----------
async def health(_):
    return web.Response(text="support bot ok")


async def main():
    bot = Bot(TOKEN)
    if PORT:
        app = web.Application()
        app.add_routes([web.get("/", health)])
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await bot.set_my_description(
            "🛟 Rodas Friend Zone Support\nWrite your question and our team will reply.")
        await bot.set_my_short_description("© 2026 Rodas Friend Zone • Support 🛟")
    except Exception as e:
        print("could not set description:", repr(e))
    await bot.set_my_commands([BotCommand(command="start", description="Contact support")])
    for aid in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                [BotCommand(command="help", description="Admin help"),
                 BotCommand(command="ban", description="Block a player"),
                 BotCommand(command="unban", description="Unblock a player")],
                scope=BotCommandScopeChat(chat_id=aid))
        except Exception as e:
            print("could not set admin menu:", aid, repr(e))
    print("support bot running, admins:", sorted(ADMIN_IDS))
    await dp.start_polling(bot)


asyncio.run(main())

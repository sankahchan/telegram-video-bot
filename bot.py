"""
Restricted Telegram Media Downloader Bot (v5)
=============================================
- python-telegram-bot (Bot API HTTP polling) : user နဲ့ စကားပြော / update လက်ခံ
- Pyrogram user session (MTProto)            : restricted media download
- Pyrogram bot client (MTProto)             : ပြန်ပို့ (2GB အထိ ရတယ်)

Features:
- Batch download (တစ်ခါတည်း 10 ခုအထိ)
- မူရင်း caption အတိုင်း ပြန်ပို့
- photo / video / video_note / document / animation / audio / voice
- /mode : video အဖြစ်ပို့မလား / file အဖြစ်ပို့မလား

Env vars:
    API_ID, API_HASH   - my.telegram.org က ရတာ
    BOT_TOKEN          - @BotFather က ရတာ
    SESSION_STRING     - မရှိရင် session_string.txt ကနေ auto-ဖတ်မယ်
    ALLOWED_USER_IDS   - သုံးခွင့်ရှိတဲ့ Telegram user ID များ (comma နဲ့ခြား)
"""
import os
import re
import asyncio
import tempfile
import traceback

from dotenv import load_dotenv

load_dotenv()  # .env file ရှိရင် အဲဒီကနေ settings ဖတ်မယ်

from pyrogram import Client as PyroClient
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters as tg_filters,
)

API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()

if not SESSION_STRING:
    try:
        with open("session_string.txt") as f:
            SESSION_STRING = f.read().strip()
    except FileNotFoundError:
        pass

if not all([API_ID, API_HASH, BOT_TOKEN, SESSION_STRING]):
    raise SystemExit(
        "API_ID / API_HASH / BOT_TOKEN / SESSION_STRING လိုအပ်ပါတယ်.\n"
        "SESSION_STRING အတွက်: python3 generate_session.py ကို အရင် run ပါ."
    )

# Bot ကို ဒီ user ID တွေပဲ သုံးလို့ရမယ် (သူစိမ်းတွေ သုံးမရအောင်)
_raw_ids = os.environ.get("ALLOWED_USER_IDS", "").strip()
ALLOWED_IDS = {int(x) for x in _raw_ids.split(",") if x.strip()}
if not ALLOWED_IDS:
    ALLOWED_IDS = {1180438393}  # default: owner

MAX_BATCH = 10

# user_id -> "video" | "file"  (ပို့မယ့်ပုံစံ)
user_modes = {}


def get_mode(uid: int) -> str:
    return user_modes.get(uid, "video")


def mode_label(mode: str) -> str:
    return "🎬 Video (player နဲ့တိုက်ရိုက်ကြည့်)" if mode == "video" else "📦 File (ဖိုင်အတိုင်း)"


# User account — restricted media download အတွက်
user = PyroClient(
    "userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

# Bot (MTProto) — media ပြန်ပို့ဖို့ (file ကြီးတွေ ရတယ်)
bot_client = PyroClient(
    "dlbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

LINK_RE = re.compile(r"t\.me/(?:c/(\d+)|([A-Za-z0-9_]{5,}))/(\d+)(?:/(\d+))?")

WELCOME = (
    "👋 Restricted Media Downloader Bot မှ ကြိုဆိုပါတယ်!\n\n"
    "Download လုပ်ချင်တဲ့ message ရဲ့ link ကို ပို့ပေးပါ — "
    f"တစ်ခါတည်း {MAX_BATCH} ခုအထိ ပို့လို့ရပါတယ်.\n"
    "ဥပမာ:\n"
    "https://t.me/c/1234567890/123\n"
    "https://t.me/somechannel/123\n\n"
    "Video / photo / file အကုန်ရပါတယ်, မူရင်း caption အတိုင်း ပြန်ပို့ပေးမယ်.\n\n"
    "Commands:\n"
    "/mode — ပို့မယ့်ပုံစံ ပြောင်း (လက်ရှိ: {mode_label})\n"
    "  /mode video → player နဲ့ကြည့်\n"
    "  /mode file → ဖိုင်အတိုင်းသိမ်း\n\n"
    "⚠️ Login ဝင်ထားတဲ့ account က channel/group ရဲ့ member ဖြစ်နေရပါမယ်."
)


def allowed(update: Update) -> bool:
    return update.effective_user and update.effective_user.id in ALLOWED_IDS


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("⛔ ဒီ bot ကို သုံးခွင့်မရှိပါ။")
        return
    print(f"📩 /start from {update.effective_user.id}")
    await update.message.reply_text(
        WELCOME.format(mode_label=mode_label(get_mode(update.effective_user.id)))
    )


async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("⛔ ဒီ bot ကို သုံးခွင့်မရှိပါ။")
        return
    uid = update.effective_user.id
    arg = context.args[0].lower() if context.args else ""
    if arg in ("video", "file"):
        user_modes[uid] = arg
    elif arg == "":
        user_modes[uid] = "file" if get_mode(uid) == "video" else "video"
    else:
        await update.message.reply_text("အသုံးပြုပုံ: /mode video  သို့မဟုတ်  /mode file")
        return
    await update.message.reply_text(f"✅ ပို့မယ့်ပုံစံ: {mode_label(user_modes[uid])}")


async def fetch_message(chat_id, msg_id, _retried=False):
    """Message ကို တိုက်ရိုက်ရှာမယ်.

    မတွေ့ရင် linked chat (channel <-> discussion group) မှာပါ ထပ်ရှာမယ် —
    t.me/c/ link ထဲက ID က ဘယ် chat ရဲ့ ID လဲ မသေချာလို့ပါ.
    Returns: (message, used_chat_id, error_text)
    """
    tried = []
    candidates = [chat_id]
    try:
        chat = await user.get_chat(chat_id)
        linked = getattr(chat, "linked_chat", None)
        if linked and linked.id not in candidates:
            candidates.append(linked.id)
            print(f"🔗 linked chat တွေ့ပြီ: {linked.id}")
    except Exception as e:
        tried.append(f"get_chat({chat_id}): {type(e).__name__}: {e}")

    for cid in candidates:
        try:
            msg = await user.get_messages(cid, msg_id)
            if msg and not getattr(msg, "empty", False):
                print(f"✅ message တွေ့ပြီ: chat={cid} msg={msg_id}")
                return msg, cid, None
            tried.append(f"{cid}: message မရှိပါ (empty)")
        except Exception as e:
            tried.append(f"{cid}: {type(e).__name__}: {e}")
            traceback.print_exc()

    err = " | ".join(tried)

    # Session က ဒီ peer ကို မသိသေးရင် (Peer id invalid) dialogs sync လုပ်ပြီး တစ်ခါထပ်စမ်း
    if not _retried and "Peer id invalid" in err:
        print("🔄 Peer မသိသေးလို့ dialogs sync လုပ်နေပါတယ်...")
        try:
            async for _ in user.get_dialogs():
                pass
            print("✅ Dialogs sync ပြီးပါပြီ, ပြန်စမ်းနေပါတယ်...")
        except Exception as e:
            return None, None, err + f" | dialogs sync failed: {e}"
        return await fetch_message(chat_id, msg_id, _retried=True)

    return None, None, err


def build_caption(msg) -> str:
    cap = (msg.caption or "").strip()
    if len(cap) > 1000:
        cap = cap[:1000] + "…"
    return cap or "✅ Download ပြီးပါပြီ"


async def send_media(chat_id, path, msg, mode: str):
    """Download ပြီးသား file ကို mode အတိုင်း ပြန်ပို့မယ်."""
    caption = build_caption(msg)
    if mode == "file":
        await bot_client.send_document(chat_id, path, caption=caption)
        return
    if msg.photo:
        await bot_client.send_photo(chat_id, path, caption=caption)
    elif msg.video_note:
        await bot_client.send_video_note(chat_id, path)
    elif msg.video or msg.animation:
        await bot_client.send_video(chat_id, path, caption=caption)
    else:
        await bot_client.send_document(chat_id, path, caption=caption)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if not allowed(update):
        await update.message.reply_text("⛔ ဒီ bot ကို သုံးခွင့်မရှိပါ။")
        return

    # Message ထဲက link အားလုံး ထုတ် (batch)
    jobs = []
    for m in LINK_RE.finditer(update.message.text):
        private_id, username, msg_id, thread_msg_id = m.groups()
        mid = int(thread_msg_id or msg_id)
        cid = int(f"-100{private_id}") if private_id else username
        if (cid, mid) not in jobs:
            jobs.append((cid, mid))

    if not jobs:
        await update.message.reply_text(
            "❌ Link ပုံစံ မှားနေပါတယ်.\n"
            "ဥပမာ: https://t.me/c/1234567890/123"
        )
        return

    if len(jobs) > MAX_BATCH:
        await update.message.reply_text(
            f"⚠️ တစ်ခါတည်း အများဆုံး {MAX_BATCH} ခုပဲ ရပါမယ် — "
            f"ပထမ {MAX_BATCH} ခုကို လုပ်ပေးမယ်."
        )
        jobs = jobs[:MAX_BATCH]

    n = len(jobs)
    single = n == 1
    mode = get_mode(update.effective_user.id)
    loop = asyncio.get_running_loop()
    chat_id = update.effective_chat.id

    status = await update.message.reply_text(f"📥 {n} ခု တွေ့ပြီ — စတင်နေပါတယ်...")
    ok, fail = 0, 0

    def make_progress(tag):
        last = 0

        def cb(current, total):
            nonlocal last
            pct = int(current / total * 100) if total else 0
            if pct - last >= 10:
                last = pct
                fut = status.edit_text(f"{tag} ⬇️ {pct}%")
                asyncio.run_coroutine_threadsafe(fut, loop)

        return cb

    with tempfile.TemporaryDirectory() as tmpdir:
        for i, (cid, mid) in enumerate(jobs, 1):
            tag = f"📥 {i}/{n}" if not single else "📥"
            await status.edit_text(f"{tag} ရှာနေပါတယ်...")
            print(f"📩 link from {update.effective_user.id}: chat={cid} msg={mid}")

            msg, used_chat, err = await fetch_message(cid, mid)
            if not msg:
                fail += 1
                await update.message.reply_text(f"❌ {tag} မရပါ.\n{err}")
                continue

            media = (
                msg.photo or msg.video or msg.video_note or msg.document
                or msg.animation or msg.audio or msg.voice
            )
            if not media:
                fail += 1
                await update.message.reply_text(f"❌ {tag}: download လုပ်လို့ရတဲ့ media မရှိပါ.")
                continue

            try:
                path = await user.download_media(
                    msg, file_name=f"{tmpdir}/{i}_", progress=make_progress(tag)
                )
                await status.edit_text(f"{tag} 📤 ပို့နေပါတယ်...")
                await send_media(chat_id, path, msg, mode)
                ok += 1
                print(f"✅ ပို့ပြီးပါပြီ ({i}/{n}) -> {update.effective_user.id}")
            except Exception as e:
                traceback.print_exc()
                fail += 1
                await update.message.reply_text(
                    f"❌ {tag} download မအောင်မြင်ပါ: {type(e).__name__}: {e}"
                )

    if single and ok == 1:
        await status.delete()
    else:
        done = f"✅ ပြီးပါပြီ: {ok} ခု အောင်မြင်"
        if fail:
            done += f", {fail} ခု မအောင်"
        await status.edit_text(done)


async def post_init(application: Application):
    await user.start()
    await bot_client.start()
    me = await bot_client.get_me()
    print(f"✅ Bot @{me.username} အလုပ်လုပ်နေပါပြီ (HTTP polling)")
    # Fresh session က peer တွေ မသိသေးလို့ dialogs sync လုပ်မယ် (Peer id invalid မဖြစ်အောင်)
    print("📋 Dialogs sync လုပ်နေပါတယ်...")
    try:
        async for _ in user.get_dialogs():
            pass
        print("✅ Dialogs sync ပြီးပါပြီ")
    except Exception as e:
        print(f"⚠️ Dialogs sync မအောင်မြင်ပါ: {e}")


async def post_shutdown(application: Application):
    await bot_client.stop()
    await user.stop()


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(
        MessageHandler(
            tg_filters.ChatType.PRIVATE & tg_filters.TEXT & ~tg_filters.COMMAND,
            handle_link,
        )
    )
    print("📡 Polling စတင်နေပါပြီ...")
    app.run_polling()


if __name__ == "__main__":
    main()

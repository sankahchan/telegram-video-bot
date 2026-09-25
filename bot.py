"""
Restricted Telegram Media + Web Video Downloader Bot (v5.2)
============================================================
- python-telegram-bot (Bot API HTTP polling) : user နဲ့ စကားပြော / update လက်ခံ
- Pyrogram user session (MTProto)            : restricted Telegram media download
- Pyrogram bot client (MTProto)             : ပြန်ပို့ (2GB အထိ ရတယ်)
- yt-dlp                                    : YouTube / TikTok / Facebook / Instagram / X + ရာချီတဲ့ sites

Features:
 1. /stats     — download stats (တစ်နေ့/တစ်လ/စုစုပေါင်း)
 2. /mp3       — video ကနေ audio ထုတ်
 3. /adduser   — user ထပ်ထည့် (owner only)
 4. /watch     — channel post အသစ် auto-download
 5. progress   — download % အပြည့်အစုံ
 6. /quality   — high / low (compress) + link တိုင်း Low/High မေး
 7. /save      — Saved Messages ထဲ auto-save
 8. /nightmode — file ကြီးတွေ ညဘက် auto-download
 9. /zip       — batch ကို ZIP တစ်ဖိုင်တည်း
10. /trim      — video အပိုင်းဖြတ်
11. /find      — channel ထဲ media ရှာ
12. web links  — YouTube/TikTok/FB/IG/X + sites ရာချီ
13. /help <command> — command တစ်ခုချင်းစီ ရှင်းပြချက် + ဥပမာ

Env vars:
    API_ID, API_HASH   - my.telegram.org က ရတာ
    BOT_TOKEN          - @BotFather က ရတာ
    SESSION_STRING     - မရှိရင် session_string.txt ကနေ auto-ဖတ်မယ်
    ALLOWED_USER_IDS   - သုံးခွင့်ရှိတဲ့ Telegram user ID များ (comma နဲ့ခြား)
    DOWNLOAD_WORKERS   - parallel download connections (default 8)
    NIGHT_MIN_MB       - night queue threshold MB (default 100)
"""
import os
import re
import mimetypes
import asyncio
import tempfile
import traceback
import zipfile
import datetime
import time
import uuid

from dotenv import load_dotenv

load_dotenv()  # .env file ရှိရင် အဲဒီကနေ settings ဖတ်မယ်

from fast_download import fast_download  # noqa: E402
from store import StatsStore, UserStore, WatchStore, QueueStore, SettingsStore  # noqa: E402
from web_download import (  # noqa: E402
    extract_web_urls, download_web, probe_size,
    download_direct_file, looks_like_direct_file, direct_file_kind,
)
from media_tools import to_mp3, trim_video, compress_video, parse_trim_args, probe_video  # noqa: E402

from pyrogram import Client as PyroClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters as tg_filters,
)


def _env(name: str, default: str = "") -> str:
    # systemd EnvironmentFile doesn't strip quotes, so a value like
    # ALLOWED_USER_IDS="123" would arrive literally with quotes -> strip them.
    # Also strip Unicode "smart quotes" (Mac/iPhone autocorrect " -> " " etc.)
    return os.environ.get(name, default).strip().strip("\"'“”‘’").strip()


API_ID = int(re.sub(r"\D", "", _env("API_ID", "0")) or 0)
API_HASH = _env("API_HASH")
BOT_TOKEN = _env("BOT_TOKEN")
SESSION_STRING = _env("SESSION_STRING")

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

# Bot ကို ဒီ user ID တွေပဲ သုံးခွင့်ရှိမယ် (owner — env က ပထမ ID)
_raw_ids = _env("ALLOWED_USER_IDS")
_env_ids = [int(x) for x in re.findall(r"\d+", _raw_ids)]
ALLOWED_IDS = set(_env_ids) if _env_ids else {1180438393}
OWNER_ID = _env_ids[0] if _env_ids else 1180438393

MAX_BATCH = 10
DOWNLOAD_WORKERS = int(_env("DOWNLOAD_WORKERS", "8") or 8)
NIGHT_MIN_MB = float(_env("NIGHT_MIN_MB", "100") or 100)

# Persistent stores (JSON, restart လည်း မပျက်)
stats = StatsStore()
user_store = UserStore()
watches = WatchStore()
night_q = QueueStore()
settings = SettingsStore()

# In-memory pending states
pending_trim = {}      # uid -> (start_sec, end_sec)
pending_finds = {}     # uid -> [(chat_id, msg_id, label)]
pending_quality = {}   # uid -> {"token", "text", "trim", "ts"} (quality prompt)


# ---------------------------------------------------------------- auth
def allowed_uid(uid: int) -> bool:
    return uid in ALLOWED_IDS or uid in user_store.allowed_ids()


def allowed(update: Update) -> bool:
    return bool(update.effective_user) and allowed_uid(update.effective_user.id)


def is_owner(uid: int) -> bool:
    return uid == OWNER_ID


def st(uid: int) -> dict:
    return settings.get(uid)


def mode_label(mode: str) -> str:
    return "🎬 Video" if mode == "video" else "📦 File"


# ---------------------------------------------------------------- clients
user = PyroClient(
    "userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    max_concurrent_transmissions=DOWNLOAD_WORKERS,
)

bot_client = PyroClient(
    "dlbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

LINK_RE = re.compile(r"t\.me/(?:c/(\d+)|([A-Za-z0-9_]{5,}))/(\d+)(?:/(\d+))?")

WELCOME = (
    "👋 Downloader Bot မှ ကြိုဆိုပါတယ်!\n\n"
    "📌 **Telegram link** (restricted channel/group ရတာတွေအပါအဝင်) —\n"
    "📌 **Web link** (YouTube / TikTok / Facebook / Instagram / X / PDF / file) —\n"
    f"တစ်ခါတည်း {MAX_BATCH} ခုအထိ ပို့လို့ရပါတယ်.\n\n"
    "🎞️ Link ပို့တိုင်း Low / High quality ရွေးခိုင်းမယ် (ဒီတစ်ခါစာပဲ).\n\n"
    "Commands:\n"
    "/mode [video|file] — ပို့မယ့်ပုံစံ\n"
    "/quality [high|low] — low = compress (file သေး)\n"
    "/mp3 [on|off] — audio သက်သက် ထုတ်\n"
    "/zip [on|off] — batch ကို ZIP တစ်ဖိုင်တည်း\n"
    "/trim <အစ> <အဆုံး> — video ဖြတ် (ဥပမာ /trim 0:10 0:45)\n"
    "/find <channel> <စာသား> — channel ထဲ media ရှာ\n"
    "/watch <channel> — post အသစ် auto-download\n"
    "/unwatch /watchlist\n"
    "/nightmode [on|off] [နာရီ] — file ကြီးတွေ ညဘက်ဒေါင်း\n"
    "/save [on|off] — Saved Messages ထဲ auto-save\n"
    "/stats — download stats\n"
    "/adduser /deluser /users — (owner only)\n\n"
    "📖 အသေးစိတ်: /help <command>  (ဥပမာ /help quality)\n\n"
    "⚠️ Login ဝင်ထားတဲ့ account က channel/group ရဲ့ member ဖြစ်နေရပါမယ်."
)


# ---------------------------------------------------------------- help
HELP_OVERVIEW = (
    "📖 **Command များ**\n"
    "(အသေးစိတ်: /help <command> — ဥပမာ /help quality)\n\n"
    "/mode — ပို့မယ့်ပုံစံ (video/file)\n"
    "/quality — high/low (မူရင်း/compress)\n"
    "/mp3 — audio ထုတ် on/off\n"
    "/zip — batch ကို ZIP တစ်ဖိုင်တည်း\n"
    "/save — Saved Messages auto-save\n"
    "/nightmode — file ကြီး ညဘက်ဒေါင်း\n"
    "/trim — video အပိုင်းဖြတ်\n"
    "/find — channel ထဲ media ရှာ\n"
    "/watch — post အသစ် auto-download\n"
    "/unwatch /watchlist\n"
    "/stats — download stats\n"
    "/adduser /deluser /users — owner only"
)

HELP_TOPICS = {
    "mode": (
        "🎬 /mode — ပို့မယ့်ပုံစံ / Send mode\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /mode video  — video player ပုံစံနဲ့ ပို့ (default)\n"
        "  /mode file   — file/document ပုံစံနဲ့ ပို့\n"
        "  /mode        — နှစ်ခုကြား ပြောင်း (toggle)\n\n"
        "ဥပမာ / Example:\n"
        "  /mode file\n"
        "→ နောက်ဒေါင်းမယ့် video တွေ file အဖြစ် ရောက်မယ်"
    ),
    "quality": (
        "🎞️ /quality — Video quality\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /quality high  — မူရင်း resolution အတိုင်း (default)\n"
        "  /quality low   — 720p compress (file သေး, မြန်)\n"
        "  /quality       — နှစ်ခုကြား ပြောင်း (toggle)\n\n"
        "မှတ်ချက် / Note:\n"
        "• Link ပို့တိုင်း bot က Low/High မေးမယ် — ရွေးတာ ဒီတစ်ခါစာပဲ\n"
        "• /quality setting က watch/night auto-download တွေအတွက် default\n\n"
        "ဥပမာ / Example:\n"
        "  /quality low"
    ),
    "mp3": (
        "🎵 /mp3 — Video ကနေ audio ထုတ် / Extract audio\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /mp3 on   — ဒေါင်းသမျှ video ကို MP3 အဖြစ် ပို့\n"
        "  /mp3 off  — ပိတ် (default)\n"
        "  /mp3      — toggle\n\n"
        "ဥပမာ / Example:\n"
        "  /mp3 on\n"
        "→ link ပို့ရင် video အစား MP3 file ရမယ်"
    ),
    "zip": (
        "📦 /zip — Batch ကို ZIP တစ်ဖိုင်တည်း\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /zip on   — link အများကြီး ပို့ရင် ZIP တစ်ဖိုင်တည်း ပေါင်းပို့\n"
        "  /zip off  — တစ်ခုချင်း ပို့ (default)\n"
        "  /zip      — toggle\n\n"
        "ဥပမာ / Example:\n"
        "  /zip on\n"
        "→ link ၅ ခု ပို့ရင် downloads.zip တစ်ဖိုင်တည်း ရမယ်"
    ),
    "save": (
        "💾 /save — Saved Messages ထဲ auto-save\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /save on   — ပို့သမျှ Saved Messages ထဲလည်း မိတ္တူ သိမ်း\n"
        "  /save off  — မသိမ်း (default)\n"
        "  /save      — toggle\n\n"
        "ဥပမာ / Example:\n"
        "  /save on"
    ),
    "nightmode": (
        "🌙 /nightmode — File ကြီးတွေ ညဘက်မှ auto-download\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /nightmode on [နာရီ]  — ဖွင့် (နာရီ မပေးရင် 03:00 KST)\n"
        "  /nightmode off       — ပိတ် (queue ထဲကျန်တာ ချက်ချင်းဒေါင်း)\n"
        "  /nightmode           — လက်ရှိအခြေအနေ ကြည့်\n\n"
        "100MB+ file တွေ ညဘက် သတ်မှတ်နာရီမှ ဒေါင်းမယ်.\n\n"
        "ဥပမာ / Example:\n"
        "  /nightmode on 2\n"
        "→ မနက် ၂ နာရီ (KST) မှာ file ကြီးတွေ auto-download"
    ),
    "trim": (
        "✂️ /trim — Video အပိုင်းဖြတ် / Trim video\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /trim <အစ> <အဆုံး>\n"
        "  ပြီးမှ video link ပို့ — နောက် တစ်သုတ်စာပဲ သက်ရောက်မယ် (one-shot)\n\n"
        "အချိန် ပုံစံ / Time format:\n"
        "  စက္ကန့်: 90 | မိနစ်:စက္ကန့်: 1:30 | နာရီ:မိနစ်:စက္ကန့်: 01:02:03\n\n"
        "ဥပမာ / Example:\n"
        "  /trim 0:10 0:45\n"
        "→ video ရဲ့ 10s–45s အပိုင်းပဲ ဖြတ်ပို့မယ်"
    ),
    "find": (
        "🔍 /find — Channel ထဲ media ရှာ / Search channel media\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /find <channel> <ရှာမယ့်စာသား>\n\n"
        "ဥပမာ / Example:\n"
        "  /find @mychannel funny video\n"
        "→ တွေ့တာတွေ နံပါတ်နဲ့ ပြမယ်, နံပါတ် ပို့ရင် ဒေါင်းမယ်:\n"
        "  1"
    ),
    "watch": (
        "👁️ /watch — Post အသစ် auto-download\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /watch <channel link သို့မဟုတ် @username>\n\n"
        "5 မိနစ်တစ်ခါ စစ်ပြီး post အသစ်တင်တိုင်း auto-download ပို့ပေးမယ်.\n"
        "Quality က /quality setting အတိုင်း သုံးမယ်.\n\n"
        "ဥပမာ / Example:\n"
        "  /watch @mychannel\n"
        "→ စလုပ်ချိန်ကနေ နောက်ပိုင်း post အသစ်တွေ အလိုလို ရောက်မယ်"
    ),
    "unwatch": (
        "🚫 /unwatch — Watch ဖြုတ်\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /unwatch <channel>\n"
        "  /unwatch all  — အားလုံး ဖြုတ်\n\n"
        "ဥပမာ / Example:\n"
        "  /unwatch @mychannel"
    ),
    "watchlist": (
        "📋 /watchlist — Watch လုပ်ထားတဲ့ channel များ ကြည့်\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /watchlist"
    ),
    "stats": (
        "📊 /stats — Download stats\n\n"
        "ဒီနေ့ / ရက် ၃၀ / စုစုပေါင်း — အရေအတွက် နဲ့ MB ပြမယ်.\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /stats"
    ),
    "adduser": (
        "➕ /adduser — User ထပ်ထည့် (owner only)\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /adduser <Telegram ID>\n"
        "  /adduser @username\n\n"
        "ဥပမာ / Example:\n"
        "  /adduser 123456789"
    ),
    "deluser": (
        "➖ /deluser — User ဖြုတ် (owner only)\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /deluser <Telegram ID>\n\n"
        "ဥပမာ / Example:\n"
        "  /deluser 123456789"
    ),
    "users": (
        "👥 /users — သုံးခွင့်ရှိသူများ ကြည့် (owner only)\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /users"
    ),
}


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("⛔ ဒီ bot ကို သုံးခွင့်မရှိပါ။")
        return
    if context.args:
        name = context.args[0].lstrip("/").lower()
        topic = HELP_TOPICS.get(name)
        if topic:
            await update.message.reply_text(topic)
        else:
            await update.message.reply_text(
                f"❌ /{name} ဆိုတာ မရှိပါ.\n/help ပို့ပြီး command list ကြည့်ပါ."
            )
        return
    await update.message.reply_text(HELP_OVERVIEW)


# ---------------------------------------------------------------- helpers
def media_of(msg):
    return (
        msg.photo or msg.video or msg.video_note or msg.document
        or msg.animation or msg.audio or msg.voice
    )


def media_kind(msg) -> str:
    if msg.photo:
        return "photo"
    if msg.video_note:
        return "video_note"
    if msg.video or msg.animation:
        return "video"
    if msg.audio or msg.voice:
        return "audio"
    return "doc"


def media_size_mb(msg) -> float:
    m = media_of(msg)
    size = getattr(m, "file_size", 0) or 0
    return round(size / 1048576, 1)


def _safe_filename(name: str) -> str:
    name = (name or "").replace("/", "_").replace("\\", "_")
    name = "".join(c for c in name if c.isprintable()).strip().strip(".")
    return name[:120] or "file"


def original_filename(msg, kind: str, tag) -> str:
    """Download filename that keeps Telegram's original name/extension.

    Without this the file lands as e.g. '1_' (no extension) and Telegram
    shows it as generic 'data' that can't be opened (PDF case).
    """
    media = media_of(msg)
    raw = (getattr(media, "file_name", None) or "").strip()
    if raw:
        return f"{tag}_{_safe_filename(raw)}"
    mime = (getattr(media, "mime_type", None) or "").split(";")[0].strip()
    if mime == "audio/ogg":
        ext = ".ogg"  # voice messages; mimetypes would give .oga
    else:
        ext = mimetypes.guess_extension(mime) if mime else None
    base = {"video": "video", "audio": "audio", "voice": "voice",
            "photo": "photo", "video_note": "video_note",
            "animation": "animation", "doc": "document"}.get(kind, "file")
    return f"{tag}_{base}{ext or ''}"


def build_caption(text: str) -> str:
    text = (text or "").strip()
    if len(text) > 1000:
        text = text[:1000] + "…"
    return text or "✅ Download ပြီးပါပြီ"


def resolve_chat_ref(ref: str):
    """t.me link / @username / -100id -> chat_id or username for get_chat."""
    ref = ref.strip()
    m = re.search(r"t\.me/(?:c/(\d+)|([A-Za-z0-9_]{5,}))", ref)
    if m:
        if m.group(1):
            return int(f"-100{m.group(1)}")
        return m.group(2)
    if re.fullmatch(r"-?\d+", ref):
        return int(ref)
    return ref.lstrip("@")


async def _toggle_setting(update: Update, key: str, arg: str, name: str):
    uid = update.effective_user.id
    cur = st(uid)[key]
    if arg in ("on", "off"):
        val = arg == "on"
    elif arg == "":
        val = not cur
    else:
        await update.message.reply_text(f"အသုံးပြုပုံ: /{key} on  သို့မဟုတ်  /{key} off")
        return
    settings.set(uid, key, val)
    await update.message.reply_text(f"✅ {name}: {'ON 🟢' if val else 'OFF ⚪'}")


# ---------------------------------------------------------------- commands
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("⛔ ဒီ bot ကို သုံးခွင့်မရှိပါ။")
        return
    await update.message.reply_text(WELCOME)


async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("⛔ ဒီ bot ကို သုံးခွင့်မရှိပါ။")
        return
    uid = update.effective_user.id
    arg = context.args[0].lower() if context.args else ""
    if arg in ("video", "file"):
        settings.set(uid, "mode", arg)
    elif arg == "":
        settings.set(uid, "mode", "file" if st(uid)["mode"] == "video" else "video")
    else:
        await update.message.reply_text("အသုံးပြုပုံ: /mode video  သို့မဟုတ်  /mode file")
        return
    await update.message.reply_text(f"✅ ပို့မယ့်ပုံစံ: {mode_label(st(uid)['mode'])}")


async def quality_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    uid = update.effective_user.id
    arg = context.args[0].lower() if context.args else ""
    if arg in ("high", "low"):
        settings.set(uid, "quality", arg)
    elif arg == "":
        settings.set(uid, "quality", "low" if st(uid)["quality"] == "high" else "high")
    else:
        await update.message.reply_text("အသုံးပြုပုံ: /quality high  သို့မဟုတ်  /quality low")
        return
    q = st(uid)["quality"]
    await update.message.reply_text(
        f"✅ Quality: {'⬆️ High (မူရင်း)' if q == 'high' else '⬇️ Low (compress, file သေး)'}"
    )


async def mp3_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    arg = context.args[0].lower() if context.args else ""
    await _toggle_setting(update, "mp3", arg, "🎵 MP3 ထုတ်မယ်")


async def zip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    arg = context.args[0].lower() if context.args else ""
    await _toggle_setting(update, "zip", arg, "📦 ZIP ပေါင်း")


async def save_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    arg = context.args[0].lower() if context.args else ""
    await _toggle_setting(update, "save", arg, "💾 Saved Messages auto-save")


async def nightmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    uid = update.effective_user.id
    args = [a.lower() for a in context.args]
    if not args or args[0] not in ("on", "off"):
        s = st(uid)
        await update.message.reply_text(
            f"🌙 Night mode: {'ON 🟢' if s['night'] else 'OFF ⚪'}\n"
            f"⏰ အချိန်: {s['night_hour']:02d}:00 (KST)\n"
            f"📦 {NIGHT_MIN_MB:g}MB ထက်ကြီးတဲ့ file တွေ ညဘက်မှ ဒေါင်းမယ်.\n\n"
            "အသုံးပြုပုံ: /nightmode on 3  (မနက် ၃ နာရီ KST)"
        )
        return
    settings.set(uid, "night", args[0] == "on")
    if len(args) > 1 and args[1].isdigit():
        settings.set(uid, "night_hour", max(0, min(23, int(args[1]))))
    s = st(uid)
    await update.message.reply_text(
        f"✅ Night mode {'ON 🟢' if s['night'] else 'OFF ⚪'} — "
        f"{s['night_hour']:02d}:00 KST မှာ {NIGHT_MIN_MB:g}MB+ file တွေ ဒေါင်းမယ်."
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    s = stats.summary()
    await update.message.reply_text(
        "📊 **Download Stats**\n\n"
        f"📅 ဒီနေ့: {s['day'][0]} ခု, {s['day'][1]} MB\n"
        f"🗓️ ဒီလ (ရက် ၃၀): {s['month'][0]} ခု, {s['month'][1]} MB\n"
        f"♾️ စုစုပေါင်း: {s['all'][0]} ခု, {s['all'][1]} MB"
    )


async def adduser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("⛔ Owner ပဲ ဒီ command သုံးလို့ရပါတယ်.")
        return
    if not context.args:
        await update.message.reply_text("အသုံးပြုပုံ: /adduser <Telegram ID သို့မဟုတ် @username>")
        return
    ref = context.args[0]
    ids = re.findall(r"\d+", ref)
    if not ref.startswith("@") and not ids:
        await update.message.reply_text("❌ ဂဏန်း Telegram ID ထည့်ပါ — ဥပမာ: /adduser 123456789")
        return
    try:
        if ref.startswith("@"):
            u = await user.get_users(ref)
            new_id = u.id
        else:
            new_id = int(ids[0])
    except Exception as e:
        await update.message.reply_text(f"❌ User ရှာမရပါ: {e}")
        return
    if user_store.add(new_id):
        await update.message.reply_text(f"✅ User {new_id} ကို ထည့်ပြီးပါပြီ.")
    else:
        await update.message.reply_text("ℹ️ ဒီ user ရှိပြီးသားပါ.")


async def deluser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("⛔ Owner ပဲ ဒီ command သုံးလို့ရပါတယ်.")
        return
    if not context.args or not re.findall(r"\d+", context.args[0]):
        await update.message.reply_text("အသုံးပြုပုံ: /deluser <Telegram ID>")
        return
    del_id = int(re.findall(r"\d+", context.args[0])[0])
    if user_store.remove(del_id):
        await update.message.reply_text(f"✅ User {del_id} ကို ဖြုတ်ပြီးပါပြီ.")
    else:
        await update.message.reply_text("ℹ️ ဒီ user list ထဲမှာ မရှိပါ.")


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner ပဲ ဒီ command သုံးလို့ရပါတယ်.")
        return
    ids = sorted(ALLOWED_IDS | user_store.allowed_ids())
    lines = [f"• `{i}`" + (" (owner)" if i == OWNER_ID else "") for i in ids]
    await update.message.reply_text("👥 **သုံးခွင့်ရှိသူများ:**\n" + "\n".join(lines))


async def trim_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    uid = update.effective_user.id
    try:
        start, end = parse_trim_args(context.args)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    pending_trim[uid] = (start, end)
    await update.message.reply_text(
        f"✂️ OK — {start:g}s → {end:g}s ဖြတ်ပေးမယ်.\n"
        "Video link ပို့လိုက်ပါ (နောက် link တစ်သုတ်စာပဲ)."
    )


async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    uid = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text(
            "အသုံးပြုပုံ: /find <channel> <ရှာမယ့်စာသား>\n"
            "ဥပမာ: /find @somechannel funny video"
        )
        return
    chat_ref = resolve_chat_ref(context.args[0])
    query = " ".join(context.args[1:])
    status = await update.message.reply_text(f"🔍 '{query}' ရှာနေပါတယ်...")
    try:
        chat = await user.get_chat(chat_ref)
    except Exception as e:
        await status.edit_text(f"❌ Channel ရှာမရပါ: {type(e).__name__}")
        return
    results = []
    try:
        async for m in user.search_messages(chat.id, query, limit=15):
            if media_of(m):
                label = (m.caption or m.text or "").strip().split("\n")[0][:60]
                results.append((m.chat.id, m.id, media_kind(m), label))
            if len(results) >= 10:
                break
    except Exception as e:
        await status.edit_text(f"❌ ရှာမရပါ: {type(e).__name__}: {e}")
        return
    if not results:
        await status.edit_text("🔍 ဘာမှ မတွေ့ပါ.")
        return
    pending_finds[uid] = results
    lines = [f"{i}. [{kind}] {label or '(caption မရှိ)'}" for i, (_, _, kind, label) in enumerate(results, 1)]
    await status.edit_text(
        "🔍 **တွေ့တာတွေ** (နံပါတ်ပို့ပြီး ဒေါင်းပါ):\n\n" + "\n".join(lines)
    )


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("အသုံးပြုပုံ: /watch <channel link သို့မဟုတ် @username>")
        return
    chat_ref = resolve_chat_ref(context.args[0])
    try:
        chat = await user.get_chat(chat_ref)
    except Exception as e:
        await update.message.reply_text(f"❌ Channel ရှာမရပါ: {type(e).__name__}")
        return
    # အခုနောက်ဆုံး message ID ကနေ စမှတ် — အဟောင်းတွေ ပြန်မဒေါင်းအောင်
    last_id = 0
    try:
        async for m in user.get_chat_history(chat.id, limit=1):
            last_id = m.id
    except Exception:
        pass
    watches.add(uid, chat.id, chat.title or str(chat.id), last_id)
    await update.message.reply_text(
        f"👁️ **{chat.title or chat.id}** ကို watch လုပ်ပြီးပါပြီ.\n"
        "Post အသစ်တင်တိုင်း auto-download လုပ်ပို့ပေးမယ် (၅ မိနစ်တစ်ခါ စစ်မယ်)."
    )


async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("အသုံးပြုပုံ: /unwatch <channel>  သို့မဟုတ်  /unwatch all")
        return
    if context.args[0].lower() == "all":
        for cid in list(watches.list(uid).keys()):
            watches.remove(uid, int(cid))
        await update.message.reply_text("✅ Watch အားလုံး ဖြုတ်ပြီးပါပြီ.")
        return
    chat_ref = resolve_chat_ref(context.args[0])
    try:
        chat = await user.get_chat(chat_ref)
        cid = chat.id
    except Exception:
        await update.message.reply_text("❌ Channel ရှာမရပါ.")
        return
    if watches.remove(uid, cid):
        await update.message.reply_text("✅ Watch ဖြုတ်ပြီးပါပြီ.")
    else:
        await update.message.reply_text("ℹ️ ဒီ channel ကို watch မလုပ်ထားပါ.")


async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    uid = update.effective_user.id
    wl = watches.list(uid)
    if not wl:
        await update.message.reply_text("👁️ Watch လုပ်ထားတာ မရှိသေးပါ.\n/watch <channel> နဲ့ ထည့်ပါ.")
        return
    lines = [f"• {v['title']}" for v in wl.values()]
    await update.message.reply_text("👁️ **Watch list:**\n" + "\n".join(lines))


# ---------------------------------------------------------------- quality prompt
QUALITY_PROMPT_TTL = 600  # seconds


def _needs_quality_prompt(jobs) -> bool:
    """True when the batch may contain video (quality choice matters)."""
    for kind, ref in jobs:
        if kind == "tg":
            return True  # kind unknown until fetched — usually video
        if not looks_like_direct_file(ref):
            return True  # YouTube/TikTok/... -> video
        if direct_file_kind(ref) == "video":
            return True  # direct .mp4 etc.
    return False


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline button callbacks: quality choice for a pending download."""
    q = update.callback_query
    if not q or not q.data:
        return
    try:
        await q.answer()
    except Exception:
        pass
    if not allowed(update):
        return
    m = re.fullmatch(r"q:(low|high):([0-9a-f]+)", q.data or "")
    if not m:
        return
    choice, token = m.groups()
    uid = q.from_user.id
    pend = pending_quality.get(uid)
    if not pend or pend.get("token") != token:
        try:
            await q.edit_message_text("⏰ ဒီ ရွေးချယ်မှု မရှိတော့ပါ — link ပြန်ပို့ပေးပါ.")
        except Exception:
            pass
        return
    if time.time() - pend.get("ts", 0) > QUALITY_PROMPT_TTL:
        pending_quality.pop(uid, None)
        try:
            await q.edit_message_text("⏰ သက်တမ်းကုန်သွားပါပြီ — link ပြန်ပို့ပေးပါ.")
        except Exception:
            pass
        return
    pending_quality.pop(uid, None)
    if pend.get("trim"):
        pending_trim[uid] = pend["trim"]
    label = "⬆️ High (မူရင်း)" if choice == "high" else "⬇️ Low (မြန်/file သေး)"
    try:
        await q.edit_message_text(f"✅ {label} ရွေးပြီးပါပြီ — ဒေါင်းနေပါတယ်...")
    except Exception:
        pass
    await handle_link(update, context, text=pend["text"], quality=choice, prompt=False)


# ---------------------------------------------------------------- core flow
async def fetch_message(chat_id, msg_id, _retried=False):
    """Message ကို တိုက်ရိုက်ရှာမယ် (linked chat fallback + dialogs sync retry)."""
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
    if not _retried and "Peer id invalid" in err:
        print("🔄 Peer မသိသေးလို့ dialogs sync လုပ်နေပါတယ်...")
        try:
            async for _ in user.get_dialogs():
                pass
        except Exception as e:
            return None, None, err + f" | dialogs sync failed: {e}"
        return await fetch_message(chat_id, msg_id, _retried=True)
    return None, None, err


async def download_tg_media(msg, dest, progress):
    """Telegram media download (fast parallel -> fallback normal).

    Never delivers a truncated file silently: the final size is verified
    against the message's file_size, with retries before giving up.
    """
    media = media_of(msg)
    expected = getattr(media, "file_size", 0) or 0

    def _ok(path):
        return not expected or (os.path.exists(path) and os.path.getsize(path) == expected)

    try:
        path = await fast_download(user, msg, dest, workers=DOWNLOAD_WORKERS, progress=progress)
        if _ok(path):
            return path
        got = os.path.getsize(path) if os.path.exists(path) else 0
        print(f"⚠️ fast download size mismatch ({got} != {expected}) — normal download နဲ့ ပြန်စမ်းမယ်")
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"⚠️ fast download failed ({type(e).__name__}), fallback: {e}")

    last_err = None
    for attempt in range(3):
        try:
            path = await user.download_media(msg, file_name=dest, progress=progress)
            if not path:
                raise RuntimeError("download failed")
            if _ok(path):
                return path
            last_err = (f"incomplete: {os.path.getsize(path)} != {expected} bytes")
            print(f"⚠️ {last_err} — retrying ({attempt + 1}/3)")
            os.remove(path)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"⚠️ normal download failed ({last_err}) — retrying ({attempt + 1}/3)")
        await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"download မအောင်မြင်ပါ (3 ကြိမ် စမ်းပြီးပြီ): {last_err}")


async def post_process(path: str, kind: str, uid: int, tmpdir: str, idx: int,
                     use_trim: bool = True, quality: str = None):
    """trim -> mp3 -> compress. Returns (final_path, as_audio).

    quality: one-time override ("high"/"low"); falls back to saved setting.
    """
    s = st(uid)
    eff_quality = quality or s["quality"]
    cur = path
    as_audio = False
    # ffmpeg ops only make sense on real audio/video files (not PDF etc.)
    ext = os.path.splitext(path)[1].lower()
    is_media = ext in {
        ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts",
        ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac",
    }
    # 1. trim (one-shot, user-sent links only — not watch/night jobs)
    if use_trim and is_media and uid in pending_trim and kind in ("video", "audio", "doc"):
        start, end = pending_trim[uid]
        out = f"{tmpdir}/{idx}_trim.mp4"
        cur = await trim_video(cur, out, start, end)
    # 2. mp3 extract
    if is_media and s["mp3"] and kind in ("video", "audio", "doc"):
        out = f"{tmpdir}/{idx}_audio.mp3"
        cur = await to_mp3(cur, out)
        as_audio = True
    # 3. compress (quality low, video only, not already audio)
    elif is_media and eff_quality == "low" and kind == "video":
        out = f"{tmpdir}/{idx}_low.mp4"
        cur = await compress_video(cur, out)
    return cur, as_audio


async def deliver(uid: int, chat_id: int, path: str, caption: str,
                  kind: str, as_audio: bool, as_video: bool, log_kind: str = "tg"):
    """Bot ကနေ ပို့ + Saved Messages (optional) + stats."""
    s = st(uid)
    mode = s["mode"]
    caption = build_caption(caption)
    if as_audio or (kind == "audio" and mode == "video"):
        # MP3-extracted or Telegram audio/voice message -> proper audio bubble
        meta = await asyncio.to_thread(probe_video, path)
        await bot_client.send_audio(
            chat_id, path, caption=caption,
            duration=meta.get("duration", 0) or 0)
    elif kind == "photo" and mode == "video":
        await bot_client.send_photo(chat_id, path, caption=caption)
    elif kind == "video_note":
        await bot_client.send_video_note(chat_id, path)
    elif as_video and kind == "video" and mode == "video":
        # Pass real dimensions so Telegram shows the original aspect ratio
        # (w=0/h=0 makes clients render a square bubble).
        meta = await asyncio.to_thread(probe_video, path)
        await bot_client.send_video(
            chat_id, path, caption=caption,
            width=meta.get("width", 0) or 0,
            height=meta.get("height", 0) or 0,
            duration=meta.get("duration", 0) or 0)
    else:
        # documents (PDF/ZIP/...) and anything else -> plain file
        await bot_client.send_document(chat_id, path, caption=caption)
    if s["save"]:
        try:
            await user.send_document("me", path, caption=caption)
        except Exception as e:
            print(f"⚠️ Saved Messages ပို့မရပါ: {e}")
    try:
        stats.log(os.path.getsize(path) / 1048576, log_kind, uid)
    except Exception:
        pass


def _swallow(fut):
    try:
        fut.result()
    except Exception:
        pass


def make_tg_progress(status, tag, loop):
    last = [0]

    def cb(current, total):
        pct = int(current / total * 100) if total else 0
        if pct - last[0] >= 5:
            last[0] = pct
            fut = status.edit_text(f"{tag} ⬇️ {pct}%")
            f2 = asyncio.run_coroutine_threadsafe(fut, loop)
            f2.add_done_callback(_swallow)

    return cb


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      text: str = None, quality: str = None, prompt: bool = True):
    """Link handler.

    text: override message text (used by quality-prompt callback re-entry).
    quality: one-time "high"/"low" override for this batch (None = saved setting).
    prompt: ask Low/High via buttons before downloading (False for jobs/callbacks).
    """
    emsg = update.effective_message
    text = (text or "").strip()
    if not text and emsg and emsg.text:
        text = emsg.text.strip()
    if not text or emsg is None:
        return
    if not allowed(update):
        await emsg.reply_text("⛔ ဒီ bot ကို သုံးခွင့်မရှိပါ။")
        return
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # /find ရွေးချယ်မှု (နံပါတ်)
    if text.isdigit() and uid in pending_finds:
        results = pending_finds[uid]
        i = int(text)
        if 1 <= i <= len(results):
            cid, mid, kind, label = results[i - 1]
            del pending_finds[uid]
            await emsg.reply_text(f"📥 '{label or kind}' ဒေါင်းနေပါတယ်...")
            with tempfile.TemporaryDirectory() as tmpdir:
                loop = asyncio.get_running_loop()
                status = await emsg.reply_text("📥 ရှာနေပါတယ်...")
                msg, _, err = await fetch_message(cid, mid)
                if not msg or not media_of(msg):
                    await status.edit_text(f"❌ မရပါ: {err or 'media မရှိ'}")
                    return
                try:
                    path = await download_tg_media(
                        msg, os.path.join(tmpdir, original_filename(msg, media_kind(msg), "find")),
                        make_tg_progress(status, "📥", loop))
                    final, as_audio = await post_process(path, media_kind(msg), uid, tmpdir, 0)
                    await status.edit_text("📤 ပို့နေပါတယ်...")
                    await deliver(uid, chat_id, final, msg.caption, media_kind(msg), as_audio, media_kind(msg) == "video")
                    await status.delete()
                except Exception as e:
                    traceback.print_exc()
                    await status.edit_text(f"❌ မအောင်မြင်ပါ: {type(e).__name__}: {e}")
        else:
            await emsg.reply_text("❌ နံပါတ် မှားနေပါတယ်.")
        return

    # Telegram links
    tg_jobs = []
    for m in LINK_RE.finditer(text):
        private_id, username, msg_id, thread_msg_id = m.groups()
        mid = int(thread_msg_id or msg_id)
        cid = int(f"-100{private_id}") if private_id else username
        if (cid, mid) not in tg_jobs:
            tg_jobs.append((cid, mid))

    # Web video links
    web_urls = extract_web_urls(text)

    if not tg_jobs and not web_urls:
        await emsg.reply_text(
            "❌ Link ပုံစံ မှားနေပါတယ်.\n"
            "Telegram: https://t.me/c/1234567890/123\n"
            "Web: YouTube / TikTok / Facebook / Instagram / X link"
        )
        return

    jobs = [("tg", j) for j in tg_jobs] + [("web", u) for u in web_urls]
    if len(jobs) > MAX_BATCH:
        await emsg.reply_text(
            f"⚠️ တစ်ခါတည်း အများဆုံး {MAX_BATCH} ခုပဲ — ပထမ {MAX_BATCH} ခု လုပ်ပေးမယ်."
        )
        jobs = jobs[:MAX_BATCH]

    # Quality prompt — link ပို့တိုင်း Low/High မေး (ဒီတစ်ခါစာပဲ)
    if prompt and _needs_quality_prompt(jobs):
        token = uuid.uuid4().hex[:8]
        prev = pending_quality.get(uid)
        pending_quality[uid] = {
            "token": token,
            "text": text,
            # /trim one-shot ကို prompt ကျော်ပြီး ထိန်း (အဟောင်း prompt ရှိရင်လည်း မပျောက်)
            "trim": pending_trim.pop(uid, None) or (prev.get("trim") if prev else None),
            "ts": time.time(),
        }
        kb = [[
            InlineKeyboardButton("⬇️ Low (မြန်/file သေး)",
                                 callback_data=f"q:low:{token}"),
            InlineKeyboardButton("⬆️ High (မူရင်း)",
                                 callback_data=f"q:high:{token}"),
        ]]
        await emsg.reply_text(
            "🎞️ Quality ရွေးပါ (ဒီတစ်ခါစာပဲ):\n"
            "🤖 watch/night auto-download တွေက /quality setting အတိုင်း သုံးမယ်.",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    n = len(jobs)
    single = n == 1
    s = st(uid)
    loop = asyncio.get_running_loop()
    status = await emsg.reply_text(f"📥 {n} ခု တွေ့ပြီ — စတင်နေပါတယ်...")
    ok, fail, queued = 0, 0, 0
    collected = []  # zip mode

    async def web_progress(tag, pct):
        try:
            await status.edit_text(f"{tag} ⬇️ {pct}%")
        except Exception:
            pass

    with tempfile.TemporaryDirectory() as tmpdir:
        idx = 0
        for kind, ref in jobs:
            idx += 1
            tag = f"📥 {idx}/{n}" if not single else "📥"
            try:
                if kind == "tg":
                    cid, mid = ref
                    await status.edit_text(f"{tag} ရှာနေပါတယ်...")
                    print(f"📩 tg link from {uid}: chat={cid} msg={mid}")
                    msg, _, err = await fetch_message(cid, mid)
                    if not msg:
                        fail += 1
                        await emsg.reply_text(f"❌ {tag} မရပါ.\n{err}")
                        continue
                    if not media_of(msg):
                        fail += 1
                        await emsg.reply_text(f"❌ {tag}: media မရှိပါ.")
                        continue
                    # night queue?
                    if s["night"] and media_size_mb(msg) >= NIGHT_MIN_MB:
                        night_q.add({"kind": "tg", "ref": {"chat": cid, "msg": mid},
                                     "user_id": uid, "chat_id": chat_id,
                                     "ts": datetime.datetime.now().isoformat(),
                                     "label": f"t.me msg {mid}",
                                     "quality": quality or s["quality"]})
                        queued += 1
                        continue
                    path = await download_tg_media(
                        msg, os.path.join(tmpdir, original_filename(msg, media_kind(msg), idx)),
                        make_tg_progress(status, tag, loop))
                    final, as_audio = await post_process(
                        path, media_kind(msg), uid, tmpdir, idx, quality=quality)
                    if s["zip"]:
                        collected.append((final, build_caption(msg.caption),
                                          media_kind(msg), as_audio))
                    else:
                        await status.edit_text(f"{tag} 📤 ပို့နေပါတယ်...")
                        await deliver(uid, chat_id, final, msg.caption,
                                      media_kind(msg), as_audio, media_kind(msg) == "video")
                    ok += 1
                    print(f"✅ tg ပို့ပြီးပါပြီ ({idx}/{n}) -> {uid}")
                else:
                    url = ref
                    await status.edit_text(f"{tag} 🌐 ဒေါင်းနေပါတယ်...")
                    print(f"📩 web link from {uid}: {url}")
                    # night queue? (probe size first)
                    if s["night"]:
                        mb = await probe_size(url)
                        if mb is None or mb >= NIGHT_MIN_MB:
                            night_q.add({"kind": "web", "ref": {"url": url},
                                         "user_id": uid, "chat_id": chat_id,
                                         "ts": datetime.datetime.now().isoformat(),
                                         "label": url[:80],
                                         "quality": quality or s["quality"]})
                            queued += 1
                            continue
                    # PDF / direct file -> plain HTTP download; else yt-dlp
                    if looks_like_direct_file(url):
                        path, title = await download_direct_file(
                            url, tmpdir, progress_cb=web_progress, loop=loop, tag=tag)
                        wk = direct_file_kind(url)
                    else:
                        try:
                            path, title = await download_web(
                                url, tmpdir, quality=quality or s["quality"],
                                progress_cb=web_progress, loop=loop, tag=tag)
                            wk = "video"
                        except Exception as e:
                            if "Unsupported URL" in str(e) or "Unsupported" in type(e).__name__:
                                path, title = await download_direct_file(
                                    url, tmpdir, progress_cb=web_progress, loop=loop, tag=tag)
                                wk = direct_file_kind(url)
                            else:
                                raise
                    final, as_audio = await post_process(
                        path, wk, uid, tmpdir, idx, quality=quality)
                    if s["zip"]:
                        collected.append((final, title, wk, as_audio))
                    else:
                        await status.edit_text(f"{tag} 📤 ပို့နေပါတယ်...")
                        await deliver(uid, chat_id, final, title, wk, as_audio,
                                      wk == "video", log_kind="web")
                    ok += 1
                    print(f"✅ web ပို့ပြီးပါပြီ ({idx}/{n}) -> {uid}")
            except Exception as e:
                traceback.print_exc()
                fail += 1
                err = str(e)[:300]
                await emsg.reply_text(f"❌ {tag} မအောင်မြင်ပါ: {type(e).__name__}: {err}")

        # zip mode: everything into one archive
        if collected and s["zip"]:
            try:
                zpath = os.path.join(tmpdir, "downloads.zip")
                with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                    for p, _, _, _ in collected:
                        z.write(p, os.path.basename(p))
                await status.edit_text("📦 ZIP ပေါင်းနေပါတယ်...")
                await bot_client.send_document(
                    chat_id, zpath,
                    caption=f"📦 {len(collected)} files")
                if s["save"]:
                    try:
                        await user.send_document("me", zpath, caption=f"📦 {len(collected)} files")
                    except Exception as e:
                        print(f"⚠️ Saved Messages ပို့မရပါ: {e}")
                stats.log(os.path.getsize(zpath) / 1048576, "zip", uid)
            except Exception as e:
                traceback.print_exc()
                fail += len(collected)
                await emsg.reply_text(f"❌ ZIP မအောင်မြင်ပါ: {e}")

    # trim one-shot ပဲ — သုံးပြီးရင် ရှင်း
    pending_trim.pop(uid, None)

    if single and ok == 1 and not queued:
        await status.delete()
    else:
        done = f"✅ ပြီးပါပြီ: {ok} ခု အောင်မြင်"
        if fail:
            done += f", {fail} ခု မအောင်"
        if queued:
            done += f", 🌙 {queued} ခု ညဘက်စောင့်မယ်"
        await status.edit_text(done)


# ---------------------------------------------------------------- background jobs
async def watch_job(context: ContextTypes.DEFAULT_TYPE):
    """5 မိနစ်တစ်ခါ: watch လုပ်ထားတဲ့ channel တွေမှာ post အသစ် စစ်မယ်."""
    for uid, chats in watches.all().items():
        if not allowed_uid(uid):
            continue
        for cid, w in chats.items():
            try:
                max_id = w.get("last_id", 0)
                fresh = []
                async for m in user.get_chat_history(cid, limit=15):
                    if m.id <= max_id:
                        break
                    if media_of(m) and not getattr(m, "empty", False):
                        fresh.append(m)
                if not fresh:
                    continue
                fresh.sort(key=lambda m: m.id)
                with tempfile.TemporaryDirectory() as tmpdir:
                    for m in fresh:
                        try:
                            path = await download_tg_media(
                                m, os.path.join(tmpdir, original_filename(m, media_kind(m), "w")), None)
                            final, as_audio = await post_process(
                                path, media_kind(m), uid, tmpdir, m.id, use_trim=False)
                            await deliver(uid, uid, final,
                                          f"👁️ {w.get('title','')}\n{(m.caption or '')}",
                                          media_kind(m), as_audio, media_kind(m) == "video")
                            print(f"👁️ watch: {w.get('title')} msg {m.id} -> {uid}")
                        except Exception as e:
                            print(f"⚠️ watch download failed: {e}")
                watches.set_last(uid, cid, max(m.id for m in fresh))
            except Exception as e:
                print(f"⚠️ watch check failed {cid}: {type(e).__name__}: {e}")


async def night_job(context: ContextTypes.DEFAULT_TYPE):
    """1 နာရီတစ်ခါ: night_hour (KST) ရောက်ရင် queue ထဲက file ကြီးတွေ ဒေါင်းမယ်.

    night mode ပိတ်ထားရင်တော့ queue ထဲကျန်တာ အခုချက်ချင်း ဒေါင်းမယ်
    (ပိတ်လိုက်တာနဲ့ စောင့်စရာမလိုတော့ဘူးလို့ ယူဆတယ်).
    """
    kst_hour = (datetime.datetime.now(datetime.timezone.utc).hour + 9) % 24
    items = night_q.all()
    if not items:
        return
    by_user = {}
    for it in items:
        by_user.setdefault(it.get("user_id"), []).append(it)
    remaining = []
    for uid, ulist in by_user.items():
        s = settings.get(uid)
        if s["night"] and s["night_hour"] != kst_hour:
            remaining.extend(ulist)  # အချိန်မကျသေးဘူး — ဆက်စောင့်
            continue
        print(f"🌙 night queue processing for {uid}: {len(ulist)} items")
        with tempfile.TemporaryDirectory() as tmpdir:
            for it in ulist:
                try:
                    kind, ref = it["kind"], it["ref"]
                    nq = it.get("quality") or s["quality"]
                    if kind == "tg":
                        msg, _, err = await fetch_message(ref["chat"], ref["msg"])
                        if not msg or not media_of(msg):
                            continue
                        path = await download_tg_media(
                            msg, os.path.join(tmpdir, original_filename(msg, media_kind(msg), "n")), None)
                        final, as_audio = await post_process(
                            path, media_kind(msg), uid, tmpdir, 0,
                            use_trim=False, quality=nq)
                        await deliver(uid, it["chat_id"], final, msg.caption,
                                      media_kind(msg), as_audio, media_kind(msg) == "video")
                    else:
                        url = ref["url"]
                        if looks_like_direct_file(url):
                            path, title = await download_direct_file(url, tmpdir)
                            wk = direct_file_kind(url)
                        else:
                            path, title = await download_web(
                                url, tmpdir, quality=nq)
                            wk = "video"
                        final, as_audio = await post_process(
                            path, wk, uid, tmpdir, 0, use_trim=False, quality=nq)
                        await deliver(uid, it["chat_id"], final, title,
                                      wk, as_audio, wk == "video", log_kind="web")
                    try:
                        await bot_client.send_message(
                            it["chat_id"], f"🌙 ညဘက် download ပြီးပါပြီ: {it.get('label','')}")
                    except Exception:
                        pass
                except Exception as e:
                    print(f"⚠️ night queue item failed: {e}")
                    remaining.append(it)
    night_q.replace(remaining)


# ---------------------------------------------------------------- lifecycle
async def post_init(application: Application):
    await user.start()
    await bot_client.start()
    me = await bot_client.get_me()
    print(f"✅ Bot @{me.username} အလုပ်လုပ်နေပါပြီ (HTTP polling)")
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
    for cmd, fn in [
        ("start", start_cmd), ("help", help_cmd),
        ("mode", mode_cmd), ("quality", quality_cmd),
        ("mp3", mp3_cmd), ("zip", zip_cmd), ("save", save_cmd),
        ("nightmode", nightmode_cmd), ("stats", stats_cmd),
        ("adduser", adduser_cmd), ("deluser", deluser_cmd), ("users", users_cmd),
        ("trim", trim_cmd), ("find", find_cmd),
        ("watch", watch_cmd), ("unwatch", unwatch_cmd), ("watchlist", watchlist_cmd),
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^q:(low|high):"))
    app.add_handler(
        MessageHandler(
            tg_filters.ChatType.PRIVATE & tg_filters.TEXT & ~tg_filters.COMMAND,
            handle_link,
        )
    )
    jq = app.job_queue
    if jq is None:
        print("⚠️ job-queue မရှိပါ — watch/night mode အလုပ်မလုပ်ပါ "
              "(pip install 'python-telegram-bot[job-queue]')")
    else:
        jq.run_repeating(watch_job, interval=300, first=90)
        jq.run_repeating(night_job, interval=3600, first=120)
        print("⏰ background jobs: watch (5min), night queue (1h)")
    print("📡 Polling စတင်နေပါပြီ...")
    app.run_polling()


if __name__ == "__main__":
    main()

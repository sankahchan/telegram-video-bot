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
import hashlib
import shutil
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
    pot_server_hint, storyboard_only, yt_pipeline_status,
    _diagnose_formats, web_info,
)
from media_tools import to_mp3, trim_video, compress_video, parse_trim_args, probe_video, ios_remux  # noqa: E402
from filecache import FileIdCache, make_key  # noqa: E402
from x_media import fetch_x_timeline, parse_timeline_args  # noqa: E402
from torrent_download import (  # noqa: E402
    is_magnet, extract_magnets, have_aria2, fetch_magnet_metadata,
    torrent_files, pick_targets, download_torrent, check_torrent_size,
    TorrentError, MAX_TORRENT_FILE_MB,
)
from follow import (  # noqa: E402
    FollowStore, fetch_items, new_items, is_torrent_link, MAX_ATTEMPTS,
    tvmaze_search, tvmaze_show, fetch_eztv_items, select_releases,
    apibay_search, fmt_size,
)
from subs import (search_movies, movie_subtitles, movie_languages,
                      download_subtitle, LANG_ALIASES)  # noqa: E402
import gdrive  # noqa: E402  (google libs imported lazily inside)

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
fcache = FileIdCache()  # URL -> Telegram file_id (instant repeat delivery)
follows = FollowStore()  # torrent RSS auto-follow
bookmarks = BookmarkStore()  # per-user saved links

# In-memory pending states
pending_trim = {}      # uid -> (start_sec, end_sec)
pending_finds = {}     # uid -> [(chat_id, msg_id, label)]
pending_quality = {}   # uid -> {"token", "text", "trim", "ts"} (quality prompt)
pending_history = {}   # uid -> [stats entries] (for /history resend)
pending_sublangs = {}  # uid -> {imdb, langs[]} (/subs language step)
pending_sub_lang = {}  # uid -> preferred lang from '/subs x mm'
pending_drive = {}     # token -> {"uid","chat_id","source","is_magnet","tname","size","tdata","ts"}


# ---------------------------------------------------------------- auth
def allowed_uid(uid: int) -> bool:
    if uid in ALLOWED_IDS:
        return True
    if uid not in user_store.allowed_ids():
        return False
    return not user_store.is_expired(uid)


def quota_allows(uid: int):
    """(ok, used_mb, quota_mb|None) — rolling 30-day download quota."""
    q = user_store.quota_mb(uid)
    if not q:
        return True, 0.0, None
    used = stats.usage(uid, days=30)
    return used < q, used, q


def quota_block_msg(used: float, quota: float) -> str:
    return (f"📊 Quota ကုန်သွားပါပြီ ({used:.0f}/{quota:.0f} MB, ရက် 30 အတွင်း) — "
            "ဆက်သုံးချင်ရင် owner ကို ဆက်သွယ်ပါ.")


def _md_esc(t: str) -> str:
    """Telegram legacy-Markdown escape for user/URL-derived text.

    URLs နဲ့ feed နာမည်တွေမှာ `_` `*` `[` `]` ပါရင် Telegram က
    "can't parse entities" error တက်တယ် — အဲ့ဒါကြောင့် escape လုပ်တယ်.
    """
    return re.sub(r"([_*\[\]`])", r"\\\1", t or "")


def _tg_src_url(cid, mid) -> str:
    """Rebuild a t.me link from (chat_id, msg_id) — for /history resend."""
    s = str(cid)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{mid}"
    return f"https://t.me/{cid}/{mid}"


_expired_notice = {}  # uid -> day string (notify at most once/day)


def allowed(update: Update) -> bool:
    u = update.effective_user
    if not u:
        return False
    uid = u.id
    if uid in ALLOWED_IDS or uid in user_store.allowed_ids():
        if user_store.is_expired(uid):
            day = time.strftime("%Y-%m-%d")
            if _expired_notice.get(uid) != day:
                _expired_notice[uid] = day
                try:
                    update.effective_message.reply_text(
                        "⏰ **သက်တမ်း ကုန်သွားပါပြီ** / Subscription expired.\n"
                        "ဆက်သုံးချင်ရင် owner ကို ဆက်သွယ်ပါ.",
                        parse_mode="Markdown")
                except Exception:
                    pass
            return False
        return True
    return False


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
    "/follow <rss-url> [name] — series episode အသစ် auto-download\n"
    "/unfollow /follows\n"
    "/tv <series name> — bot ထဲကနေ series ရှာပြီး follow လုပ်\n"
    "/history — ဒေါင်းခဲ့တာတွေ ပြန်ပို့\n"
    "/info <link> — မဒေါင်းခင် info ကြိုကြည့်\n"
    "/bookmark <link> — link သိမ်း\n"
    "/menu — 🎛️ ခလုတ်တွေနဲ့ သုံး (အလွယ်ဆုံး)\n"
    "/search <text> — torrent အကုန် ရှာ (movie/music/series/software)\n"
    "/subs <movie> — subtitle (.srt) ရှာ\n"
    "/drivestatus — Google Drive upload status\n"
    "/nightmode [on|off] [နာရီ] — file ကြီးတွေ ညဘက်ဒေါင်း\n"
    "/save [on|off] — Saved Messages ထဲ auto-save\n"
    "/stats — download stats\n"
    "/adduser /deluser /users — (owner only)\n"
    "/extend <id> <လ> — user သက်တမ်း တိုး (owner only)\n"
    "/admin — 👑 admin panel (owner only)\n"
    "/xtimeline @user [n] — X profile ရဲ့ latest video တွေ\n"
    "/clearcache — file_id cache ရှင်း (owner only)\n\n"
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
    "/follow — series RSS, episode အသစ် auto-download\n"
    "/unfollow /follows — ⬇️ auto ↔ 🔔 notify-only ပြောင်းလို့ရ\n"
    "/tv — bot ထဲကနေ series ရှာ + follow (website မလို)\n"
    "/menu — 🎛️ ခလုတ်တွေနဲ့ သုံး\n"
    "/search — torrent အကုန် ရှာ + ဒေါင်း\n"
    "/subs — subtitle (.srt) ရှာ (ဘာသာစကား ရွေး)\n"
    "/history — ဒေါင်းခဲ့တာတွေ, နှိပ်တာနဲ့ ပြန်ပို့\n"
    "/info — link info ကြိုကြည့် (မဒေါင်းခင်)\n"
    "/bookmark — link သိမ်း, /bookmarks — သိမ်းထားတာတွေ\n"
    "/quota — ကိုယ့် download quota ကြည့်\n"
    "/drivestatus — Google Drive upload status\n"
    "/stats — download stats\n"
    "/adduser /deluser /users — owner only\n"
    "/extend <id> <months> — extend user subscription (owner only)\n"
    "/quota <id> [GB|off] — user download quota သတ် (owner only)\n"
    "/admin — 👑 admin panel (owner only)\n"
    "/join — VPS account ကို channel join ခိုင်း (owner only)\n"
    "/xtimeline — X profile ရဲ့ latest video တွေ\n"
    "/clearcache — file_id cache ရှင်း (owner only)\n"
    "/ytcheck [url] — YouTube pipeline စစ် (owner only)\n"
    "🧲 **Torrent** — magnet link ပို့ (သို့) .torrent file တင်\n"
    "   • album/pack ဆို video/audio အားလုံး + subtitle (.srt) တွဲပို့\n"
    "   • 2GB ကျော်တဲ့ file → ☁️ Google Drive တင်ဖို့ မေး (/drivestatus)"
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
    "follow": (
        "📡 /follow — Series episode အသစ် auto-download\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /follow <rss-url> [name]\n\n"
        "ဥပမာ / Example:\n"
        "  /follow https://showrss.info/show/123.rss \"My Series\"\n\n"
        "မှတ်ချက် / Note:\n"
        "• မိနစ် ၃၀ တစ်ခါ feed စစ်မယ် — episode အသစ်တွေ့ရင် auto-download\n"
        "• စထည့်တုန်း ရှိပြီးသား episode တွေကို ကျော်မယ် (အသစ်ပဲ ဒေါင်းမယ်)\n"
        "• seeders မရှိသေးရင် ၃ ခါအထိ ပြန်စမ်းမယ်"
    ),
    "unfollow": (
        "🚫 /unfollow — Follow ဖြုတ်\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /unfollow <name>  — တစ်ခု ဖြုတ်\n"
        "  /unfollow all     — အားလုံး ဖြုတ်"
    ),
    "follows": (
        "📡 /follows — Follow လုပ်ထားတဲ့ series များ ကြည့်\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /follows\n\n"
        "မှတ်ချက် / Note:\n"
        "• ခလုတ်နှိပ်ပြီး ⬇️ auto-download ↔ 🔔 notify-only ပြောင်းလို့ရတယ်\n"
        "• notify-only ဆို episode အသစ်ကျမှ အကြောင်းကြားရုံ (download မလုပ်)"
    ),
    "history": (
        "🕘 /history — ဒေါင်းခဲ့တာတွေ ပြန်ပို့\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /history\n\n"
        "မှတ်ချက် / Note:\n"
        "• အသစ်ဆုံး 10 ခု ပြမယ်\n"
        "• ↩️ ခလုတ်နှိပ်ရင် cache ထဲက ချက်ချင်းပို့ (မရှိရင် ပြန်ဒေါင်း)"
    ),
    "info": (
        "ℹ️ /info — Link info ကြိုကြည့် (မဒေါင်းခင်)\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /info <link>\n\n"
        "ဥပမာ / Example:\n"
        "  /info https://t.me/c/123/456\n\n"
        "မှတ်ချက် / Note:\n"
        "• magnet → file list + size\n"
        "• t.me → media kind/size/duration\n"
        "• web video → title/duration/quality တွေ"
    ),
    "bookmark": (
        "🔖 /bookmark — Link သိမ်းထား, နောက်မှ ဒေါင်း\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /bookmark <link>\n"
        "  /bookmarks        — သိမ်းထားတာတွေ (⬇️ ဒေါင်း / 🗑️ ဖျက်)\n"
        "  /unbookmark <နံပါတ်> — ဖျက်"
    ),
    "bookmarks": (
        "🔖 /bookmarks — သိမ်းထားတဲ့ link တွေ\n\n"
        "/bookmark <link> နဲ့ သိမ်း — /bookmarks မှာ\n"
        "⬇️ နှိပ်ရင် ဒေါင်း, 🗑️ နှိပ်ရင် ဖျက်."
    ),
    "unbookmark": (
        "🗑️ /unbookmark — Bookmark ဖျက်\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /unbookmark <နံပါတ်>\n"
        "(နံပါတ်ကို /bookmarks မှာ ကြည့်)"
    ),
    "quota": (
        "📊 /quota — Download quota\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /quota              — ကိုယ့် quota ကြည့်\n"
        "  /quota <id> [GB|off] — owner only: user quota သတ်/ဖြုတ်\n\n"
        "ဥပမာ / Example:\n"
        "  /quota 123456789 50   (30 ရက်ကို 50GB)\n"
        "  /quota 123456789 off  (unlimited)\n\n"
        "မှတ်ချက် / Note:\n"
        "• quota ကုန်ရင် download ရပ်မယ် — owner ကို ဆက်သွယ်ပါ"
    ),
    "tv": (
        "🔍 /tv — Bot ထဲကနေ series ရှာပြီး follow လုပ်\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /tv <series နာမည်>\n\n"
        "ဥပမာ / Example:\n"
        "  /tv Lioness\n\n"
        "မှတ်ချက် / Note:\n"
        "• website သွားစရာမလို — bot ထဲမှာပဲ ရှာပြီး ရွေးရုံ\n"
        "• ရွေးပြီးရင် episode အသစ်ထွက်တိုင်း auto-download (မိနစ် ၃၀ တစ်ခါစစ်)\n"
        "• episode တစ်ခုကို 1080p တစ်ဖိုင်ပဲ ဒေါင်းမယ်"
    ),
    "menu": (
        "🎛️ /menu — ခလုတ်တွေနဲ့ သုံး\n\n"
        "command တွေ ရိုက်စရာမလို — /menu နှိပ်ပြီး\n"
        "ခလုတ်နှိပ်ရုံနဲ့ ရှာ / follow / setting ချိန်လို့ရတယ်."
    ),
    "subs": (
        "📝 /subs — Subtitle (.srt) ရှာပြီး ပို့\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /subs <movie နာမည်>\n\n"
        "ဥပမာ / Example:\n"
        "  /subs dune part two\n"
        "  /subs dune part two mm  (မြန်မာ subtitle)\n\n"
        "မှတ်ချက် / Note:\n"
        "• movie ရွေးပြီးရင် ဘာသာစကား ရွေးရမယ် (English အပါအဝင်)\n"
        "• YIFY database — ရွေးပြီးရင် .srt file တန်းပို့ပေးမယ်"
    ),
    "search": (
        "🔎 /search — Torrent အကုန် ရှာပြီး ဒေါင်း\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /search <ရှာချင်တဲ့စာသား>\n\n"
        "ဥပမာ / Example:\n"
        "  /search dune part 2 1080p\n"
        "  /search abbey road flac\n\n"
        "မှတ်ချက် / Note:\n"
        "• movie / music / series / software — အကုန်ရှာလို့ရ\n"
        "• seeders များတာကို အရင်ပြမယ် — ရွေးရင် တန်းဒေါင်းပေးမယ်"
    ),
    "drivestatus": (
        "☁️ /drivestatus — Google Drive upload status\n\n"
        "2GB ကျော်တဲ့ torrent file တွေ Telegram ပို့မရတဲ့အခါ\n"
        "Drive ထဲ တင်ပေးဖို့ ဒါ ချိတ်ထားရမယ်.\n\n"
        "Setup လုပ်ပုံ /help drivestatus အစား ဒီ command ကိုပဲ\n"
        "run ကြည့် — အဆင့်ဆင့် ပြပေးမယ်."
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
        "  /adduser <Telegram ID> [လ]\n"
        "  /adduser @username [လ]\n\n"
        "ဥပမာ / Example:\n"
        "  /adduser 123456789 3  (= 3 လ)\n"
        "  /adduser 123456789    (= ထာဝရ)\n\n"
        "မှတ်ချက် / Note:\n"
        "• 1 လ = ရက် 30\n"
        "• သက်တမ်း ကုန်ရင် bot သုံးမရတော့ပါ"
    ),
    "extend": (
        "⏳ /extend — User သက်တမ်း တိုး (owner only)\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /extend <Telegram ID> <လ>\n\n"
        "ဥပမာ / Example:\n"
        "  /extend 123456789 3\n\n"
        "လက်ရှိ သက်တမ်း (သို့) အခုချိန်ကနေ လ ထပ်ပေါင်းမယ်."
    ),
    "admin": (
        "👑 /admin — Admin panel (owner only)\n\n"
        "User အရေအတွက်, သက်တမ်း status, download stats နဲ့\n"
        "လိုအပ်တဲ့ command တွေ တစ်နေရာတည်း ကြည့်လို့ရမယ်."
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
        "  /users\n\n"
        "တစ်ယောက်ချင်းစီရဲ့ သက်တမ်း (ကျန်တဲ့ ရက် / ကုန်ပြီ) ပြမယ်."
    ),
    "join": (
        "🔗 /join — VPS account ကို channel/group join ခိုင်း (owner only)\n\n"
        "Bot က သူ့ရဲ့ VPS Telegram account နဲ့ ဒေါင်းတာမို့ private\n"
        "channel/group ဆို အဲဒီ account ကိုယ်တိုင် member ဖြစ်ရမယ်.\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /join <invite link>\n\n"
        "ဥပမာ / Example:\n"
        "  /join https://t.me/+AbCdEfGhIjKlMnOp\n\n"
        "→ join ပြီးရင် link ပြန်ပို့ပြီး ဒေါင်းလို့ရပြီ"
    ),
    "clearcache": (
        "🧹 /clearcache — file_id cache ရှင်း (owner only)\n\n"
        "တစ်ခါဒေါင်းဖူးတဲ့ link တွေကို bot က Telegram file_id နဲ့ မှတ်ထားပြီး\n"
        "နောက်တစ်ခါ ပြန်ဒေါင်းစရာမလိုဘဲ ချက်ချင်းပို့တယ်. cache က 30 ရက်\n"
        "ကြာရင် အလိုအလျောက် ပျက်မယ်.\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /clearcache\n\n"
        "→ ဖျက်လိုက်တဲ့ entry အရေအတွက်ကို ပြမယ်"
    ),
    "ytcheck": (
        "🔍 /ytcheck — YouTube pipeline စစ်ဆေးချက် (owner only)\n\n"
        "VPS ပေါ်မှာ YouTube ဒေါင်းဖို့ လိုအပ်ချက်တွေ အကုန် စစ်ပေးမယ်:\n"
        "yt-dlp version, PO-token plugin, PO-token server, cookies.\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /ytcheck\n"
        "  /ytcheck <youtube-url>\n\n"
        "ဥပမာ / Example:\n"
        "  /ytcheck https://youtu.be/d33A264UMqo\n\n"
        "→ URL ပေးရင် အဲဒီ video ကို probe လုပ်ပြီး format diagnosis�ါ ပြမယ်.\n"
        "YouTube မရတိုင်း ဒါကို အရင် run ပြီး ရလဒ် ပို့ပေးပါ."
    ),
    "xtimeline": (
        "🐦 /xtimeline — X profile ရဲ့ latest video tweets ဒေါင်း\n\n"
        "အသုံးပြုပုံ / Usage:\n"
        "  /xtimeline @username [အရေအတွက်]\n\n"
        "ဥပမာ / Example:\n"
        "  /xtimeline @NASA 5\n\n"
        "• public account ပဲ ရမယ် (private/protected မရ)\n"
        "• အရေအတွက် 1–10 (default 5)\n"
        "• တွေ့တဲ့ video tweet တွေကို ပုံမှန် download flow နဲ့ ပို့မယ်"
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
    if not ext and kind == "photo":
        # Telegram Photo objects carry no file_name/mime_type, but photos are
        # always JPEG. Without an extension send_photo() fails with
        # PHOTO_EXT_INVALID.
        ext = ".jpg"
    if not ext and kind == "video_note":
        # VideoNote objects likewise carry no file_name/mime_type (mp4).
        ext = ".mp4"
    base = {"video": "video", "audio": "audio", "voice": "voice",
            "photo": "photo", "video_note": "video_note",
            "animation": "animation", "doc": "document"}.get(kind, "file")
    return f"{tag}_{base}{ext or ''}"


def friendly_web_error(e: Exception) -> str | None:
    """Raw yt-dlp errors -> short bilingual fix guide (None = no special case)."""
    s = str(e)
    if "confirm you're not a bot" in s:
        return (
            "❌ YouTube က ဒီ VPS ကို bot အဖြစ် သတ်မှတ်ပြီး block ထားပါတယ်.\n\n"
            "ပြင်နည်း — browser cookies တင်ပေးပါ:\n"
            "1️⃣ ကွန်ပျူတာ browser မှာ YouTube ကို login ဝင်ထားပါ\n"
            "2️⃣ \"Get cookies.txt\" extension နဲ့ youtube.com အတွက် cookies ထုတ်ပါ\n"
            "3️⃣ ရလာတဲ့ cookies.txt ကို VPS ပေါ်မှာ\n"
            "     /opt/tg-video-bot/cookies_youtube.txt (သို့)\n"
            "     cookies.txt အဖြစ် တင်ပါ\n"
            "4️⃣ ပြီးရင် link ပြန်ပို့ပါ\n\n"
            "YouTube is blocking this VPS as a bot. Fix: log into YouTube in a "
            "desktop browser, export youtube.com cookies with the \"Get cookies.txt\" "
            "extension, and upload it as cookies_youtube.txt (or cookies.txt) "
            "under /opt/tg-video-bot/ on the VPS, then resend the link."
            + pot_server_hint()
        )
    if "Requested format is not available" in s:
        extra = ""
        if "||" in s:
            extra = "\n\n🔍 စစ်ဆေးချက်: " + s.split("||", 1)[1].strip()
        # storyboard-only / empty formats = missing PO token signature
        hint = pot_server_hint() if storyboard_only(s) else ""
        return (
            "❌ YouTube က ဒီ video အတွက် download format မပေးပါ.\n"
            "ဖြစ်နိုင်ချေများ:\n"
            "• yt-dlp version အဟောင်း — VPS မှာ update.sh ပြန် run ပေးပါ\n"
            "  (bash /opt/tg-video-bot/update.sh)\n"
            "• video က age-restricted / region-blocked / members-only\n"
            "• VPS IP ကို YouTube က ခဏ limit လုပ်ထားနိုင် — ခဏကြာမှ ပြန်စမ်းပါ"
            + extra + hint +
            "\n\n"
            "YouTube isn't offering a downloadable format for this video. "
            "Try updating yt-dlp via update.sh on the VPS; the video itself "
            "may also be age-restricted, region-blocked, or members-only."
        )
    if "isn't available to everyone" in s or "can't be seen by certain audiences" in s:
        return (
            "❌ ဒီ Instagram reel က လူတိုင်းကြည့်လို့မရတဲ့ content ပါ.\n"
            "ဖြစ်နိုင်ချေများ:\n"
            "• account က private / reel က audience-restricted (age/country)\n"
            "• Instagram က VPS IP ကို login တောင်းနေတာ\n\n"
            "ပြင်နည်း — Instagram cookies တင်ပေးပါ:\n"
            "1️⃣ ကွန်ပျူတာ browser မှာ Instagram ကို login ဝင်ထားပါ\n"
            "2️⃣ \"Get cookies.txt\" extension နဲ့ cookies ထုတ်ပါ\n"
            "   (instagram.com cookies ပါရမယ်)\n"
            "3️⃣ VPS ပေါ် /opt/tg-video-bot/cookies_instagram.txt (သို့)\n"
            "   cookies.txt အဖြစ် တင်ပါ\n"
            "4️⃣ ပြီးရင် link ပြန်ပို့ပါ\n\n"
            "This Instagram reel isn't available to everyone — it may be "
            "private/audience-restricted, or Instagram may be login-walling "
            "the VPS IP. Fix: export cookies.txt while logged into Instagram "
            "and upload it as /opt/tg-video-bot/cookies.txt on the VPS."
        )
    if "[twitter]" in s and "unavailable" in s.lower():
        return (
            "❌ ဒီ X video ကို ရယူလို့မရပါ — age-restricted (သို့) login "
            "လိုအပ်တဲ့ content ဖြစ်ပါတယ်.\n\n"
            "ပြင်နည်း — X cookies တင်ပေးပါ:\n"
            "1️⃣ ကွန်ပျူတာ browser မှာ X ကို login ဝင်ထားပါ\n"
            "2️⃣ \"Get cookies.txt\" extension နဲ့ cookies ထုတ်ပါ\n"
            "   (x.com cookies ပါရမယ်)\n"
            "3️⃣ VPS ပေါ် /opt/tg-video-bot/cookies_twitter.txt (သို့)\n"
            "   cookies.txt အဖြစ် တင်ပါ\n"
            "4️⃣ ပြီးရင် link ပြန်ပို့ပါ\n\n"
            "This X video is unavailable — it's age-restricted or needs login. "
            "Fix: export cookies.txt while logged into X and upload it as "
            "cookies_twitter.txt (or cookies.txt) under /opt/tg-video-bot/ "
            "on the VPS."
        )
    if s.startswith("X_MEDIA:"):
        parts = s.split(":", 2)
        xkind = parts[1] if len(parts) > 1 else "no_media"
        if xkind == "not_found":
            return (
                "❌ ဒီ X post ကို ရှာမတွေ့ပါ — ဖျက်လိုက်တာ (သို့) link မှား"
                "နေတာ ဖြစ်နိုင်ပါတယ်.\n\n"
                "This X post was not found — it may have been deleted, or "
                "the link is wrong."
            )
        if xkind == "private":
            return (
                "❌ ဒီ X post က private/protected account ကပါ — login မပါဘဲ "
                "ရယူလို့မရပါ.\n\n"
                "This X post is from a private/protected account and can't be "
                "fetched without login."
            )
        if xkind == "rate_limited":
            return (
                "❌ X က ခဏ rate-limit ချထားပါတယ် — ၁-၂ မိနစ်ကြာမှ "
                "ပြန်စမ်းပါ.\n\n"
                "X is rate-limiting requests right now — please try again "
                "in a minute or two."
            )
        if xkind == "age_restricted":
            return (
                "❌ ဒီ X video က age-restricted (sensitive) content ပါ.\n\n"
                "ပြင်နည်း — X cookies တင်ပေးပါ:\n"
                "1️⃣ ကွန်ပျူတာ browser မှာ X ကို login ဝင်ထားပါ\n"
                "2️⃣ \"Get cookies.txt\" extension နဲ့ cookies ထုတ်ပါ\n"
                "3️⃣ VPS ပေါ် /opt/tg-video-bot/cookies_twitter.txt (သို့)\n"
                "   cookies.txt အဖြစ် တင်ပါ\n"
                "4️⃣ ပြီးရင် link ပြန်ပို့ပါ\n\n"
                "This X video is age-restricted (sensitive). Fix: export "
                "cookies.txt while logged into X and upload it as "
                "cookies_twitter.txt (or cookies.txt) under /opt/tg-video-bot/."
            )
    if s.startswith("TIKTOK_MEDIA:"):
        parts = s.split(":", 2)
        tkind = parts[1] if len(parts) > 1 else "network"
        if tkind == "not_found":
            return (
                "❌ ဒီ TikTok video ကို ရှာမတွေ့ပါ — ဖျက်လိုက်တာ (သို့) link "
                "မှားနေတာ ဖြစ်နိုင်ပါတယ်.\n\n"
                "This TikTok video was not found — it may have been deleted, "
                "or the link is wrong."
            )
        if tkind == "private":
            return (
                "❌ ဒီ TikTok video က private ပါ — login မပါဘဲ ရယူလို့"
                "မရပါ.\n\n"
                "ပြင်နည်း — TikTok cookies တင်ပေးပါ:\n"
                "1️⃣ ကွန်ပျူတာ browser မှာ TikTok ကို login ဝင်ထားပါ\n"
                "2️⃣ \"Get cookies.txt\" extension နဲ့ cookies ထုတ်ပါ\n"
                "3️⃣ VPS ပေါ် /opt/tg-video-bot/cookies_tiktok.txt (သို့)\n"
                "   cookies.txt အဖြစ် တင်ပါ\n"
                "4️⃣ ပြီးရင် link ပြန်ပို့ပါ\n\n"
                "This TikTok video is private. Fix: export cookies.txt while "
                "logged into TikTok and upload it as cookies_tiktok.txt "
                "(or cookies.txt) under /opt/tg-video-bot/."
            )
        if tkind == "rate_limited":
            return (
                "❌ TikTok download service က ခဏ rate-limit ချထားပါတယ် — "
                "၁-၂ မိနစ်ကြာမှ ပြန်စမ်းပါ.\n\n"
                "The TikTok download service is rate-limiting requests right "
                "now — please try again in a minute or two."
            )
    if "Unexpected response from webpage request" in s and "tiktok" in s.lower():
        return (
            "❌ TikTok က ဒီ VPS ရဲ့ request ကို block လုပ်ထားပါတယ် "
            "(bot စစ်ဆေးမှု).\n"
            "အလိုအလျောက် နည်းလမ်း (tikwm) လည်း အခု မရသေးပါ.\n\n"
            "ပြင်နည်း — TikTok cookies တင်ပေးပါ:\n"
            "1️⃣ ကွန်ပျူတာ browser မှာ TikTok ကို login ဝင်ထားပါ\n"
            "2️⃣ \"Get cookies.txt\" extension နဲ့ cookies ထုတ်ပါ\n"
            "   (tiktok.com cookies ပါရမယ်)\n"
            "3️⃣ VPS ပေါ် /opt/tg-video-bot/cookies_tiktok.txt (သို့)\n"
            "   cookies.txt အဖြစ် တင်ပါ\n"
            "4️⃣ ပြီးရင် link ပြန်ပို့ပါ\n\n"
            "TikTok is blocking this VPS's requests (bot detection), and the "
            "automatic fallback is also unavailable right now. Fix: log into "
            "TikTok in a desktop browser, export cookies with the \"Get "
            "cookies.txt\" extension, and upload it as cookies_tiktok.txt "
            "(or cookies.txt) under /opt/tg-video-bot/, then resend the link."
        )
    return None


def friendly_peer_error(err: str | None) -> str | None:
    """Peer errors -> bilingual guide (None = not a peer error).

    - NOT_A_MEMBER: bot ရဲ့ account က channel/group member မဟုတ် -> join ခိုင်း
    - ကျန် Peer id invalid: member ဖြစ်ပြီးသားကို ခဏတာ resolve မရတာ ->
      ပြန်ပို့ခိုင်း (transient)
    """
    if not err:
        return None
    if "NOT_A_MEMBER" in err:
        return (
            "❌ ဒီ channel/group ကို access မရှိပါ.\n"
            "• Bot ကို run နေတဲ့ Telegram account (VPS ပေါ်မှာ login ဝင်ထားတဲ့\n"
            "  account) က ဒီ channel/group ရဲ့ member ဖြစ်ရမယ်\n"
            "• ⚠️ bot ကို နှိပ်နေတဲ့သူ member ဖြစ်ရုံနဲ့ မရပါ — bot က\n"
            "  သူ့ကိုယ်ပိုင် account နဲ့ ဒေါင်းတာပါ\n"
            "• Private channel/group ဆို အဲဒီ account က အရင် join (သို့)\n"
            "  invite ယူထားရမယ်\n\n"
            "No access to this channel/group. The Telegram account the bot itself "
            "runs as (logged in on the VPS) must be a member — it downloads with "
            "its own account, not yours. Join the private channel/group with that "
            "account first, then resend the link."
        )
    if "Peer id invalid" in err:
        return (
            "⚠️ Telegram နဲ့ ခဏတာ ဆက်သွယ်မှု error တက်သွားပါတယ်.\n"
            "👉 link ကို ပြန်ပို့ပေးပါ — ခဏနေရင် ရနိုင်ပါတယ်.\n"
            "ထပ်ခါထပ်ခါ မရရင် VPS မှာ bot ကို restart လုပ်ပါ:\n"
            "  sudo systemctl restart tg-video-bot\n\n"
            "Temporary Telegram connection issue. Please resend the link — "
            "it often works on retry. If it keeps failing, restart the bot "
            "on the VPS."
        )
    return None


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
    await update.message.reply_text(_stats_text())


async def adduser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("⛔ Owner ပဲ ဒီ command သုံးလို့ရပါတယ်.")
        return
    if not context.args:
        await update.message.reply_text(
            "အသုံးပြုပုံ: /adduser <Telegram ID သို့မဟုတ် @username> [လ]\n"
            "ဥပမာ: /adduser 123456789 3  (= 3 လ)\n"
            "လ မထည့်ရင် ထာဝရ (unlimited).")
        return
    ref = context.args[0]
    months = None
    if len(context.args) > 1:
        try:
            months = float(context.args[1])
            if months <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ လ အရေအတွက် မှားနေပါတယ် — ဥပမာ: /adduser 123456789 3")
            return
    ids = re.findall(r"\d+", ref)
    if not ref.startswith("@") and not ids:
        await update.message.reply_text(
            "❌ ဂဏန်း Telegram ID ထည့်ပါ — ဥပမာ: /adduser 123456789 3")
        return
    try:
        if ref.startswith("@"):
            u = await user.get_users(ref)
            new_id, name = u.id, (u.username or u.first_name or "")
        else:
            new_id, name = int(ids[0]), ""
    except Exception as e:
        await update.message.reply_text(f"❌ User ရှာမရပါ: {e}")
        return
    is_new = user_store.add(new_id, months=months, name=name)
    exp_txt = (f" — {months:g} လ "
               f"({time.strftime('%Y-%m-%d', time.localtime(user_store.expiry(new_id)))})"
               if months else " — ထာဝရ")
    if is_new:
        await update.message.reply_text(f"✅ User {new_id} ကို ထည့်ပြီးပါပြီ{exp_txt}.")
    else:
        await update.message.reply_text(f"ℹ️ ဒီ user ရှိပြီးသားပါ{exp_txt}.")


async def extend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: /extend <id> <လ> — သက်တမ်း တိုး."""
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner ပဲ ဒီ command သုံးလို့ရပါတယ်.")
        return
    if len(context.args) < 2 or not re.findall(r"\d+", context.args[0]):
        await update.message.reply_text(
            "အသုံးပြုပုံ: /extend <Telegram ID> <လ>\nဥပမာ: /extend 123456789 3")
        return
    try:
        months = float(context.args[1])
        if months <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ လ အရေအတွက် မှားနေပါတယ်.")
        return
    ext_id = int(re.findall(r"\d+", context.args[0])[0])
    new_exp = user_store.extend(ext_id, months)
    if new_exp is None:
        await update.message.reply_text("ℹ️ ဒီ user list ထဲမှာ မရှိပါ.")
        return
    await update.message.reply_text(
        f"✅ User {ext_id} ကို {months:g} လ တိုးပြီးပါပြီ — "
        f"သက်တမ်း: {time.strftime('%Y-%m-%d', time.localtime(new_exp))}")


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


def _user_line(u: dict) -> str:
    uid = u["id"]
    tag = " (owner)" if uid == OWNER_ID else ""
    exp = u["expires"]
    if uid == OWNER_ID or exp is None:
        st_txt = "♾️ ထာဝရ"
    else:
        days = (exp - time.time()) / 86400
        dstr = time.strftime("%Y-%m-%d", time.localtime(exp))
        if days < 0:
            st_txt = f"❌ ကုန်ပြီ ({dstr})"
        elif days <= 7:
            st_txt = f"⚠️ {days:.0f} ရက် ကျန် ({dstr})"
        else:
            st_txt = f"✅ {days:.0f} ရက် ကျန် ({dstr})"
    name = f" @{_md_esc(u['name'])}" if u.get("name") else ""
    q = user_store.quota_mb(uid)
    qtxt = ""
    if q:
        used = stats.usage(uid, days=30)
        qtxt = f" 📊 {used:.0f}/{q:.0f}MB"
    return f"• `{uid}`{name}{tag} — {st_txt}{qtxt}"


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner ပဲ ဒီ command သုံးလို့ရပါတယ်.")
        return
    users = [{"id": i, "added": None, "expires": None, "name": ""}
             for i in sorted(ALLOWED_IDS)]
    users += user_store.all_users()
    await update.message.reply_text(
        "👥 **သုံးခွင့်ရှိသူများ:**\n" + "\n".join(_user_line(u) for u in users),
        parse_mode="Markdown")


def _admin_text() -> str:
    users = user_store.all_users()
    n_total = len(users)
    n_expired = sum(1 for u in users if user_store.is_expired(u["id"]))
    n_soon = sum(1 for u in users
                 if (d := user_store.days_left(u["id"])) is not None
                 and 0 <= d <= 7)
    s = stats.summary()
    lines = [
        "👑 **Admin Panel**",
        f"👥 Users: {n_total} (✅ {n_total - n_expired - n_soon} active"
        f"{f', ⚠️ {n_soon} expiring' if n_soon else ''}"
        f"{f', ❌ {n_expired} expired' if n_expired else ''})",
        f"📊 Downloads: today {s['day'][0]} ({s['day'][1]}MB) / "
        f"30d {s['month'][0]} ({s['month'][1]}MB)",
        "",
        "**Commands:**",
        "/adduser <id> [လ] — user ထည့် (ဥ: `/adduser 123 3`)",
        "/extend <id> <လ> — သက်တမ်း တိုး",
        "/deluser <id> — user ဖြုတ်",
        "/users — user list အသေးစိတ် (quota အပါအဝင်)",
        "/quota <id> <GB|off> — user download quota သတ်/ဖြုတ်",
    ]
    return "\n".join(lines)


def _magnet_info(magnet: str) -> str:
    """Sync (run in thread): magnet -> name/size/file list."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tpath = fetch_magnet_metadata(magnet, tmpdir)
        files = torrent_files(tpath)
    if not files:
        return "❌ file list ရမရပါ"
    total = sum(f["size"] for f in files)
    name = files[0]["path"].strip("/").split("/")[0]
    lines = [f"🧲 {name}",
             f"📁 {len(files)} files • 💾 {total / 1073741824:.2f} GB"]
    for f in sorted(files, key=lambda x: -x["size"])[:5]:
        base = os.path.basename(f["path"])[:42]
        lines.append(f"• {base} — {f['size'] / 1048576:.0f}MB")
    if len(files) > 5:
        lines.append(f"… +{len(files) - 5} more")
    lines.append("⬇️ ဒေါင်းချင်ရင် magnet ကို ဒီအတိုင်းပို့လိုက်ပါ")
    return "\n".join(lines)


async def _tg_info(m) -> str:
    """t.me link -> media kind/size/duration."""
    private_id, username, msg_id, thread_msg_id = m.groups()
    mid = int(thread_msg_id or msg_id)
    cid = int(f"-100{private_id}") if private_id else username
    msg, _, err = await fetch_message(cid, mid)
    if not msg or not media_of(msg):
        return f"❌ မရပါ: {friendly_peer_error(err) or (err or 'media မရှိ')}"
    media = media_of(msg)
    kind = media_kind(msg)
    size = getattr(media, "file_size", 0) or 0
    dur = getattr(media, "duration", 0) or 0
    dur_txt = f"{dur // 60}:{dur % 60:02d}" if dur else "?"
    cap = (msg.caption or "")[:60]
    out = [f"✈️ Telegram [{kind}]",
           f"💾 {size / 1048576:.1f} MB",
           f"⏱️ {dur_txt}"]
    if cap:
        out.append(f"💬 {cap}")
    out.append("⬇️ ဒေါင်းချင်ရင် link ကို ဒီအတိုင်းပို့လိုက်ပါ")
    return "\n".join(out)


def _web_info_text(d: dict) -> str:
    dur = d.get("duration")
    dur_txt = f"{dur // 60}:{dur % 60:02d}" if dur else "?"
    out = [f"🌐 {(d.get('title') or '?')[:70]}",
           f"📺 {d.get('site') or '?'}" +
           (f" • 👤 {(d.get('uploader') or '')[:30]}" if d.get("uploader") else ""),
           f"⏱️ {dur_txt}"]
    for f in d.get("formats") or []:
        res = f"{f['height']}p" if f.get("height") else "?"
        sz = f" • {f['mb']:.0f}MB" if f.get("mb") else ""
        out.append(f"• {res} ({f.get('ext') or '?'}){sz}")
    out.append("⬇️ ဒေါင်းချင်ရင် link ကို ဒီအတိုင်းပို့လိုက်ပါ")
    return "\n".join(out)


def _looks_bookmarkable(text: str) -> bool:
    return bool(extract_magnets(text) or LINK_RE.search(text)
                or extract_web_urls(text))


def _bookmarks_kb(items: list):
    kb = []
    for i, b in enumerate(items):
        kb.append([
            InlineKeyboardButton(f"⬇️ {i + 1}", callback_data=f"bm:dl:{i}"),
            InlineKeyboardButton(f"🗑️ {i + 1}", callback_data=f"bm:del:{i}"),
        ])
    return InlineKeyboardMarkup(kb) if kb else None


def _bookmarks_text(items: list) -> str:
    lines = ["🔖 **Bookmarks** (⬇️=ဒေါင်း, 🗑️=ဖျက်):"]
    for i, b in enumerate(items):
        ts = time.strftime("%m-%d", time.localtime(b.get("ts", 0)))
        lines.append(f"{i + 1}. `{ts}` {_md_esc((b.get('title') or '')[:55])}")
    return "\n".join(lines)


async def bookmark_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bookmark <link> — link သိမ်းထား, နောက်မှ ဒေါင်း."""
    if not allowed(update):
        return
    uid = update.effective_user.id
    text = " ".join(context.args or []).strip()
    if not text or not _looks_bookmarkable(text):
        await update.message.reply_text(
            "အသုံးပြုပုံ: /bookmark <link>\n"
            "(web video / magnet / t.me link)")
        return
    # first link-ish token only
    token = text.split()[0]
    if bookmarks.add(uid, token, token[:60]):
        n = len(bookmarks.list(uid))
        await update.message.reply_text(
            f"🔖 သိမ်းပြီးပါပြီ ({n} ခု) — /bookmarks နဲ့ ကြည့်ပါ.")
    else:
        await update.message.reply_text("ℹ️ ဒီ link သိမ်းပြီးသားပါ.")


async def bookmarks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bookmarks — သိမ်းထားတဲ့ link တွေ."""
    if not allowed(update):
        return
    uid = update.effective_user.id
    items = bookmarks.list(uid)
    if not items:
        await update.message.reply_text(
            "🔖 Bookmark မရှိသေးပါ — /bookmark <link> နဲ့ သိမ်းပါ.")
        return
    await update.message.reply_text(
        _bookmarks_text(items), parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=_bookmarks_kb(items))


async def unbookmark_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unbookmark <နံပါတ်> — bookmark ဖျက်."""
    if not allowed(update):
        return
    uid = update.effective_user.id
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "အသုံးပြုပုံ: /unbookmark <နံပါတ်>\n"
            "(/bookmarks မှာ နံပါတ်ကြည့်ပါ)")
        return
    if bookmarks.remove(uid, int(args[0]) - 1):
        await update.message.reply_text("🗑️ ဖျက်ပြီးပါပြီ.")
    else:
        await update.message.reply_text("❌ နံပါတ် မှားနေပါတယ်.")


async def bm_pick(q, update, context, action: str, idx: int):
    uid = q.from_user.id
    items = bookmarks.list(uid)
    if idx >= len(items):
        try:
            await q.answer("မရှိတော့ပါ — /bookmarks ပြန်နှိပ်ပါ",
                           show_alert=True)
        except Exception:
            pass
        return
    if action == "del":
        bookmarks.remove(uid, idx)
        items = bookmarks.list(uid)
        try:
            if items:
                await q.edit_message_text(
                    _bookmarks_text(items), parse_mode="Markdown",
                    disable_web_page_preview=True,
                    reply_markup=_bookmarks_kb(items))
            else:
                await q.edit_message_text("🔖 Bookmark ကုန်သွားပါပြီ.")
        except Exception:
            pass
        return
    # dl
    try:
        await q.edit_message_text(
            f"🔖 bookmark {idx + 1} ဒေါင်းနေပါတယ်...")
    except Exception:
        pass
    await handle_link(update, context, text=items[idx]["url"], prompt=False)


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/info <link> — မဒေါင်းခင် info ကြိုကြည့် (magnet/t.me/web)."""
    if not allowed(update):
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text(
            "အသုံးပြုပုံ: /info <link>\n"
            "ဥပမာ: /info https://t.me/c/123/456\n"
            "(magnet / web video link လည်း ရပါတယ်)")
        return
    status = await update.message.reply_text("ℹ️ info ယူနေပါတယ်...")
    try:
        magnets = extract_magnets(text)
        if magnets:
            txt = await asyncio.to_thread(_magnet_info, magnets[0])
        else:
            m = LINK_RE.search(text)
            if m:
                txt = await _tg_info(m)
            else:
                d = await asyncio.wait_for(
                    asyncio.to_thread(web_info, text), timeout=90)
                txt = _web_info_text(d)
        await status.edit_text(txt, disable_web_page_preview=True)
    except Exception as e:
        await status.edit_text(
            f"❌ info ရမရပါ: {type(e).__name__}: {str(e)[:200]}")


def _history_view(uid: int):
    """-> (text, InlineKeyboardMarkup|None) — shared by /history and menu."""
    items = stats.recent(uid, 10)
    if not items:
        return "🕘 History မရှိသေးပါ — link ပို့ပြီး စဒေါင်းပါ.", None
    pending_history[uid] = items
    lines = ["🕘 **Download history** (အသစ်ဆုံး 10):"]
    kb = []
    for i, it in enumerate(items):
        ts = time.strftime("%m-%d %H:%M", time.localtime(it.get("ts", 0)))
        kind, mb = it.get("kind", "?"), it.get("mb", 0) or 0
        label = _md_esc((it.get("url") or "")[:50]) or kind
        lines.append(f"{i + 1}. `{ts}` [{kind}] {mb:.0f}MB\n   {label}")
        if it.get("url") or it.get("ckey"):
            kb.append([InlineKeyboardButton(f"↩️ {i + 1} ပြန်ပို့",
                                            callback_data=f"hist:{i}")])
    return "\n".join(lines), InlineKeyboardMarkup(kb) if kb else None


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🕘 /history — ဒေါင်းခဲ့တာတွေ, နှိပ်တာနဲ့ ပြန်ပို့."""
    if not allowed(update):
        return
    text, kb = _history_view(update.effective_user.id)
    await update.message.reply_text(
        text, parse_mode="Markdown", disable_web_page_preview=True,
        reply_markup=kb)


async def hist_pick(q, update, context, idx: int):
    """History resend: file_id cache -> instant, else re-download URL."""
    uid = q.from_user.id
    items = pending_history.get(uid, [])
    if idx >= len(items):
        try:
            await q.edit_message_text("⏰ သက်တမ်းကုန်သွားပါပြီ — /history ပြန်နှိပ်ပါ.")
        except Exception:
            pass
        return
    it = items[idx]
    ckey = it.get("ckey")
    if ckey:
        hit = fcache.get(ckey)
        if hit:
            try:
                await q.edit_message_text("⚡ မှတ်ထားပြီးသား — ချက်ချင်းပို့နေပါတယ်...")
            except Exception:
                pass
            await deliver_cached(uid, q.message.chat_id, hit,
                                 it.get("url") or "")
            return
    url = it.get("url")
    if url:
        try:
            await q.edit_message_text("🔄 ပြန်ဒေါင်းနေပါတယ်...")
        except Exception:
            pass
        await handle_link(update, context, text=url, prompt=False)
    else:
        try:
            await q.answer("cache သက်တမ်းကုန်သွားပါပြီ", show_alert=True)
        except Exception:
            pass


async def quota_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📊 Quota: /quota (ကိုယ့်ဟာ) | /quota <id> [GB|off] (owner)."""
    uid = update.effective_user.id
    if not allowed(update):
        return
    args = context.args or []

    def _usage_text(tid: int) -> str:
        ok, used, q = quota_allows(tid)
        if q is None:
            return f"📊 User `{tid}` quota: ♾️ unlimited"
        pct = used / q if q else 0
        flag = "❌ ကုန်ပြီ" if not ok else ("⚠️ နီးနေပြီ" if pct >= 0.8 else "✅")
        return (f"📊 User `{tid}` quota: {used:.0f}/{q:.0f} MB "
                f"(ရက် 30) {flag}")

    if not args:
        await update.message.reply_text(_usage_text(uid), parse_mode="Markdown")
        return
    if not is_owner(uid):
        await update.message.reply_text("⛔ Owner ပဲ ဒီ command သုံးလို့ရပါတယ်.")
        return
    ids = re.findall(r"\d+", args[0])
    if not ids:
        await update.message.reply_text(
            "အသုံးပြုပုံ: /quota <Telegram ID> [GB|off]\n"
            "ဥပမာ: /quota 123456789 50")
        return
    tid = int(ids[0])
    if len(args) == 1:
        await update.message.reply_text(_usage_text(tid), parse_mode="Markdown")
        return
    val = args[1].lower()
    if val in ("off", "0", "unlimited"):
        if user_store.set_quota(tid, None):
            await update.message.reply_text(f"✅ User {tid} quota ဖြုတ်ပြီးပါပြီ (unlimited).")
        else:
            await update.message.reply_text("ℹ️ ဒီ user list ထဲမှာ မရှိပါ.")
        return
    try:
        gb = float(val)
        if gb <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ GB မှားနေပါတယ် — ဥပမာ: /quota 123456789 50")
        return
    if user_store.set_quota(tid, gb * 1024):
        await update.message.reply_text(
            f"✅ User {tid} quota: {gb:g} GB / 30 ရက် သတ်မှတ်ပြီးပါပြီ.")
    else:
        await update.message.reply_text("ℹ️ ဒီ user list ထဲမှာ မရှိပါ.")


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner ပဲ ဒီ command သုံးလို့ရပါတယ်.")
        return
    await update.message.reply_text(_admin_text(), parse_mode="Markdown")


async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: VPS Telegram account ကို channel/group join ခိုင်းမယ်.

    အသုံးပြုပုံ: /join <invite link>
    ဥပမာ: /join https://t.me/+AbCdEfGhIjKlMnOp
    """
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner ပဲ ဒီ command သုံးလို့ရပါတယ်.")
        return
    if not context.args:
        await update.message.reply_text(
            "အသုံးပြုပုံ: /join <invite link>\n"
            "ဥပမာ: /join https://t.me/+AbCdEfGhIjKlMnOp\n\n"
            "Bot ရဲ့ VPS account က အဲဒီ channel/group ကို join လုပ်ပါမယ် — "
            "ပြီးမှ link တွေ ဒေါင်းလို့ရမယ်."
        )
        return
    link = context.args[0].strip()
    wait = await update.message.reply_text("⏳ Join လုပ်နေပါတယ်...")
    try:
        chat = await user.join_chat(link)
        title = getattr(chat, "title", None) or getattr(chat, "id", link)
        try:
            await user.get_chat(chat.id)
            ok = True
        except Exception:
            ok = False
        await wait.edit_text(
            f"✅ Join ပြီးပါပြီ: **{title}**\n"
            + ("📥 အခု link တွေ ဒေါင်းလို့ရပါပြီ — ပြန်ပို့ပေးပါ." if ok
               else "⚠️ join ခဲ့ပေမယ့် ဖတ်လို့ မရသေးပါ — invite link စစ်ပါ.")
        )
    except Exception as e:
        msg = str(e)
        hint = ""
        if "USER_ALREADY_PARTICIPANT" in msg:
            hint = "\nℹ️ အဲဒီ account က member ဖြစ်ပြီးသားပါ — link ပြန်ပို့စမ်းကြည့်ပါ."
        elif "INVITE_HASH_EXPIRED" in msg or "INVITE_HASH_INVALID" in msg:
            hint = "\n💡 invite link သက်တမ်း ကုန်နေတာ (သို့) မှားနေတာ ဖြစ်နိုင်ပါတယ်."
        await wait.edit_text(f"❌ Join မရပါ: {msg}{hint}")


async def ytcheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: YouTube download pipeline diagnostics on the VPS.

    အသုံးပြုပုံ: /ytcheck [youtube-url]
    URL ပေးရင် အဲဒီ video ကို probe လုပ်ပြီး format diagnosis�ါ ပြမယ်.
    """
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner ပဲ ဒီ command သုံးလို့ရပါတယ်.")
        return
    st = yt_pipeline_status()
    L = ["🔍 YouTube pipeline စစ်ဆေးချက် (VPS):", ""]
    L.append(f"• yt-dlp: {st['ytdlp']}")
    if st["pot_plugin"]:
        L.append(f"• PO-token plugin: ✅ installed ({st['pot_plugin']})")
    else:
        L.append("• PO-token plugin: ❌ မရှိပါ — update.sh run ပေးပါ\n"
                 "  (bash /opt/tg-video-bot/update.sh)")
    if st["pot_server"]:
        L.append(f"• PO-token server: ✅ reachable ({st['pot_url']})")
    else:
        L.append(f"• PO-token server: ❌ down ({st['pot_url']})")
        L.append("  run: docker run -d --restart unless-stopped "
                 "--name pot-provider -p 127.0.0.1:4416:4416 "
                 "brainicism/bgutil-ytdlp-pot-provider")
    if st["cookies"]:
        L.append(f"• cookies: ✅ {st['cookies']}")
    else:
        L.append("• cookies: ❌ မရှိပါ — cookies_youtube.txt တင်ပေးပါ")
    url = (context.args[0] if context.args else "").strip()
    if url and ("youtube.com" in url or "youtu.be" in url):
        wait = await update.message.reply_text("\n".join(L) + "\n\n⏳ probe လုပ်နေပါတယ်...")
        diag = await _diagnose_formats(url)
        L += ["", "🔍 probe: " + diag]
        await wait.edit_text("\n".join(L))
    elif url:
        L.append("\n⚠️ YouTube URL မဟုတ်လို့ probe ကျော်လိုက်ပါတယ်.")
        await update.message.reply_text("\n".join(L))
    else:
        L.append("\n💡 URL ပါ ပေးရင် probe လုပ်ပေးမယ်: /ytcheck <youtube-url>")
        await update.message.reply_text("\n".join(L))


async def clearcache_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: file_id cache ရှင်း (30 ရက် TTL အလိုအလျောက်ပျက်)."""
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner ပဲ ဒီ command သုံးလို့ရပါတယ်.")
        return
    n = fcache.clear()
    await update.message.reply_text(
        f"🧹 file_id cache ရှင်းပြီးပါပြီ — {n} entry ဖျက်လိုက်တယ်.\n"
        f"Cache cleared — {n} entries removed.")


async def xtimeline_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """X profile ရဲ့ latest video tweets တွေ ဒေါင်း: /xtimeline @NASA 5."""
    if not allowed(update):
        return
    args = context.args or []
    username, n = parse_timeline_args(args)
    if not username:
        if not args:
            await update.message.reply_text(
                "အသုံးပြုပုံ / Usage:\n"
                "  /xtimeline @username [အရေအတွက်]\n\n"
                "ဥပမာ / Example:\n"
                "  /xtimeline @NASA 5\n\n"
                "• public account ပဲ ရမယ် (private/protected မရ)\n"
                "• အရေအတွက် 1–10 (default 5)")
        else:
            await update.message.reply_text(
                "❌ username မှားနေပါတယ် — ဥပမာ /xtimeline @NASA 5\n"
                "Invalid username — e.g. /xtimeline @NASA 5")
        return
    wait = await update.message.reply_text(
        f"⏳ @{username} ရဲ့ နောက်ဆုံး video {n} ခု ရှာနေပါတယ်...\n"
        f"Fetching @{username}'s latest {n} videos...")
    try:
        ids = await fetch_x_timeline(username, n)
    except Exception as e:
        await wait.edit_text(f"❌ X timeline မရပါ:\n{str(e)[:300]}")
        return
    if not ids:
        await wait.edit_text(
            f"ℹ️ @{username} ရဲ့ နောက်ဆုံး post တွေထဲမှာ video မတွေ့ပါ.\n"
            f"No videos in @{username}'s latest posts.")
        return
    await wait.edit_text(
        f"✅ {len(ids)} ခု တွေ့ပြီ — ဒေါင်းနေပါတယ်...\n"
        f"Found {len(ids)} — downloading...")
    urls = [f"https://x.com/i/status/{i}" for i in ids]
    await handle_link(update, context, text="\n".join(urls), prompt=False)


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


# ---------------------------------------------------------------- series auto-follow (torrent RSS)
async def follow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/follow <rss-url> [name] — episode အသစ်ထွက်တိုင်း auto-download."""
    if not allowed(update):
        return
    uid = update.effective_user.id
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "အသုံးပြုပုံ: /follow <rss-url> [name]\n"
            "ဥပမာ: /follow https://showrss.info/show/123.rss \"My Series\"\n"
            "episode အသစ်ထွက်တိုင်း bot က auto-download လုပ်ပေးမယ်.")
        return
    rss_url = args[0]
    name = " ".join(args[1:]).strip()
    status = await update.message.reply_text("📡 feed စစ်နေပါတယ်...")
    try:
        items = await asyncio.to_thread(fetch_items, rss_url)
    except Exception as e:
        await status.edit_text(f"❌ feed ဖတ်မရပါ: {e}")
        return
    if not items:
        await status.edit_text("❌ feed ထဲမှာ item မရှိပါ.")
        return
    seen = {it["guid"]: MAX_ATTEMPTS for it in items}  # လက်ရှိတွေကို ကျော်
    fid = follows.add(uid, rss_url, name or items[0]["title"][:50],
                      update.effective_chat.id, seen)
    await status.edit_text(
        f"✅ follow လုပ်ပြီးပါပြီ: **{name or items[0]['title'][:50]}**\n"
        f"📡 {rss_url}\n🆕 episode အသစ်ထွက်ရင် auto-download လုပ်ပေးမယ်.")


async def unfollow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unfollow <name|id|all>."""
    if not allowed(update):
        return
    uid = update.effective_user.id
    args = context.args or []
    if not args:
        await update.message.reply_text("အသုံးပြုပုံ: /unfollow <name>  သို့မဟုတ်  /unfollow all")
        return
    if args[0].lower() == "all":
        n = follows.remove_all(uid)
        await update.message.reply_text(f"🚫 follow {n} ခု ဖြုတ်ပြီးပါပြီ.")
        return
    key = " ".join(args).lower()
    fl = follows.list(uid)
    fid = None
    for k, v in fl.items():
        if k.startswith(key) or key in v.get("name", "").lower():
            fid = k
            break
    if fid and follows.remove(uid, fid):
        await update.message.reply_text("🚫 follow ဖြုတ်ပြီးပါပြီ.")
    else:
        await update.message.reply_text("ℹ️ ဒီ follow မရှိပါ — /follows နဲ့ ကြည့်ပါ.")


async def follows_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/follows — follow list."""
    if not allowed(update):
        return
    uid = update.effective_user.id
    fl = follows.list(uid)
    if not fl:
        await update.message.reply_text(
            "📡 Follow လုပ်ထားတာ မရှိသေးပါ.\n"
            "🔍 /tv <series> ဒါမှမဟုတ် /follow <rss-url> နဲ့ ထည့်ပါ.")
        return
    await update.message.reply_text(
        _follows_text(uid) + "\n\n_ခလုတ်နှိပ်ပြီး ⬇️ auto-download ↔ 🔔 notify-only ပြောင်းပါ_",
        parse_mode="Markdown", reply_markup=_follows_kb(uid))


# ------------------------------------------------- in-bot series search (/tv)
async def tv_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tv <series name> — bot ထဲကနေ series ရှာပြီး follow လုပ် (TVMaze+EZTV)."""
    if not allowed(update):
        return
    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text(
            "အသုံးပြုပုံ: /tv <series နာမည်>\n"
            "ဥပမာ: /tv Lioness\n"
            "တွေ့တဲ့ series ကို ရွေးရင် episode အသစ်ထွက်တိုင်း\n"
            "auto-download လုပ်ပေးမယ် — website သွားစရာမလို.")
        return
    status = await update.message.reply_text(f"🔍 \"{query}\" ရှာနေပါတယ်...")
    try:
        results = await asyncio.to_thread(tvmaze_search, query)
    except Exception as e:
        await status.edit_text(f"❌ ရှာမရပါ: {e}")
        return
    results = results[:8]
    if not results:
        await status.edit_text("❌ ဒီနာမည်နဲ့ series မတွေ့ပါ.")
        return
    kb = [[InlineKeyboardButton(
        f"{r['name']}{(' (' + r['year'] + ')') if r['year'] else ''}",
        callback_data=f"tv:{r['tvmaze_id']}")] for r in results]
    await status.edit_text(
        "🔍 တွေ့တဲ့ series — follow လုပ်ချင်တာ ရွေးပါ:",
        reply_markup=InlineKeyboardMarkup(kb))


async def tv_pick(q, tvmaze_id: int):
    """Inline button -> EZTV follow တစ်ခု ထည့်."""
    uid = q.from_user.id
    try:
        await q.edit_message_text("📡 series အချက်အလက် ယူနေပါတယ်...")
    except Exception:
        pass
    try:
        show = await asyncio.to_thread(tvmaze_show, tvmaze_id)
    except Exception as e:
        try:
            await q.edit_message_text(f"❌ series ဖတ်မရပါ: {e}")
        except Exception:
            pass
        return
    imdb = show.get("imdb_id")
    name = show.get("name") or "?"
    if show.get("year"):
        name = f"{name} ({show['year']})"
    if not imdb:
        try:
            await q.edit_message_text(
                f"❌ **{name}** — IMDb ID မရှိလို့ follow လုပ်မရပါ.")
        except Exception:
            pass
        return
    try:
        items = await asyncio.to_thread(fetch_eztv_items, imdb)
    except Exception as e:
        try:
            await q.edit_message_text(f"❌ episode list ဖတ်မရပါ: {e}")
        except Exception:
            pass
        return
    if not items:
        try:
            await q.edit_message_text(f"ℹ️ **{name}** — torrent မရှိသေးပါ.")
        except Exception:
            pass
        return
    seen = {it["guid"]: MAX_ATTEMPTS for it in items}  # လက်ရှိတွေကို ကျော်
    follows.add_eztv(uid, name, imdb, tvmaze_id, q.message.chat_id, seen)
    try:
        await q.edit_message_text(
            f"✅ follow လုပ်ပြီးပါပြီ: **{name}**\n"
            f"🆕 episode အသစ်ထွက်ရင် auto-download လုပ်ပေးမယ်.")
    except Exception:
        pass


# ------------------------------------------------- general torrent search
async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/search <text> — torrent မှာ ရှိသမျှ ရှာ (movie/music/series/software)."""
    if not allowed(update):
        return
    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text(
            "အသုံးပြုပုံ: /search <ရှာချင်တဲ့စာသား>\n"
            "ဥပမာ: /search dune part 2 1080p\n"
            "ဥပမာ: /search abbey road flac")
        return
    status = await update.message.reply_text(f"🔍 \"{query}\" ရှာနေပါတယ်...")
    try:
        results = await asyncio.to_thread(apibay_search, query)
    except Exception as e:
        await status.edit_text(f"❌ ရှာမရပါ: {e}")
        return
    results = results[:8]
    if not results:
        await status.edit_text("❌ ဒါနဲ့ကိုက်တဲ့ torrent မတွေ့ပါ.")
        return
    kb = []
    for r in results:
        label = r["name"][:42]
        label += f" ({fmt_size(r['size'])}, 🌱{r['seeders']})"
        kb.append([InlineKeyboardButton(
            label, callback_data=f"dl:{r['info_hash']}")])
    await status.edit_text(
        "🔍 တွေ့တဲ့ torrent — ဒေါင်းချင်တာ ရွေးပါ (seeders များတာကို အရင်ပြ):",
        reply_markup=InlineKeyboardMarkup(kb))


async def dl_pick(q, info_hash: str):
    """Inline button -> magnet တစ်ခု ဒေါင်း (ပုံမှန် torrent pipeline)."""
    uid = q.from_user.id
    magnet = f"magnet:?xt=urn:btih:{info_hash}"
    try:
        msg = await q.edit_message_text("🧲 torrent ဒေါင်းနေပါတယ်...")
    except Exception:
        msg = q.message
    await run_torrent(msg, uid, q.message.chat_id, magnet, True, status=msg)


# ------------------------------------------------- subtitles (/subs)
async def subs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/subs <movie name> — subtitle (.srt) ရှာပြီး ပို့."""
    if not allowed(update):
        return
    args = list(context.args or [])
    lang_pref = None
    if args and args[-1].lower() in LANG_ALIASES:
        lang_pref = LANG_ALIASES[args.pop().lower()]
    pending_sub_lang[update.effective_user.id] = lang_pref
    query = " ".join(args).strip()
    if not query:
        await update.message.reply_text(
            "အသုံးပြုပုံ: /subs <movie နာမည်> [ဘာသာစကား]\n"
            "ဥပမာ: /subs dune part two\n"
            "ဥပမာ: /subs dune part two mm  (မြန်မာ subtitle)\n"
            "(YIFY database)")
        return
    status = await update.message.reply_text(f"📝 \"{query}\" ရှာနေပါတယ်...")
    try:
        movies = await asyncio.to_thread(search_movies, query)
    except Exception as e:
        await status.edit_text(f"❌ ရှာမရပါ: {e}")
        return
    if not movies:
        await status.edit_text("❌ ဒါနဲ့ကိုက်တဲ့ movie မတွေ့ပါ.")
        return
    if len(movies) == 1:
        await _subs_movie(status, update.effective_user.id,
                          movies[0]["imdb"], lang_pref)
        return
    kb = [[InlineKeyboardButton(
        m["movie"][:50], callback_data=f"subm:{m['imdb']}")]
        for m in movies[:8]]
    await status.edit_text("🎬 ဘယ် movie လဲ ရွေးပါ:",
                           reply_markup=InlineKeyboardMarkup(kb))


async def _subs_movie(msg, uid: int, imdb: str, lang: str | None):
    """lang given (/subs x mm) -> straight to subs, else language picker."""
    if lang:
        await _show_subs(msg, imdb, lang)
    else:
        await _show_langs(msg, uid, imdb)


async def _show_langs(msg, uid: int, imdb: str):
    """Movie page -> language buttons."""
    try:
        langs = await asyncio.to_thread(movie_languages, imdb)
    except Exception as e:
        await msg.edit_text(f"❌ subtitle ရမရပါ: {e}")
        return
    if not langs:
        await msg.edit_text("❌ ဒီ movie အတွက် subtitle မတွေ့ပါ.")
        return
    pending_sublangs[uid] = {"imdb": imdb, "langs": langs}
    kb = [[InlineKeyboardButton(l, callback_data=f"subl:{i}")]
          for i, l in enumerate(langs)]
    await msg.edit_text("🌐 ဘယ်ဘာသာစကားလဲ ရွေးပါ:",
                        reply_markup=InlineKeyboardMarkup(kb))


async def _show_subs(msg, imdb: str, lang: str = "English"):
    """Movie page -> subtitle buttons in `lang`."""
    try:
        title, subs = await asyncio.to_thread(movie_subtitles, imdb, lang)
    except Exception as e:
        await msg.edit_text(f"❌ subtitle ရမရပါ: {e}")
        return
    subs = subs[:8]
    if not subs:
        await msg.edit_text(
            f"❌ \"{title}\" အတွက် {lang} subtitle မတွေ့ပါ.")
        return
    kb = []
    for s in subs:
        rel = (s["releases"][0][:36] + "…") if s["releases"] else s["slug"]
        star = f" ⭐{s['rating']}" if s["rating"] > 0 else ""
        kb.append([InlineKeyboardButton(
            f"{rel}{star}", callback_data=f"subs:{s['slug']}")])
    await msg.edit_text(f"📝 \"{title}\" ({lang}) — subtitle ရွေးပါ:",
                        reply_markup=InlineKeyboardMarkup(kb))


async def subm_pick(q, imdb: str):
    uid = q.from_user.id
    lang = pending_sub_lang.get(uid)
    try:
        await q.edit_message_text("📝 subtitle တွေ ယူနေပါတယ်...")
    except Exception:
        pass
    await _subs_movie(q.message, uid, imdb, lang)


async def subl_pick(q, idx: int):
    uid = q.from_user.id
    pend = pending_sublangs.get(uid)
    if not pend or idx >= len(pend["langs"]):
        try:
            await q.edit_message_text("⏰ သက်တမ်းကုန်သွားပါပြီ — /subs ပြန်နှိပ်ပါ.")
        except Exception:
            pass
        return
    lang = pend["langs"][idx]
    try:
        await q.edit_message_text(f"📝 {lang} subtitle တွေ ယူနေပါတယ်...")
    except Exception:
        pass
    await _show_subs(q.message, pend["imdb"], lang)


async def subs_pick(q, slug: str):
    uid = q.from_user.id
    chat_id = q.message.chat_id
    try:
        msg = await q.edit_message_text("📝 subtitle ဒေါင်းနေပါတယ်...")
    except Exception:
        msg = q.message
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            name = slug.rsplit("-yify-", 1)[0].replace("-", " ").title()
            path = await asyncio.to_thread(
                download_subtitle, slug, name, tmpdir)
            await bot_client.send_document(
                chat_id, path, caption=f"📝 {os.path.basename(path)}")
            try:
                await msg.delete()
            except Exception:
                pass
    except Exception as e:
        traceback.print_exc()
        try:
            await msg.edit_text(f"❌ မရပါ: {e}")
        except Exception:
            pass


# ---------------------------------------------------------------- menu (UI/UX)
BOT_COMMANDS = [
    ("menu", "🎛️ Menu — ခလုတ်တွေနဲ့ သုံး"),
    ("search", "🔎 Torrent ရှာ (movie/music/series)"),
    ("subs", "📝 Subtitle (.srt) ရှာ"),
    ("tv", "🔍 Series ရှာပြီး follow"),
    ("follow", "📡 Series RSS follow"),
    ("follows", "📡 Follow list ကြည့်"),
    ("unfollow", "🚫 Follow ဖြုတ်"),
    ("mode", "🎬 video/file ပို့ပုံစံ"),
    ("quality", "🎞️ high/low quality"),
    ("mp3", "🎵 MP3 ထုတ် on/off"),
    ("zip", "📦 ZIP ပေါင်း on/off"),
    ("save", "💾 Saved Messages auto-save"),
    ("nightmode", "🌙 ညဘက် ဒေါင်း"),
    ("trim", "✂️ video အပိုင်းဖြတ်"),
    ("find", "🔍 channel ထဲ media ရှာ"),
    ("watch", "👁️ channel post အသစ် auto-download"),
    ("watchlist", "👁️ watch list"),
    ("unwatch", "👁️ unwatch"),
    ("xtimeline", "🐦 X profile video တွေ"),
    ("drivestatus", "☁️ Google Drive status"),
    ("stats", "📊 download stats"),
    ("history", "🕘 ဒေါင်းခဲ့တာတွေ ပြန်ပို့"),
    ("info", "ℹ️ link info ကြိုကြည့်"),
    ("bookmark", "🔖 link သိမ်း"),
    ("bookmarks", "🔖 သိမ်းထားတာတွေ"),
    ("quota", "📊 ကိုယ့် quota ကြည့်"),
    ("ytcheck", "▶️ YouTube စစ်"),
    ("help", "📖 အကူအညီ"),
]


async def _set_bot_commands(app):
    """'/' menu မှာ command list ပေါ်အောင် (BotFather သွားစရာမလို)."""
    try:
        from telegram import BotCommand
        await app.bot.set_my_commands(
            [BotCommand(c, d) for c, d in BOT_COMMANDS])
        print("✅ bot command menu set")
    except Exception as e:
        print(f"⚠️ set_my_commands failed: {e}")


def _menu_kb(uid: int) -> InlineKeyboardMarkup:
    s = st(uid)
    tg = lambda v: "🟢" if v else "⚪"  # noqa: E731
    rows = [
        [InlineKeyboardButton("🔎 Torrent ရှာ", callback_data="menu:search"),
         InlineKeyboardButton("🔍 Series follow", callback_data="menu:tv"),
         InlineKeyboardButton("📝 Subs", callback_data="menu:subs")],
        [InlineKeyboardButton("📡 Follows", callback_data="menu:follows"),
         InlineKeyboardButton("📊 Stats", callback_data="menu:stats")],
        [InlineKeyboardButton(f"🎬 Mode: {s['mode']}",
                              callback_data="menu:mode"),
         InlineKeyboardButton(f"🎞️ Quality: {s['quality']}",
                              callback_data="menu:quality")],
        [InlineKeyboardButton(f"🎵 MP3 {tg(s['mp3'])}",
                              callback_data="menu:mp3"),
         InlineKeyboardButton(f"📦 ZIP {tg(s['zip'])}",
                              callback_data="menu:zip")],
        [InlineKeyboardButton(f"💾 Save {tg(s['save'])}",
                              callback_data="menu:save"),
         InlineKeyboardButton(f"🌙 Night {tg(s['night'])}",
                              callback_data="menu:night")],
        [InlineKeyboardButton("☁️ Drive", callback_data="menu:drive"),
         InlineKeyboardButton("📖 Help", callback_data="menu:help")],
        [InlineKeyboardButton("🕘 History", callback_data="menu:history"),
         InlineKeyboardButton("🔖 Bookmarks", callback_data="menu:bookmarks")],
    ]
    if uid == OWNER_ID:
        rows.append([InlineKeyboardButton("👑 Admin", callback_data="menu:admin")])
    return InlineKeyboardMarkup(rows)


_BACK_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("« 🎛️ Menu", callback_data="menu:main")]])


def _stats_text() -> str:
    s = stats.summary()
    return ("📊 **Download Stats**\n\n"
            f"📅 ဒီနေ့: {s['day'][0]} ခု, {s['day'][1]} MB\n"
            f"🗓️ ရက် ၃၀: {s['month'][0]} ခု, {s['month'][1]} MB\n"
            f"♾️ စုစုပေါင်း: {s['all'][0]} ခု, {s['all'][1]} MB")


def _follows_text(uid: int) -> str:
    fl = follows.list(uid)
    if not fl:
        return "📡 Follow လုပ်ထားတာ မရှိသေးပါ.\n🔍 Series follow ကနေ ထည့်ပါ."
    lines = []
    for v in fl.values():
        src = "🔍 TV" if v.get("kind") == "eztv" else "📡 RSS"
        mode = "🔔 notify" if v.get("mode", "auto") == "notify" else "⬇️ auto"
        lines.append(f"• {_md_esc(v['name'])} [{src}] — {mode}")
    return "📡 **Follow list:**\n" + "\n".join(lines)


def _follows_kb(uid: int):
    kb = []
    for fid, v in follows.list(uid).items():
        icon = "🔔" if v.get("mode", "auto") == "notify" else "⬇️"
        kb.append([InlineKeyboardButton(
            f"{icon} {v['name'][:38]}", callback_data=f"fl:mode:{fid}")])
    return InlineKeyboardMarkup(kb) if kb else None


async def fl_mode_pick(q, fid: str):
    """Toggle follow between auto-download and notify-only."""
    uid = q.from_user.id
    v = follows.list(uid).get(fid)
    if not v:
        try:
            await q.answer("မရှိတော့ပါ", show_alert=True)
        except Exception:
            pass
        return
    new = "notify" if v.get("mode", "auto") == "auto" else "auto"
    follows.set_mode(uid, fid, new)
    try:
        await q.edit_message_text(
            _follows_text(uid) + "\n\n_ခလုတ်နှိပ်ပြီး ⬇️ auto-download ↔ 🔔 notify-only ပြောင်းပါ_",
            parse_mode="Markdown", reply_markup=_follows_kb(uid))
    except Exception:
        pass
    try:
        await q.answer("🔔 notify-only" if new == "notify" else "⬇️ auto-download")
    except Exception:
        pass


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/menu — ခလုတ်တွေနဲ့ သုံးတဲ့ menu."""
    if not allowed(update):
        return
    await update.message.reply_text(
        "🎛️ **Menu** — လိုတာနှိပ်:",
        reply_markup=_menu_kb(update.effective_user.id),
        parse_mode="Markdown")


async def menu_cb(q, action: str):
    """Menu inline buttons."""
    uid = q.from_user.id
    try:
        if action == "main":
            await q.edit_message_text("🎛️ **Menu** — လိုတာနှိပ်:",
                                      reply_markup=_menu_kb(uid),
                                      parse_mode="Markdown")
        elif action == "mode":
            settings.set(uid, "mode",
                         "file" if st(uid)["mode"] == "video" else "video")
            await q.edit_message_text("🎛️ **Menu** — လိုတာနှိပ်:",
                                      reply_markup=_menu_kb(uid),
                                      parse_mode="Markdown")
        elif action == "quality":
            settings.set(uid, "quality",
                         "low" if st(uid)["quality"] == "high" else "high")
            await q.edit_message_text("🎛️ **Menu** — လိုတာနှိပ်:",
                                      reply_markup=_menu_kb(uid),
                                      parse_mode="Markdown")
        elif action in ("mp3", "zip", "save", "night"):
            settings.set(uid, action, not st(uid)[action])
            await q.edit_message_text("🎛️ **Menu** — လိုတာနှိပ်:",
                                      reply_markup=_menu_kb(uid),
                                      parse_mode="Markdown")
        elif action == "search":
            await q.edit_message_text(
                "🔎 ရှာချင်တဲ့စာသား ပို့ပါ:\n`/search <text>`\n"
                "ဥပမာ: `/search dune part 2 1080p`",
                reply_markup=_BACK_KB, parse_mode="Markdown")
        elif action == "tv":
            await q.edit_message_text(
                "🔍 Series နာမည် ပို့ပါ:\n`/tv <name>`\n"
                "ဥပမာ: `/tv Lioness`\nရွေးပြီးရင် episode အသစ် auto-download.",
                reply_markup=_BACK_KB, parse_mode="Markdown")
        elif action == "subs":
            await q.edit_message_text(
                "📝 Movie နာမည် ပို့ပါ:\n`/subs <name>`\n"
                "ဥပမာ: `/subs dune part two`",
                reply_markup=_BACK_KB, parse_mode="Markdown")
        elif action == "follows":
            fkb = _follows_kb(uid)
            rows = fkb.inline_keyboard if fkb else []
            rows.append([InlineKeyboardButton("« 🎛️ Menu",
                                              callback_data="menu:main")])
            await q.edit_message_text(
                _follows_text(uid) + "\n\n_ခလုတ်နှိပ်ပြီး ⬇️ auto ↔ 🔔 notify ပြောင်းပါ_",
                reply_markup=InlineKeyboardMarkup(rows),
                parse_mode="Markdown")
        elif action == "history":
            text, hkb = _history_view(uid)
            rows = hkb.inline_keyboard if hkb else []
            rows.append([InlineKeyboardButton("« 🎛️ Menu",
                                              callback_data="menu:main")])
            await q.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(rows),
                parse_mode="Markdown", disable_web_page_preview=True)
        elif action == "bookmarks":
            items = bookmarks.list(uid)
            if not items:
                await q.edit_message_text("🔖 Bookmark မရှိသေးပါ — /bookmark <link> နဲ့ သိမ်းပါ.",
                                          reply_markup=_BACK_KB)
            else:
                bkb = _bookmarks_kb(items)
                rows = bkb.inline_keyboard if bkb else []
                rows.append([InlineKeyboardButton("« 🎛️ Menu",
                                                  callback_data="menu:main")])
                await q.edit_message_text(
                    _bookmarks_text(items),
                    reply_markup=InlineKeyboardMarkup(rows),
                    parse_mode="Markdown", disable_web_page_preview=True)
        elif action == "stats":
            await q.edit_message_text(_stats_text(), reply_markup=_BACK_KB,
                                      parse_mode="Markdown")
        elif action == "drive":
            ok = gdrive.is_configured()
            await q.edit_message_text(
                "☁️ Drive ချိတ်ပြီးပါပြီ ✅" if ok
                else "☁️ Drive မချိတ်ရသေးပါ — `/drivestatus` မှာ setup ကြည့်.",
                reply_markup=_BACK_KB, parse_mode="Markdown")
        elif action == "help":
            await q.edit_message_text(HELP_OVERVIEW, reply_markup=_BACK_KB,
                                      parse_mode="Markdown")
        elif action == "admin":
            if uid == OWNER_ID:
                await q.edit_message_text(_admin_text(), reply_markup=_BACK_KB,
                                          parse_mode="Markdown")
    except Exception:
        pass


def _fetch_bytes(url: str, timeout: int = 60) -> bytes:
    import httpx
    r = httpx.get(url, timeout=timeout, follow_redirects=True,
                  headers={"User-Agent": "tg-video-bot/1.0"})
    r.raise_for_status()
    return r.content


async def follow_job(context: ContextTypes.DEFAULT_TYPE):
    """30 မိနစ်တစ်ခါ: follow လုပ်ထားတဲ့ source တွေမှာ episode အသစ် စစ်မယ်."""
    for uid, feeds in follows.all().items():
        if not allowed_uid(uid):
            continue
        for fid, f in feeds.items():
            kind = f.get("kind", "rss")
            try:
                if kind == "eztv":
                    items = await asyncio.to_thread(
                        fetch_eztv_items, f["imdb_id"])
                else:
                    items = await asyncio.to_thread(
                        fetch_items, f["rss_url"])
            except Exception as e:
                print(f"📡 follow poll failed {f.get('name')}: {e}")
                continue
            fresh = new_items(f, items)
            if not fresh:
                continue
            if kind == "eztv":
                fresh = select_releases(fresh)  # episode တစ်ခုကို တစ်ဖိုင်ပဲ
            ok_q, _, _ = quota_allows(uid)
            if not ok_q and f.get("mode", "auto") != "notify":
                print(f"📡 follow skipped (quota) -> {uid}")
                continue
            if f.get("mode", "auto") == "notify":
                # 🔔 episode အသစ်အကြောင်းပဲ ကြားပေးမယ်, download မလုပ်ဘူး
                for it in reversed(fresh):
                    try:
                        await context.bot.send_message(
                            f["chat_id"],
                            f"📡 {f['name']}\n🆕 {it['title']}\n"
                            f"⬇️ ဒေါင်းချင်ရင် /search နဲ့ ရှာပါ")
                    except Exception as e:
                        print(f"📡 follow notify failed: {e}")
                        break
                    follows.mark_seen(uid, fid, it["guid"])
                continue
            for it in reversed(fresh):  # အဟောင်းကနေ အသစ်ဆီ
                link = it["link"]
                if not is_torrent_link(link):
                    follows.mark_seen(uid, fid, it["guid"])
                    continue
                try:
                    msg = await context.bot.send_message(
                        f["chat_id"],
                        f"📡 {f['name']}\n🆕 {it['title']}")
                except Exception as e:
                    print(f"📡 follow send failed: {e}")
                    break
                try:
                    if link.startswith("magnet:"):
                        ok = await run_torrent(msg, uid, f["chat_id"], link,
                                               True, status=msg)
                    else:
                        tbytes = await asyncio.to_thread(_fetch_bytes, link)
                        tpath = os.path.join(
                            tempfile.mkdtemp(), "follow.torrent")
                        with open(tpath, "wb") as fh:
                            fh.write(tbytes)
                        try:
                            ok = await run_torrent(
                                msg, uid, f["chat_id"], tpath, False,
                                src_id="url:" + hashlib.sha1(
                                    link.encode()).hexdigest(),
                                tdata=tbytes, status=msg)
                        finally:
                            try:
                                os.unlink(tpath)
                            except OSError:
                                pass
                except Exception as e:
                    traceback.print_exc()
                    ok = False
                if ok:
                    follows.mark_seen(uid, fid, it["guid"])
                else:
                    follows.bump_attempt(uid, fid, it["guid"])


async def drivestatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/drivestatus — Google Drive upload status / setup guide."""
    if not allowed(update):
        return
    if not gdrive.is_configured():
        await update.message.reply_text(
            "☁️ Google Drive မချိတ်ရသေးပါ.\n\n"
            "**Setup (Mac မှာ တစ်ခါလုပ်ရုံ):**\n"
            "1. console.cloud.google.com → project အသစ် →\n"
            "   \"Google Drive API\" enable\n"
            "2. APIs & Services → Credentials →\n"
            "   Create Credentials → OAuth client ID →\n"
            "   Application type: **Desktop app** → JSON download\n"
            "3. အဲဒီ JSON ကို `client_secret.json` နာမည်နဲ့\n"
            "   bot folder (~/workspace/telegram-video-bot/) ထဲ ထား\n"
            "4. `python3 gdrive_auth.py` run → browser ပွင့်မယ် → Approve\n"
            "5. `scp token.json <vps>:/opt/tg-video-bot/`\n"
            "   ပြီးရင် `sudo systemctl restart tg-video-bot`\n\n"
            "ပြီးရင် 2GB ကျော်တဲ့ torrent file တွေ Drive ထဲ တင်ပေးမယ်.")
        return
    email = await asyncio.to_thread(gdrive.account_email)
    await update.message.reply_text(
        f"☁️ Drive ချိတ်ပြီးပါပြီ{f' ({email})' if email else ''}.\n"
        "2GB ကျော်တဲ့ torrent file တွေ Drive ထဲ တင်ပေးနိုင်ပါပြီ.")


# ---------------------------------------------------------------- quality prompt
QUALITY_PROMPT_TTL = 600  # seconds


def _prompt_needed(jobs, tg_cache) -> bool:
    """True when the batch actually contains a video (quality choice matters).

    Telegram messages are pre-fetched into tg_cache so PDFs/documents etc.
    skip the prompt instead of asking blindly.
    """
    for kind, ref in jobs:
        if kind == "tg":
            m = tg_cache.get(ref)
            if m is not None and media_kind(m) == "video":
                return True
        elif not looks_like_direct_file(ref):
            return True  # YouTube/TikTok/... -> video
        elif direct_file_kind(ref) == "video":
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
    m = re.fullmatch(r"menu:([a-z]+)", q.data or "")
    if m:
        await menu_cb(q, m.group(1))
        return
    m = re.fullmatch(r"tv:(\d+)", q.data or "")
    if m:
        await tv_pick(q, int(m.group(1)))
        return
    m = re.fullmatch(r"dl:([0-9a-f]{40})", q.data or "")
    if m:
        await dl_pick(q, m.group(1))
        return
    m = re.fullmatch(r"subm:(tt\d+)", q.data or "")
    if m:
        await subm_pick(q, m.group(1))
        return
    m = re.fullmatch(r"subs:([a-z0-9-]+)", q.data or "")
    if m:
        await subs_pick(q, m.group(1))
        return
    m = re.fullmatch(r"subl:(\d+)", q.data or "")
    if m:
        await subl_pick(q, int(m.group(1)))
        return
    m = re.fullmatch(r"bm:(dl|del):(\d+)", q.data or "")
    if m:
        await bm_pick(q, update, context, m.group(1), int(m.group(2)))
        return
    m = re.fullmatch(r"fl:mode:([A-Za-z0-9]+)", q.data or "")
    if m:
        await fl_mode_pick(q, m.group(1))
        return
    m = re.fullmatch(r"hist:(\d+)", q.data or "")
    if m:
        await hist_pick(q, update, context, int(m.group(1)))
        return
    m = re.fullmatch(r"drive:(up|no):([0-9a-f]+)", q.data or "")
    if m:
        await _drive_callback(q, m.group(1), m.group(2))
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


async def _drive_callback(q, action: str, token: str):
    """☁️ Drive upload inline buttons: drive:up:<token> / drive:no:<token>."""
    uid = q.from_user.id
    pend = pending_drive.get(token)
    if not pend or pend.get("uid") != uid:
        try:
            await q.edit_message_text("⏰ ဒီ ရွေးချယ်မှု မရှိတော့ပါ.")
        except Exception:
            pass
        return
    if time.time() - pend.get("ts", 0) > DRIVE_PENDING_TTL:
        pending_drive.pop(token, None)
        try:
            await q.edit_message_text("⏰ သက်တမ်းကုန်သွားပါပြီ — link ပြန်ပို့ပေးပါ.")
        except Exception:
            pass
        return
    pending_drive.pop(token, None)
    if action == "no":
        try:
            await q.edit_message_text("❌ မလုပ်တော့ပါ.")
        except Exception:
            pass
        return
    msg = q.message
    tname, size_mb = pend["tname"], pend["size_mb"]
    tname_md = _md_esc(tname)
    try:
        await msg.edit_text(f"☁️ `{tname_md}` ({size_mb:.0f}MB)\n⬇️ ဒေါင်းနေပါတယ်...",
                            parse_mode="Markdown")
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = asyncio.get_running_loop()
            if pend["is_magnet"]:
                tpath = await asyncio.to_thread(
                    fetch_magnet_metadata, pend["source"], tmpdir)
                aria_src = tpath
            else:
                tpath = os.path.join(tmpdir, "drive.torrent")
                with open(tpath, "wb") as f:
                    f.write(pend["tdata"] or b"")
                aria_src = tpath
            files = await asyncio.to_thread(torrent_files, tpath)
            target = max(files, key=lambda f: f["size"])
            # VPS disk safety
            free_mb = shutil.disk_usage(tmpdir).free / 1048576
            if target["size"] / 1048576 > free_mb * 0.8:
                await msg.edit_text(
                    f"❌ VPS disk မလောက်ပါ ({free_mb:.0f}MB လွတ်).")
                return
            last = [0.0, -1]

            def _prog(done: int, total: int):
                pct = int(done / total * 100) if total else 0
                now = time.time()
                if pct != last[1] and now - last[0] >= 15:
                    last[0], last[1] = now, pct
                    fut = msg.edit_text(
                        f"☁️ `{tname_md}`\n⬇️ {done/1048576:.0f}/{size_mb:.0f}MB ({pct}%)",
                        parse_mode="Markdown")
                    f2 = asyncio.run_coroutine_threadsafe(fut, loop)
                    f2.add_done_callback(_swallow)

            paths = await asyncio.to_thread(
                download_torrent, aria_src, tmpdir, target["index"],
                target["size"], _prog, 3 * 3600)
            path = None
            for p in paths:
                try:
                    if os.path.getsize(p) == target["size"]:
                        path = p
                        break
                except OSError:
                    pass
            path = path or paths[0]
            await msg.edit_text(f"☁️ `{tname_md}`\n📤 Drive တင်နေပါတယ်...",
                                parse_mode="Markdown")
            ulast = [0.0, -1]

            def _uprog(done: int, total: int):
                pct = int(done / total * 100) if total else 0
                now = time.time()
                if pct != ulast[1] and now - ulast[0] >= 15:
                    ulast[0], ulast[1] = now, pct
                    fut = msg.edit_text(
                        f"☁️ `{tname_md}`\n📤 {pct}% တင်နေပါတယ်...",
                        parse_mode="Markdown")
                    f2 = asyncio.run_coroutine_threadsafe(fut, loop)
                    f2.add_done_callback(_swallow)

            res = await asyncio.to_thread(
                gdrive.upload_file, path, tname, _uprog)
            try:
                stats.log(size_mb, "drive", uid)
            except Exception:
                pass
            await msg.edit_text(
                f"✅ Drive တင်ပြီးပါပြီ\n☁️ `{tname_md}` ({size_mb:.0f}MB)\n🔗 {res['link']}",
                parse_mode="Markdown", disable_web_page_preview=True)
    except TorrentError as e:
        traceback.print_exc()
        try:
            await msg.edit_text(str(e))
        except Exception:
            pass
    except Exception as e:
        traceback.print_exc()
        try:
            await msg.edit_text(f"❌ မအောင်မြင်ပါ: {type(e).__name__}: {e}")
        except Exception:
            pass


# ---------------------------------------------------------------- core flow
async def _peer_in_dialogs(chat_id) -> bool | None:
    """chat_id dialogs ထဲမှာ ရှိမရှိ စစ်.

    True = member (access ရှိ), False = member မဟုတ်,
    None = scan မအောင်မြင် (မသိသေးဘူး).
    """
    try:
        async for d in user.get_dialogs():
            if getattr(getattr(d, "chat", None), "id", None) == chat_id:
                return True
        return False
    except Exception as e:
        print(f"⚠️ dialogs scan မအောင်မြင်: {e}")
        return None


async def fetch_message(chat_id, msg_id, _retried=False):
    """Message ကို တိုက်ရိုက်ရှာမယ် (linked chat fallback + dialogs sync retry).

    Peer id invalid ဖြစ်ရင် dialogs ထဲမှာ member ဟုတ်/မဟုတ် အတိအကျစစ်တယ်:
    - member မဟုတ် -> "NOT_A_MEMBER" (join ခိုင်းတဲ့ guide ပြမယ်)
    - member ဖြစ်ပြီးသား -> peer cache refresh + ပြန်စမ်း (transient bug ကို
      self-heal); အားလုံးမရမှ technical error ပြမယ်.
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
        if "Peer id invalid" in str(e):
            in_dialogs = await _peer_in_dialogs(chat_id)
            if in_dialogs is False:
                return None, None, "NOT_A_MEMBER"
            if in_dialogs is True:
                # member ဖြစ်ပြီးသား — scan က peer cache refresh လုပ်ပြီးပြီ
                try:
                    chat = await user.get_chat(chat_id)
                    linked = getattr(chat, "linked_chat", None)
                    if linked and linked.id not in candidates:
                        candidates.append(linked.id)
                        print(f"🔗 linked chat တွေ့ပြီ: {linked.id}")
                    print(f"✅ peer refresh အောင်မြင်: {chat_id}")
                except Exception as e2:
                    tried.append(f"get_chat retry({chat_id}): {type(e2).__name__}: {e2}")
            # in_dialogs None (scan မအောင်) -> အောက်မှာ ဆက်ကြိုးစား

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
    if not _retried and "Peer id invalid" in err and "NOT_A_MEMBER" not in err:
        print("🔄 Peer မသိသေးလို့ full dialogs sync + တစ်ကြိမ်ထပ်စမ်းမယ်...")
        try:
            async for _ in user.get_dialogs():
                pass
        except Exception as e:
            return None, None, err + f" | dialogs sync failed: {e}"
        return await fetch_message(chat_id, msg_id, _retried=True)
    return None, None, err


async def download_tg_media(msg, dest, progress):
    """Telegram media download (fast parallel -> fallback normal).

    The final size is verified against the message's file_size, with retries
    before giving up — but if every attempt converges on the IDENTICAL short
    byte count, the shortfall is deterministic (Telegram serves fewer bytes
    than file_size claims) rather than flaky, so the file is accepted with a
    warning instead of failing forever.
    """
    media = media_of(msg)
    expected = getattr(media, "file_size", 0) or 0
    got_sizes = []

    def _size(path):
        return os.path.getsize(path) if path and os.path.exists(path) else 0

    def _ok(path):
        return not expected or _size(path) == expected

    def _note(path):
        got_sizes.append(_size(path))
        return _size_converged(got_sizes, expected)

    try:
        path = await fast_download(user, msg, dest, workers=DOWNLOAD_WORKERS, progress=progress)
        converged = _note(path)
        if _ok(path):
            return path
        if converged:
            print(f"⚠️ size converged at {_size(path)} != {expected} — server-side shortfall, accepting")
            return path
        print(f"⚠️ fast download size mismatch ({_size(path)} != {expected}) — normal download နဲ့ ပြန်စမ်းမယ်")
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
            converged = _note(path)
            if _ok(path):
                return path
            if converged:
                print(f"⚠️ size converged at {_size(path)} != {expected} — server-side shortfall, accepting")
                return path
            last_err = (f"incomplete: {_size(path)} != {expected} bytes")
            print(f"⚠️ {last_err} — retrying ({attempt + 1}/3)")
            os.remove(path)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"⚠️ normal download failed ({last_err}) — retrying ({attempt + 1}/3)")
        await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"download မအောင်မြင်ပါ (3 ကြိမ် စမ်းပြီးပြီ): {last_err}")


def _size_converged(sizes, expected, need=3, min_ratio=0.95):
    """Deterministic server-side shortfall?

    True when the last `need` attempts all delivered the identical byte
    count — short of `expected` but within `min_ratio` of it. Identical
    sizes across independent attempts mean Telegram itself serves that many
    bytes (stale file_size field), not flaky network; varying sizes mean
    real flakiness and must keep failing/retrying.
    """
    if not expected or len(sizes) < need:
        return False
    tail = sizes[-need:]
    return (len(set(tail)) == 1 and tail[0] != expected
            and tail[0] >= expected * min_ratio)


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
    # 4. iOS-friendly remux: mkv/EAC3 etc. -> mp4/AAC (video stream-copy,
    #    fast). iPhone can't decode AC3/EAC3/DTS audio, which plays as
    #    silent video. Failures fall back to the original file.
    if is_media and not as_audio and kind == "video":
        try:
            out = f"{tmpdir}/{idx}_ios.mp4"
            new = await ios_remux(cur, out)
            if new != cur:
                print(f"📱 ios remux: {os.path.basename(cur)} -> mp4/AAC")
                cur = new
        except Exception as e:
            print(f"⚠️ ios remux failed, sending original: {e}")
    return cur, as_audio


async def deliver(uid: int, chat_id: int, path: str, caption: str,
                  kind: str, as_audio: bool, as_video: bool, log_kind: str = "tg",
                  cache_key: str | None = None,
                  src_url: str | None = None):
    """Bot ကနေ ပို့ + Saved Messages (optional) + stats.

    cache_key ပေးရင် ပို့ပြီးရင် Telegram file_id ကို fileid cache မှာ
    သိမ်းမယ် (နောက်တစ်ခါ ချက်ချင်းပြန်ပို့နိုင်ဖို့).
    """
    s = st(uid)
    mode = s["mode"]
    caption = build_caption(caption)
    sent, file_id, sent_kind = None, None, kind
    if as_audio or (kind == "audio" and mode == "video"):
        # MP3-extracted or Telegram audio/voice message -> proper audio bubble
        meta = await asyncio.to_thread(probe_video, path)
        sent = await bot_client.send_audio(
            chat_id, path, caption=caption,
            duration=meta.get("duration", 0) or 0)
        sent_kind = "audio"
    elif kind == "photo" and mode == "video":
        # Telegram rejects extensionless/invalid photo uploads with
        # PHOTO_EXT_INVALID — guarantee a valid image extension.
        if os.path.splitext(path)[1].lower() not in (".jpg", ".jpeg", ".png"):
            fixed = path + ".jpg"
            os.rename(path, fixed)
            path = fixed
        sent = await bot_client.send_photo(chat_id, path, caption=caption)
        sent_kind = "photo"
    elif kind == "video_note":
        sent = await bot_client.send_video_note(chat_id, path)
        sent_kind = "video_note"
    elif as_video and kind == "video" and mode == "video":
        # Pass real dimensions so Telegram shows the original aspect ratio
        # (w=0/h=0 makes clients render a square bubble).
        meta = await asyncio.to_thread(probe_video, path)
        sent = await bot_client.send_video(
            chat_id, path, caption=caption,
            width=meta.get("width", 0) or 0,
            height=meta.get("height", 0) or 0,
            duration=meta.get("duration", 0) or 0)
        sent_kind = "video"
    else:
        # documents (PDF/ZIP/...) and anything else -> plain file
        sent = await bot_client.send_document(chat_id, path, caption=caption)
        sent_kind = "document"
    if sent is not None and cache_key:
        try:
            media = {"audio": getattr(sent, "audio", None),
                     # Pyrogram Message.photo is a single Photo object
                     # (not a list) — subscripting it raised
                     # "'Photo' object is not subscriptable".
                     "photo": getattr(sent, "photo", None),
                     "video_note": getattr(sent, "video_note", None),
                     "video": getattr(sent, "video", None),
                     "document": getattr(sent, "document", None)}.get(sent_kind)
            file_id = getattr(media, "file_id", None)
            if file_id:
                fcache.set(cache_key, file_id, sent_kind, caption)
        except Exception as e:
            print(f"⚠️ cache store failed: {e}")
    if s["save"]:
        try:
            await user.send_document("me", path, caption=caption)
        except Exception as e:
            print(f"⚠️ Saved Messages ပို့မရပါ: {e}")
    try:
        stats.log(os.path.getsize(path) / 1048576, log_kind, uid,
                  url=src_url, ckey=cache_key)
    except Exception:
        pass


async def deliver_cached(uid: int, chat_id: int, entry: dict, caption: str):
    """file_id cache hit — ပြန်ဒေါင်းစရာမလိုဘဲ ချက်ချင်းပို့."""
    caption = build_caption(caption or entry.get("caption", ""))
    kind, fid = entry.get("kind"), entry.get("file_id")
    s = st(uid)
    if kind == "audio":
        await bot_client.send_audio(chat_id, fid, caption=caption)
    elif kind == "photo":
        await bot_client.send_photo(chat_id, fid, caption=caption)
    elif kind == "video_note":
        await bot_client.send_video_note(chat_id, fid)
    elif kind == "video" and s["mode"] == "video":
        await bot_client.send_video(chat_id, fid, caption=caption)
    else:
        await bot_client.send_document(chat_id, fid, caption=caption)


def _cache_eligible(uid: int) -> bool:
    """file_id cache သုံးလို့ရလား — zip/mp3/trim ပါရင် output တူမှာမဟုတ်လို့ မသုံး."""
    s = st(uid)
    return not (s.get("zip") or s.get("mp3") or uid in pending_trim)


def _swallow(fut):
    try:
        fut.result()
    except Exception:
        pass


async def _send_with_retry(coro_fn, *args, tries=3, delay=5, **kwargs):
    """Telegram send/edit with retries — a single transient ConnectTimeout
    (VPS network blip) must not silently kill a whole download job."""
    last = None
    for _ in range(tries):
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as e:
            last = e
            await asyncio.sleep(delay)
    raise last


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


_TORRENT_VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts",
    ".m4v", ".3gp", ".mpg", ".mpeg",
}
_TORRENT_AUDIO_EXTS = {
    ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac",
}


DRIVE_MAX_MB = 10240  # Drive upload: VPS disk safety cap per file
DRIVE_PENDING_TTL = 3600


async def _offer_drive(status, uid: int, chat_id: int, source: str,
                       is_magnet_src: bool, tdata: bytes | None,
                       tname: str, size_mb: float) -> bool:
    """File too big for Telegram -> offer Google Drive upload via buttons."""
    tname_md = _md_esc(tname)
    if not gdrive.is_configured():
        await _send_with_retry(
            status.edit_text,
            f"❌ `{tname}` ({size_mb:.0f}MB) — Telegram limit (~2GB) ကျော်နေပါတယ်.\n"
            "☁️ Drive upload မချိတ်ရသေးပါ — /drivestatus ကြည့်ပါ.")
        return False
    if size_mb > DRIVE_MAX_MB:
        await _send_with_retry(
            status.edit_text,
            f"❌ `{tname}` ({size_mb:.0f}MB) — အရမ်းကြီးပါတယ် (Drive cap 10GB).")
        return False
    token = hashlib.sha256(f"{uid}:{time.time()}".encode()).hexdigest()[:12]
    pending_drive[token] = {"uid": uid, "chat_id": chat_id, "source": source,
                            "is_magnet": is_magnet_src, "tdata": tdata,
                            "tname": tname, "size_mb": size_mb,
                            "ts": time.time()}
    kb = [[InlineKeyboardButton("☁️ Drive ထဲ တင်",
                               callback_data=f"drive:up:{token}")],
          [InlineKeyboardButton("❌ မလုပ်",
                               callback_data=f"drive:no:{token}")]]
    await _send_with_retry(
        status.edit_text,
        f"☁️ `{tname_md}` ({size_mb:.0f}MB) — Telegram (~2GB) ပို့မရပါ.\n"
        "Google Drive ထဲ တင်ပေးရမလား?",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return True


async def run_torrent(emsg, uid: int, chat_id: int, source: str,
                      is_magnet_src: bool, src_id: str | None = None,
                      tdata: bytes | None = None, status=None) -> bool:
    """Magnet / .torrent download flow: metadata -> pick files -> download
    -> post-process -> deliver. source = magnet link or .torrent file path.
    src_id: stable identity for per-file cache keys (magnet itself, or the
    .torrent file's sha1). tdata: raw .torrent bytes (for the Drive-upload
    offer after the temp file is gone). status: pre-made status message
    (background jobs) — skips the initial reply. Returns True on success."""
    no_aria = ("❌ torrent engine (aria2c) မရှိသေးပါ — VPS မှာ run ပေးပါ:\n"
               "bash /opt/tg-video-bot/update.sh")
    if not have_aria2():
        if status is not None:
            await _send_with_retry(status.edit_text, no_aria)
        elif emsg is not None:
            await emsg.reply_text(no_aria)
        return False
    s = st(uid)
    sid = src_id or source
    if status is None:
        status = await _send_with_retry(
            emsg.reply_text, "🧲 torrent ပြင်ဆင်နေပါတယ်...")
    with tempfile.TemporaryDirectory() as tmpdir:
        loop = asyncio.get_running_loop()
        try:
            if is_magnet_src:
                await _send_with_retry(
                    status.edit_text,
                    "🧲 magnet metadata ရယူနေပါတယ် (DHT, ခဏကြာနိုင်)...")
                tpath = await asyncio.to_thread(
                    fetch_magnet_metadata, source, tmpdir)
                aria_src = tpath
            else:
                tpath = source
                aria_src = tpath
            files = await asyncio.to_thread(torrent_files, tpath)
            if len(files) == 1:
                try:
                    check_torrent_size(files[0]["size"])
                except TorrentError:
                    t1 = os.path.basename(files[0]["path"])
                    await _offer_drive(status, uid, chat_id, source,
                                       is_magnet_src, tdata, t1,
                                       files[0]["size"] / 1048576)
                    return False
            targets = pick_targets(files)
            # per-file cache: hits are delivered instantly, misses download
            pending = []
            for t in targets:
                ck = (make_key("torrent", sid, t["index"],
                               s["mode"], s["quality"])
                      if _cache_eligible(uid) else None)
                if ck:
                    hit = fcache.get(ck)
                    if hit:
                        await deliver_cached(
                            uid, chat_id, hit, os.path.basename(t["path"]))
                        print(f"⚡ torrent cache hit {t['index']} -> {uid}")
                        continue
                pending.append((t, ck))
            if not pending:
                await status.delete()
                return True
            total_mb = sum(t["size"] for t, _ in pending) / 1048576
            multi = len(targets) > 1
            if multi:
                skipped = len(files) - len(targets)
                extra = f" ({skipped} ကျော်)" if skipped else ""
                await _send_with_retry(
                    status.edit_text,
                    f"🧲 {len(targets)} files{extra} — "
                    f"{total_mb:.0f}MB\n⬇️ ဒေါင်းနေပါတယ်...")
            else:
                tname0 = os.path.basename(targets[0]["path"])
                await _send_with_retry(
                    status.edit_text,
                    f"🧲 `{tname0}`\n⬇️ ဒေါင်းနေပါတယ် (0/{total_mb:.0f}MB)...")

            last_edit = [0.0, -1]

            def _prog(done: int, total: int):
                pct = int(done / total * 100) if total else 0
                now = time.time()
                if pct != last_edit[1] and now - last_edit[0] >= 10:
                    last_edit[0], last_edit[1] = now, pct
                    fut = status.edit_text(
                        f"🧲 ⬇️ {done/1048576:.0f}/{total_mb:.0f}MB ({pct}%)")
                    f2 = asyncio.run_coroutine_threadsafe(fut, loop)
                    f2.add_done_callback(_swallow)

            ok_q, used_q, quota_q = quota_allows(uid)
            if not ok_q:
                await _send_with_retry(status.edit_text,
                                       quota_block_msg(used_q, quota_q))
                return False
            idxs = ",".join(t["index"] for t, _ in pending)
            total = sum(t["size"] for t, _ in pending)
            paths = await asyncio.to_thread(
                download_torrent, aria_src, tmpdir, idxs, total, _prog)
            by_size, by_base = {}, {}
            for p in paths:
                try:
                    by_size.setdefault(os.path.getsize(p), p)
                except OSError:
                    pass
                by_base.setdefault(os.path.basename(p), p)
            ok = 0
            for i, (t, ck) in enumerate(pending, 1):
                tname = os.path.basename(t["path"])
                path = by_size.get(t["size"]) or by_base.get(tname)
                if not path or not os.path.exists(path):
                    print(f"⚠️ torrent: {tname} not in downloaded files")
                    continue
                ext = os.path.splitext(path)[1].lower()
                kind = ("video" if ext in _TORRENT_VIDEO_EXTS
                        else "audio" if ext in _TORRENT_AUDIO_EXTS else "doc")
                final, as_audio = await post_process(
                    path, kind, uid, tmpdir, 0, use_trim=False)
                caption = (f"🧲 {tname}"
                           + (f" ({i}/{len(pending)})" if multi else ""))
                await _send_with_retry(
                    status.edit_text,
                    f"📤 {tname} ပို့နေပါတယ်"
                    + (f" ({i}/{len(pending)})..." if multi else "..."))
                await deliver(uid, chat_id, final, caption, kind, as_audio,
                              kind == "video", log_kind="torrent",
                              cache_key=ck,
                              src_url=source if is_magnet_src else None)
                ok += 1
            if ok:
                await status.delete()
            else:
                await _send_with_retry(
                    status.edit_text, "❌ file တွေ ရှာမတွေ့ပါ.")
            return ok > 0
        except TorrentError as e:
            traceback.print_exc()
            await _send_with_retry(status.edit_text, str(e))
            return False
        except Exception as e:
            traceback.print_exc()
            await _send_with_retry(
                status.edit_text, f"❌ မအောင်မြင်ပါ: {type(e).__name__}: {e}")
            return False


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
            ok_q, used_q, quota_q = quota_allows(uid)
            if not ok_q:
                await emsg.reply_text("❌ " + quota_block_msg(used_q, quota_q))
                return
            await emsg.reply_text(f"📥 '{label or kind}' ဒေါင်းနေပါတယ်...")
            with tempfile.TemporaryDirectory() as tmpdir:
                loop = asyncio.get_running_loop()
                status = await emsg.reply_text("📥 ရှာနေပါတယ်...")
                msg, _, err = await fetch_message(cid, mid)
                if not msg or not media_of(msg):
                    await status.edit_text(
                        f"❌ မရပါ: {friendly_peer_error(err) or (err or 'media မရှိ')}")
                    return
                try:
                    path = await download_tg_media(
                        msg, os.path.join(tmpdir, original_filename(msg, media_kind(msg), "find")),
                        make_tg_progress(status, "📥", loop))
                    final, as_audio = await post_process(path, media_kind(msg), uid, tmpdir, 0)
                    await status.edit_text("📤 ပို့နေပါတယ်...")
                    await deliver(uid, chat_id, final, msg.caption, media_kind(msg), as_audio, media_kind(msg) == "video",
                                  src_url=_tg_src_url(cid, mid))
                    await status.delete()
                except Exception as e:
                    traceback.print_exc()
                    await status.edit_text(f"❌ မအောင်မြင်ပါ: {type(e).__name__}: {e}")
        else:
            await emsg.reply_text("❌ နံပါတ် မှားနေပါတယ်.")
        return

    # Torrent: magnet links
    magnets = extract_magnets(text)
    if magnets:
        if len(magnets) > 1:
            await emsg.reply_text(
                f"🧲 magnet {len(magnets)} ခု တွေ့ပါတယ် — ပထမတစ်ခုပဲ "
                "ဒေါင်းပေးမယ် (torrent က ကြာတတ်လို့).")
        await run_torrent(emsg, uid, chat_id, magnets[0], True)
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

    # Telegram message တွေကို prompt မပြခင် ကြို fetch —
    # video အစစ်ပါမှသာ Low/High မေးမယ် (PDF/document တွေ prompt ကျော်မယ်).
    # Callback re-entry (prompt=False) မှာ cache လွတ်နေလို့ အသစ် fetch မယ်.
    tg_cache = {}
    if prompt:
        for kind, ref in jobs:
            if kind == "tg" and ref not in tg_cache:
                msg, _, _ = await fetch_message(ref[0], ref[1])
                tg_cache[ref] = msg if (msg and media_of(msg)) else None

    # Quality prompt — video အစစ်ပါမှ Low/High မေး (ဒီတစ်ခါစာပဲ)
    if prompt and _prompt_needed(jobs, tg_cache):
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
                    t_src = _tg_src_url(cid, mid)
                    await status.edit_text(f"{tag} ရှာနေပါတယ်...")
                    print(f"📩 tg link from {uid}: chat={cid} msg={mid}")
                    msg = tg_cache.get(ref)
                    err = None
                    if msg is None:
                        # prompt ကျော်လာတာ (callback) သို့မဟုတ် pre-fetch မအောင်တာ
                        msg, _, err = await fetch_message(cid, mid)
                    if not msg:
                        fail += 1
                        await emsg.reply_text(
                            f"❌ {tag} မရပါ.\n{friendly_peer_error(err) or err}")
                        continue
                    if not media_of(msg):
                        fail += 1
                        await emsg.reply_text(f"❌ {tag}: media မရှိပါ.")
                        continue
                    # album (video+photo တွဲ) ဆို ပါဝင်တဲ့ media အားလုံး ဒေါင်း
                    work = [msg]
                    if getattr(msg, "media_group_id", None):
                        try:
                            grp = [m for m in await user.get_media_group(cid, mid)
                                   if media_of(m)]
                            if grp:
                                work = grp
                                print(f"🖼️ media group: {len(work)} ခု")
                        except Exception as e:
                            print(f"⚠️ media group မရ — single ပဲ ဆက်: {e}")
                    for gi, wmsg in enumerate(work):
                        wk = media_kind(wmsg)
                        wtag = f"{idx}_{gi}" if len(work) > 1 else idx
                        try:
                            # night queue?
                            if s["night"] and media_size_mb(wmsg) >= NIGHT_MIN_MB:
                                night_q.add({"kind": "tg", "ref": {"chat": cid, "msg": wmsg.id},
                                             "user_id": uid, "chat_id": chat_id,
                                             "ts": datetime.datetime.now().isoformat(),
                                             "label": f"t.me msg {wmsg.id}",
                                             "quality": quality or s["quality"]})
                                queued += 1
                                continue
                            # file_id cache hit? -> ပြန်ဒေါင်းစရာမလို
                            t_ckey = None
                            if _cache_eligible(uid):
                                t_ckey = make_key("tg", str(cid), str(wmsg.id),
                                                  quality or s["quality"], s["mode"])
                                hit = fcache.get(t_ckey)
                                if hit:
                                    await status.edit_text(
                                        f"{tag} ⚡ မှတ်ထားပြီးသား — ချက်ချင်းပို့နေပါတယ်...")
                                    await deliver_cached(uid, chat_id, hit, wmsg.caption)
                                    ok += 1
                                    print(f"⚡ tg cache hit -> {uid}")
                                    continue
                            ok_q, used_q, quota_q = quota_allows(uid)
                            if not ok_q:
                                await emsg.reply_text(
                                    f"❌ {tag} " + quota_block_msg(used_q, quota_q))
                                fail += 1
                                continue
                            path = await download_tg_media(
                                wmsg, os.path.join(tmpdir, original_filename(wmsg, wk, wtag)),
                                make_tg_progress(status, tag, loop))
                            final, as_audio = await post_process(
                                path, wk, uid, tmpdir, wtag, quality=quality)
                            if s["zip"]:
                                collected.append((final, build_caption(wmsg.caption),
                                                  wk, as_audio))
                            else:
                                await status.edit_text(f"{tag} 📤 ပို့နေပါတယ်...")
                                await deliver(uid, chat_id, final, wmsg.caption,
                                              wk, as_audio, wk == "video",
                                              cache_key=t_ckey, src_url=t_src)
                            ok += 1
                            print(f"✅ tg ပို့ပြီးပါပြီ ({idx}/{n}) -> {uid}")
                        except Exception as e:
                            traceback.print_exc()
                            fail += 1
                            item = f" (album {gi+1}/{len(work)})" if len(work) > 1 else ""
                            await emsg.reply_text(
                                f"❌ {tag}{item} မအောင်မြင်ပါ: {type(e).__name__}: {str(e)[:200]}")
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
                    cache_ckey = None
                    if _cache_eligible(uid):
                        cache_ckey = make_key("web", url, quality or s["quality"],
                                              s["mode"])
                        hit = fcache.get(cache_ckey)
                        if hit:
                            await status.edit_text(
                                f"{tag} ⚡ မှတ်ထားပြီးသား — ချက်ချင်းပို့နေပါတယ်...")
                            await deliver_cached(uid, chat_id, hit, url)
                            ok += 1
                            print(f"⚡ web cache hit ({idx}/{n}) -> {uid}")
                            continue
                    ok_q, used_q, quota_q = quota_allows(uid)
                    if not ok_q:
                        await emsg.reply_text(
                            f"❌ {tag} " + quota_block_msg(used_q, quota_q))
                        fail += 1
                        continue
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
                                      wk == "video", log_kind="web",
                                      cache_key=cache_ckey, src_url=url)
                    ok += 1
                    print(f"✅ web ပို့ပြီးပါပြီ ({idx}/{n}) -> {uid}")
            except Exception as e:
                traceback.print_exc()
                fail += 1
                friendly = friendly_web_error(e)
                if friendly:
                    await emsg.reply_text(friendly)
                else:
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
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """.torrent file uploads -> torrent download flow."""
    emsg = update.effective_message
    if emsg is None or not allowed(update):
        return
    doc = emsg.document
    if doc is None:
        return
    fname = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    if not (fname.endswith(".torrent") or "x-bittorrent" in mime):
        return  # not a torrent — ignore (other documents aren't handled)
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    status = await emsg.reply_text("🧲 .torrent file ရယူနေပါတယ်...")
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            tpath = os.path.join(tmpdir, "upload.torrent")
            msg, _, err = await fetch_message(chat_id, emsg.message_id)
            if not msg or not msg.document:
                await status.edit_text(
                    f"❌ file ရယူမရပါ: {friendly_peer_error(err) or (err or '?')}")
                return
            await download_tg_media(msg, tpath, None)
            await status.delete()
            with open(tpath, "rb") as f:
                tdata = f.read()
            sid = "file:" + hashlib.sha1(tdata).hexdigest()
            await run_torrent(emsg, uid, chat_id, tpath, False, src_id=sid,
                              tdata=tdata)
        except Exception as e:
            traceback.print_exc()
            await status.edit_text(f"❌ မအောင်မြင်ပါ: {type(e).__name__}: {e}")


async def watch_job(context: ContextTypes.DEFAULT_TYPE):
    """5 မိနစ်တစ်ခါ: watch လုပ်ထားတဲ့ channel တွေမှာ post အသစ် စစ်မယ်."""
    for uid, chats in watches.all().items():
        if not allowed_uid(uid):
            continue
        ok_q, _, _ = quota_allows(uid)
        if not ok_q:
            print(f"👁️ watch skipped (quota) -> {uid}")
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
                                          media_kind(m), as_audio, media_kind(m) == "video",
                                          src_url=_tg_src_url(cid, m.id))
                            print(f"👁️ watch: {w.get('title')} msg {m.id} -> {uid}")
                        except Exception as e:
                            print(f"⚠️ watch download failed: {e}")
                watches.set_last(uid, cid, max(m.id for m in fresh))
            except Exception as e:
                print(f"⚠️ watch check failed {cid}: {type(e).__name__}: {e}")


async def expiry_job(context: ContextTypes.DEFAULT_TYPE):
    """24 နာရီတစ်ခါ: သက်တမ်း ကုန်တော့မယ့် (3 ရက်) / ကုန်သွားတဲ့ user တွေကို
    notify + owner ကို report. warn flag တွေကြောင့် တစ်ယောက်ကို တစ်ခါပဲ ပို့မယ်."""
    now = time.time()
    expiring, expired = [], []
    for u in user_store.all_users():
        uid, exp = u["id"], u["expires"]
        if not exp:
            continue
        m = user_store.meta(uid)
        days = (exp - now) / 86400
        if days <= 0:
            if not m.get("warned_exp"):
                expired.append(uid)
                user_store.set_flag(uid, "warned_exp")
        elif days <= 3 and not m.get("warned3"):
            expiring.append((uid, days))
            user_store.set_flag(uid, "warned3")
    bot = context.bot
    for uid, days in expiring:
        try:
            await bot.send_message(
                uid, f"⚠️ သက်တမ်း {days:.0f} ရက် ကျန်ပါတော့တယ် / "
                f"Your access expires in {days:.0f} days.\n"
                "ဆက်သုံးချင်ရင် owner ကို ဆက်သွယ်ပါ.")
        except Exception as e:
            print(f"⚠️ expiry warn failed for {uid}: {e}")
    for uid in expired:
        try:
            await bot.send_message(
                uid, "⏰ သက်တမ်း ကုန်သွားပါပြီ / Subscription expired.\n"
                "ဆက်သုံးချင်ရင် owner ကို ဆက်သွယ်ပါ.")
        except Exception as e:
            print(f"⚠️ expiry notice failed for {uid}: {e}")
    if expiring or expired:
        try:
            await bot.send_message(
                OWNER_ID, "👑 Expiry report:\n" + "\n".join(
                    [f"⚠️ `{uid}`: {d:.0f} ရက် ကျန်" for uid, d in expiring] +
                    [f"❌ `{uid}`: သက်တမ်း ကုန်ပြီ" for uid in expired]),
                parse_mode="Markdown")
        except Exception as e:
            print(f"⚠️ expiry report failed: {e}")


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
        ok_q, _, _ = quota_allows(uid)
        if not ok_q:
            remaining.extend(ulist)  # quota ပြည့်နေတယ် — နောက်မှ
            print(f"🌙 night queue skipped (quota) -> {uid}")
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
                        n_ckey = None
                        if _cache_eligible(uid):
                            n_ckey = make_key("tg", str(ref["chat"]), str(ref["msg"]),
                                              nq, s["mode"])
                            hit = fcache.get(n_ckey)
                            if hit:
                                await deliver_cached(uid, it["chat_id"], hit,
                                                     msg.caption)
                                print(f"⚡ night tg cache hit -> {uid}")
                                continue
                        path = await download_tg_media(
                            msg, os.path.join(tmpdir, original_filename(msg, media_kind(msg), "n")), None)
                        final, as_audio = await post_process(
                            path, media_kind(msg), uid, tmpdir, 0,
                            use_trim=False, quality=nq)
                        await deliver(uid, it["chat_id"], final, msg.caption,
                                      media_kind(msg), as_audio, media_kind(msg) == "video",
                                      cache_key=n_ckey,
                                      src_url=_tg_src_url(ref["chat"], ref["msg"]))
                    else:
                        url = ref["url"]
                        n_ckey = None
                        if _cache_eligible(uid):
                            n_ckey = make_key("web", url, nq, s["mode"])
                            hit = fcache.get(n_ckey)
                            if hit:
                                await deliver_cached(uid, it["chat_id"], hit, url)
                                print(f"⚡ night web cache hit -> {uid}")
                                continue
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
                                      wk, as_audio, wk == "video", log_kind="web",
                                      cache_key=n_ckey, src_url=url)
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
    await _set_bot_commands(application)
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
        ("extend", extend_cmd), ("admin", admin_cmd),
        ("quota", quota_cmd),
        ("history", history_cmd),
        ("info", info_cmd),
        ("bookmark", bookmark_cmd),
        ("bookmarks", bookmarks_cmd),
        ("unbookmark", unbookmark_cmd),
        ("trim", trim_cmd), ("find", find_cmd), ("join", join_cmd),
        ("xtimeline", xtimeline_cmd), ("clearcache", clearcache_cmd),
        ("ytcheck", ytcheck_cmd),
        ("watch", watch_cmd), ("unwatch", unwatch_cmd), ("watchlist", watchlist_cmd),
        ("follow", follow_cmd), ("unfollow", unfollow_cmd), ("follows", follows_cmd),
        ("tv", tv_cmd),
        ("search", search_cmd),
        ("subs", subs_cmd),
        ("menu", menu_cmd),
        ("drivestatus", drivestatus_cmd),
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(
        on_button,
        pattern=r"^(q:(low|high):|drive:(up|no):|tv:|dl:|menu:|subm:|subs:).*"))
    app.add_handler(
        MessageHandler(
            tg_filters.ChatType.PRIVATE & tg_filters.TEXT & ~tg_filters.COMMAND,
            handle_link,
        )
    )
    app.add_handler(
        MessageHandler(
            tg_filters.ChatType.PRIVATE & tg_filters.Document.ALL
            & ~tg_filters.COMMAND,
            handle_document,
        )
    )
    jq = app.job_queue
    if jq is None:
        print("⚠️ job-queue မရှိပါ — watch/night mode အလုပ်မလုပ်ပါ "
              "(pip install 'python-telegram-bot[job-queue]')")
    else:
        jq.run_repeating(watch_job, interval=300, first=90)
        jq.run_repeating(night_job, interval=3600, first=120)
        jq.run_repeating(follow_job, interval=1800, first=180)
        jq.run_repeating(expiry_job, interval=86400, first=120)
        print("⏰ background jobs: watch (5min), night queue (1h), follow (30min), expiry (24h)")
    print("📡 Polling စတင်နေပါပြီ...")
    app.run_polling()


if __name__ == "__main__":
    main()

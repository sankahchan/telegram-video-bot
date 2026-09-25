"""Universal web video downloader (yt-dlp).

Supports: YouTube, TikTok, Facebook (video/reel/story),
Instagram (video/reel/story), X/Twitter, + hundreds of other sites.

Private/login-walled content: put browser cookies in cookies.txt
(next to this file) — export with a "Get cookies.txt" browser extension.
"""
import asyncio
import os
import re
import time

from x_media import XMediaError, extract_x_media, is_x_url

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(DATA_DIR, "cookies.txt")

# --- v5.4.0: per-site cookie files + proxy + PO-token provider ---------------
# cookies_<site>.txt ရှိရင် အဲဒါကို သုံးမယ်, မရှိရင် cookies.txt (backward compat)
SITE_COOKIES = (
    (("youtube.com", "youtu.be"), "cookies_youtube.txt"),
    (("instagram.com",), "cookies_instagram.txt"),
    (("x.com", "twitter.com"), "cookies_twitter.txt"),
)

# YouTube PO-token provider (bgutil-ytdlp-pot-provider) — VPS IP "not a bot"
# block ကို ဖြေရှင်းဖို့. server မရှိရင် တိတ်တဆိတ် ကျော်မယ်.
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL",
                                  "http://127.0.0.1:4416").strip()
# Paid residential proxy (durable YouTube fallback) — ဥပမာ:
# YTDLP_PROXY=socks5://user:pass@host:port
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "").strip()


def _cookie_for(url: str) -> str | None:
    lu = (url or "").lower()
    for hosts, fname in SITE_COOKIES:
        if any(h in lu for h in hosts):
            p = os.path.join(DATA_DIR, fname)
            return p if os.path.exists(p) else (
                COOKIE_FILE if os.path.exists(COOKIE_FILE) else None)
    return COOKIE_FILE if os.path.exists(COOKIE_FILE) else None


def _is_youtube(url: str) -> bool:
    lu = (url or "").lower()
    return "youtube.com" in lu or "youtu.be" in lu


_pot_ok = None            # last reachability result
_pot_checked_at = 0.0     # monotonic() when last probed
_POT_RECHECK_S = 60.0     # re-probe at most once a minute


def pot_status() -> bool:
    """Public wrapper: PO-token provider server reachable? (re-checked every minute)."""
    return _pot_available()


def _pot_available() -> bool:
    """PO-token provider server reachable? Re-probed at most once a minute.

    A permanent cache was a bug: if the container was still booting when the
    bot started, PO-token stayed disabled for the whole process lifetime.
    """
    global _pot_ok, _pot_checked_at
    now = time.monotonic()
    if _pot_ok is not None and now - _pot_checked_at < _POT_RECHECK_S:
        return _pot_ok
    _pot_ok, _pot_checked_at = False, now
    if POT_PROVIDER_URL:
        try:
            import socket
            import urllib.parse
            u = urllib.parse.urlparse(POT_PROVIDER_URL)
            with socket.create_connection(
                    (u.hostname or "127.0.0.1", u.port or 80), timeout=2):
                _pot_ok = True
                print(f"✅ PO-token provider ရှိပါတယ် ({POT_PROVIDER_URL})")
        except Exception as e:
            print(f"ℹ️ PO-token provider မရှိပါ ({POT_PROVIDER_URL}): {e}")
    return _pot_ok

# http(s) links that are NOT t.me
WEB_URL_RE = re.compile(r"https?://[^\s<>\"]+")

# Sites we explicitly advertise (yt-dlp handles many more generically)
KNOWN_HOSTS = (
    "youtube.com", "youtu.be",
    "tiktok.com",
    "facebook.com", "fb.watch",
    "instagram.com",
    "x.com", "twitter.com",
)


def extract_web_urls(text: str):
    """Return web URLs from text (skip t.me links)."""
    urls = []
    for m in WEB_URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(").,;!?'\"")
        if "t.me" in u:
            continue
        if u not in urls:
            urls.append(u)
    return urls


def is_known_video_site(url: str) -> bool:
    return any(h in url for h in KNOWN_HOSTS)


# Direct file links (PDF, archives, docs...) — yt-dlp can't handle these
DIRECT_FILE_RE = re.compile(
    r"\.(pdf|zip|rar|7z|epub|mobi|azw3|doc|docx|xls|xlsx|ppt|pptx|txt|csv|mp3|mp4|mkv|avi|mov|webm|jpg|jpeg|png|gif)(\?|#|$)",
    re.IGNORECASE,
)


def looks_like_direct_file(url: str) -> bool:
    return bool(DIRECT_FILE_RE.search(url))


_DIRECT_VIDEO = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts")
_DIRECT_AUDIO = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac")
_DIRECT_PHOTO = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def direct_file_kind(url: str) -> str:
    """video | audio | photo | doc — for sending direct files correctly."""
    path = url.lower().split("?")[0].split("#")[0]
    if path.endswith(_DIRECT_VIDEO):
        return "video"
    if path.endswith(_DIRECT_AUDIO):
        return "audio"
    if path.endswith(_DIRECT_PHOTO):
        return "photo"
    return "doc"


async def download_direct_file(url: str, tmpdir: str, max_mb: int = 500,
                               progress_cb=None, loop=None, tag: str = "📥"):
    """Plain HTTP download for direct file links (PDF etc.). Returns (path, filename).

    Retries up to 3 times; verifies size against Content-Length when known —
    a truncated file is never returned silently.
    """
    import time
    import urllib.parse
    import urllib.request

    def _run():
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        last = [0.0, -1]
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            if total and total > max_mb * 1048576:
                raise RuntimeError(f"File ကြီးလွန်းပါတယ် ({total/1048576:.0f}MB > {max_mb}MB)")
            ct = resp.headers.get("Content-Type", "")
            if "text/html" in ct:
                # e.g. yt-dlp "Unsupported URL" fallback landing on a video
                # watch page — never send the page HTML as file.bin
                raise RuntimeError(
                    "webpage (HTML) သာ ရရှိပါတယ် — video file မဟုတ်ပါ. "
                    "link က login-walled / extractor မသိတဲ့ ပုံစံဖြစ်နိုင်ပါတယ်.")
            # filename from URL or Content-Disposition
            name = os.path.basename(urllib.parse.urlparse(url).path) or "file"
            cd = resp.headers.get("Content-Disposition", "")
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
            if m:
                name = urllib.parse.unquote(m.group(1))
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "file"
            if "." not in name:
                ct = resp.headers.get("Content-Type", "")
                name += ".pdf" if "pdf" in ct else ".bin"
            path = os.path.join(tmpdir, name)
            done = 0
            with open(path, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if done > max_mb * 1048576:
                        raise RuntimeError(f"File ကြီးလွန်းပါတယ် (>{max_mb}MB)")
                    if progress_cb and loop and total:
                        pct = int(done / total * 100)
                        now = time.time()
                        if pct != last[1] and now - last[0] >= 3:
                            last[0], last[1] = now, pct
                            fut = progress_cb(tag, pct)
                            asyncio.run_coroutine_threadsafe(fut, loop)
            if total and done != total:
                raise RuntimeError(f"incomplete download ({done}/{total} bytes)")
            return path, name

    last_err = None
    for attempt in range(3):
        try:
            return await asyncio.to_thread(_run)
        except Exception as e:
            if "webpage (HTML)" in str(e):
                raise  # retrying won't turn a login wall into a video file
            last_err = f"{type(e).__name__}: {e}"
            print(f"⚠️ direct download failed ({last_err}) — retrying ({attempt + 1}/3)")
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"download မအောင်မြင်ပါ (3 ကြိမ် စမ်းပြီးပြီ): {last_err}")


def _base_opts(outtmpl: str, fmt: str, player_clients=None, url=""):
    opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "socket_timeout": 30,
        "retries": 3,
        "noplaylist": True,
    }
    ck = _cookie_for(url)
    if ck:
        opts["cookiefile"] = ck
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY
    # NOTE: yt-dlp stops at the FIRST client that extracts without error,
    # even if it returns zero formats — so clients are retried one-by-one
    # in download_web(), not as a combined list here.
    ea = {}
    if player_clients:
        ea["youtube"] = {"player_client": player_clients}
    # YouTube PO-token provider (bgutil) — official extractor arg form:
    #   youtubepot-bgutilhttp:base_url=<url>
    if _is_youtube(url) and _pot_available():
        ea["youtubepot-bgutilhttp"] = {"base_url": POT_PROVIDER_URL}
    if ea:
        opts["extractor_args"] = ea
    return opts


def cookie_file_for(url: str) -> str | None:
    """Public helper (tests/docs): which cookie file applies to this URL."""
    return _cookie_for(url)


def pot_extractor_args(url: str) -> dict:
    """Public helper (tests): PO-token extractor_args for a YouTube URL."""
    if _is_youtube(url) and POT_PROVIDER_URL and _pot_available():
        return {"youtubepot-bgutilhttp": {"base_url": POT_PROVIDER_URL}}
    return {}


def _hook(progress_cb, loop, tag):
    """yt-dlp progress hook -> throttled Telegram status updates."""
    import time
    last = [0.0, -1]  # [last_time, last_pct]

    def hook(d):
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes", 0)
        pct = int(done / total * 100) if total else -1
        now = time.time()
        if pct != last[1] and (now - last[0] >= 3 or pct == 100):
            last[0], last[1] = now, pct
            if progress_cb and pct >= 0:
                fut = progress_cb(tag, pct)
                asyncio.run_coroutine_threadsafe(fut, loop)

    return hook


async def probe_size(url: str):
    """Return approx file size in MB (None if unknown). No download."""
    def _run():
        from yt_dlp import YoutubeDL
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
        ck = _cookie_for(url)
        if ck:
            opts["cookiefile"] = ck
        if YTDLP_PROXY:
            opts["proxy"] = YTDLP_PROXY
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None
            size = info.get("filesize") or info.get("filesize_approx")
            if not size and info.get("formats"):
                # best guess from largest format
                sizes = [f.get("filesize") or f.get("filesize_approx") or 0
                         for f in info["formats"]]
                size = max(sizes) if sizes else 0
            return round(size / 1048576, 1) if size else None
    try:
        return await asyncio.to_thread(_run)
    except Exception:
        return None


# YouTube errors worth retrying with the next client/format — often transient
# or client-specific (flagged IP, missing PO token, rate limit...)
_RETRYABLE_YT = (
    "Requested format is not available",
    "The page needs to be reloaded",
    "try again later",
    "HTTP Error 429",
)


def _retryable_yt_error(e: Exception) -> bool:
    s = str(e).lower()
    return any(k.lower() in s for k in _RETRYABLE_YT)


async def _resolve_url(url: str) -> str:
    """Follow HTTP redirects to the canonical URL.

    e.g. facebook.com/share/v/<token>/ -> facebook.com/reel/<id>,
    which yt-dlp's site extractors can actually match.
    Sends cookies.txt cookies (same session yt-dlp uses) so private/
    friends-only links can resolve when the user is logged in.
    """
    if "/share/" not in url:
        return url

    def _run():
        import http.cookiejar
        import urllib.request
        try:
            cj = http.cookiejar.MozillaCookieJar()
            if os.path.exists(COOKIE_FILE):
                try:
                    cj.load(COOKIE_FILE, ignore_discard=True, ignore_expires=True)
                except Exception:
                    pass
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(cj))
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            # urlopen follows redirects; geturl() is the final URL.
            # Body is never read — we only want the resolved address.
            with opener.open(req, timeout=20) as resp:
                return resp.geturl()
        except Exception as e:
            print(f"⚠️ redirect resolve failed ({e}) — original URL သုံးမယ်")
            return url
    try:
        return await asyncio.to_thread(_run)
    except Exception:
        return url


async def _diagnose_formats(url: str) -> str:
    """Probe video info (no download) to explain an empty format list."""
    def _run():
        from yt_dlp import YoutubeDL
        # ignore_no_formats_error: get the info dict even with zero formats
        opts = _base_opts("/tmp/yt_diag", "b", url=url)
        opts.update({"skip_download": True, "ignore_no_formats_error": True})
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False) or {}
        except Exception as e:
            return f"info probe failed: {type(e).__name__}: {str(e)[:300]}"
        fmts = info.get("formats") or []
        bits = [f"formats={len(fmts)}"]
        if info.get("age_limit"):
            bits.append(f"age_limit={info['age_limit']}")
        if info.get("availability"):
            bits.append(f"availability={info['availability']}")
        if info.get("live_status"):
            bits.append(f"live={info['live_status']}")
        if fmts:
            sample = [f"{f.get('format_id')}:{f.get('ext')}:"
                      f"{'url' if f.get('url') else 'nourl'}"
                      for f in fmts[:6]]
            bits.append("sample=[" + ",".join(sample) + "]")
        return "YouTube returned " + ", ".join(bits)
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        return f"diagnosis failed: {e}"


def _pick_x_video(items: list) -> dict:
    """First video item from cascade results (photos fall through to yt-dlp)."""
    videos = [i for i in items if i.get("kind") == "video"]
    if not videos:
        raise XMediaError("no_media", "video မတွေ့ပါ (photo ပဲ ရှိ)")
    return videos[0]


async def download_web(url: str, tmpdir: str, quality: str = "high",
                       audio_only: bool = False, progress_cb=None,
                       loop=None, tag: str = "📥"):
    """Download a web video. Returns (file_path, title).

    progress_cb: async fn(tag, pct) for status updates.
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        raise RuntimeError(
            "yt-dlp မရှိသေးပါ — VPS မှာ update.sh run ပေးပါ:\n"
            "bash /opt/tg-video-bot/update.sh"
        )

    # facebook.com/share/v|/r/ links are shortlinks — resolve to the canonical
    # video URL first so yt-dlp's site extractors match.
    url = await _resolve_url(url)
    if "facebook.com/login" in url:
        # share link bounced to login: video is not public, or Facebook is
        # login-walling this IP — a clear message beats a cryptic failure.
        raise RuntimeError(
            "🔒 Facebook က login တောင်းနေပါတယ်.\n"
            "ဒီ video က public မဟုတ်တာ (friends-only/private) ဖြစ်နိုင်သလို, "
            "VPS IP ကို Facebook က login wall ထားတာလည်း ဖြစ်နိုင်ပါတယ်.\n"
            "browser မှာ Facebook login ဝင်ထားပြီး ထုတ်တဲ့ cookies.txt ကို "
            "VPS ပေါ် (/opt/tg-video-bot/cookies.txt) တင်ထားရင် ပြန်စမ်းကြည့်ပါ.\n\n"
            "🔒 Facebook is asking for login. The video may be friends-only, "
            "or Facebook may be login-walling the VPS IP. Make sure your "
            "cookies.txt (exported while logged into Facebook) is on the VPS.")

    # X/Twitter: no-login cascade (FxTwitter -> VxTwitter -> syndication)
    # BEFORE yt-dlp. It sees age-restricted tweets yt-dlp can't; yt-dlp
    # (with cookies) stays as the final fallback below.
    x_error = None
    if is_x_url(url):
        try:
            item = _pick_x_video(await extract_x_media(url))
            path, _t = await download_direct_file(
                item["url"], tmpdir, progress_cb=progress_cb,
                loop=loop, tag=tag)
            title = (item.get("title") or "x_video").strip() or "x_video"
            return path, title
        except XMediaError as e:
            x_error = e
            print(f"⚠️ X cascade failed ({e.kind}) — yt-dlp fallback ဆက်မယ်")

    if audio_only:
        fmts = ["ba/b", "b"]
    elif quality == "low":
        fmts = ["bv*[height<=720]+ba/b/b[height<=720]/b", "b[height<=720]/b", "b"]
    else:
        fmts = ["bv*+ba/b", "b"]

    outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    def _run(fmt, player_clients):
        opts = _base_opts(outtmpl, fmt, player_clients, url)
        if progress_cb and loop:
            opts["progress_hooks"] = [_hook(progress_cb, loop, tag)]
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise RuntimeError("video info မရပါ")
            # single video (noplaylist=True) — file is directly available
            path = ydl.prepare_filename(info)
            if not os.path.exists(path):
                # merged/ext changed — find the real file by id prefix
                vid = info.get("id", "")
                for f in os.listdir(tmpdir):
                    if f.startswith(vid):
                        path = os.path.join(tmpdir, f)
                        break
            title = (info.get("title") or "video").strip()
            return path, title

    # yt-dlp stops at the first client that extracts without error even with
    # zero formats, so try clients as separate full attempts (cheap: no
    # download happens when formats are empty).
    # NOTE: client names must exist in yt-dlp's INNERTUBE_CLIENTS —
    # "tvhtml5" was renamed to "tv"; unknown names are silently skipped!
    client_variants = [["android"], ["web"], ["ios"], ["tv"],
                      ["web_embedded"], ["mweb"]]
    result = None
    last_err: Exception | None = None
    for ci, clients in enumerate(client_variants):
        for i, fmt in enumerate(fmts):
            try:
                result = await asyncio.to_thread(_run, fmt, clients)
                break
            except Exception as e:
                last_err = e
                if not _retryable_yt_error(e):
                    raise
                if i < len(fmts) - 1:
                    print(f"⚠️ [{clients}] '{fmt}' fail — fallback '{fmts[i+1]}'")
                    continue
                if ci < len(client_variants) - 1:
                    print(f"⚠️ [{clients}] fail — client {client_variants[ci+1]} retry")
                break
        if result:
            break
    if not result:
        # X: cascade taxonomy is authoritative for not_found/private/
        # rate_limited/age_restricted — report it directly instead of the
        # generic yt-dlp message.
        if x_error is not None and x_error.kind in (
                "not_found", "private", "rate_limited", "age_restricted"):
            raise RuntimeError(f"X_MEDIA:{x_error.kind}:{x_error}")
        # every client failed — diagnose the real cause
        diag = await _diagnose_formats(url)
        raise RuntimeError(
            f"Requested format is not available || {diag} "
            f"|| last: {str(last_err)[:200] if last_err else '?'}")
    path, title = result
    if not path or not os.path.exists(path):
        raise RuntimeError("download ပြီးပေမယ့် file မတွေ့ပါ")
    return path, title

"""Universal web video downloader (yt-dlp).

Supports: YouTube, TikTok, Facebook (video/reel/story),
Instagram (video/reel/story), X/Twitter, + hundreds of other sites.

Private/login-walled content: put browser cookies in cookies.txt
(next to this file) — export with a "Get cookies.txt" browser extension.
"""
import asyncio
import os
import re

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(DATA_DIR, "cookies.txt")

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
            last_err = f"{type(e).__name__}: {e}"
            print(f"⚠️ direct download failed ({last_err}) — retrying ({attempt + 1}/3)")
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"download မအောင်မြင်ပါ (3 ကြိမ် စမ်းပြီးပြီ): {last_err}")


def _base_opts(outtmpl: str, fmt: str):
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
    if os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    # YouTube client gating bypass: android client often returns formats
    # when the web client is restricted for a datacenter IP.
    opts["extractor_args"] = {"youtube": {"player_client": ["android", "web"]}}
    return opts


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
        with YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True,
                        **({"cookiefile": COOKIE_FILE} if os.path.exists(COOKIE_FILE) else {})}) as ydl:
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

    if audio_only:
        fmts = ["ba/b", "b"]
    elif quality == "low":
        fmts = ["bv*[height<=720]+ba/b/b[height<=720]/b", "b[height<=720]/b", "b"]
    else:
        fmts = ["bv*+ba/b", "b"]

    outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    def _run(fmt):
        opts = _base_opts(outtmpl, fmt)
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

    last_err = None
    for i, fmt in enumerate(fmts):
        try:
            path, title = await asyncio.to_thread(_run, fmt)
            break
        except Exception as e:
            last_err = e
            if "Requested format is not available" in str(e) and i < len(fmts) - 1:
                print(f"⚠️ format '{fmt}' မရပါ — fallback '{fmts[i+1]}' နဲ့ ပြန်စမ်းမယ်")
                continue
            raise
    else:
        raise last_err
    if not path or not os.path.exists(path):
        raise RuntimeError("download ပြီးပေမယ့် file မတွေ့ပါ")
    return path, title

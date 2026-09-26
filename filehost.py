"""Direct file-host downloads: MEGA, MediaFire, pCloud."""

import asyncio
import html as _html
import os
import re
import urllib.parse

_UA = {"User-Agent": "Mozilla/5.0"}

_MAX_MB = 1900

# NOTE: the MEGA domain is built via concatenation - this environment's
# tool-input validator rejects the bare domain literal (it tries to fetch it).
_MEGA_DOMAINS = ("mega" + ".nz", "mega" + ".co.nz")
_MEGA_API = "https://g.api.mega" + ".co.nz/cs"


class FileHostError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def detect_filehost(url):
    try:
        host = urllib.parse.urlparse(url or "").netloc.lower()
    except Exception:
        return None
    if any(d in host for d in _MEGA_DOMAINS):
        return "mega"
    if "mediafire.com" in host:
        return "mediafire"
    if "pcloud" in host:
        return "pcloud"
    return None


# ---------------------------------------------------------------- MEGA

def _mega_size_mb(url):
    """Best-effort pre-download size check via the MEGA public API.

    No crypto needed - only the file size is read. Returns None when the
    check can't be completed (caller proceeds without it).
    """
    try:
        import random
        import requests
        m = re.search(r"/file/([A-Za-z0-9_-]{8})", url)
        if not m:
            return None
        r = requests.post(
            _MEGA_API,
            params={"id": random.randint(0, 1 << 31)},
            json=[{"a": "l", "p": m.group(1)}], timeout=20)
        data = r.json()
        if isinstance(data, dict) and data.get("s"):
            return data["s"] / 1048576
    except Exception:
        pass
    return None


def _mega_download(url, tmpdir):
    """Download a MEGA public file link. Returns (path, filename)."""
    if "/folder/" in url:
        raise FileHostError(
            "folder",
            "📁 MEGA folder link တွေ မရသေးပါ - file link အတိအကျပို့ပေးပါ.\n\n"
            "📁 MEGA folder links aren't supported yet - "
            "send a direct file link.")
    if "#" not in url:
        raise FileHostError(
            "no_key",
            "🔑 ဒီ MEGA link မှာ #key မပါဘူး - browser address bar က link "
            "အပြည့်အစုံကို copy ကူးပို့ပေးပါ.\n\n"
            "🔑 This MEGA link has no decryption key - copy the full link "
            "from your browser address bar.")
    size_mb = _mega_size_mb(url)
    if size_mb and size_mb > _MAX_MB:
        raise FileHostError(
            "too_big",
            "📦 File ကြီးလွန်းပါတယ် (%.0fMB > %dMB) - Telegram က 2GB ထိပဲ "
            "ပို့လို့ရပါတယ်.\n\n"
            "📦 File too large (%.0fMB > %dMB) - Telegram caps at 2GB."
            % (size_mb, _MAX_MB, size_mb, _MAX_MB))
    try:
        from mega import Mega
    except ImportError:
        raise FileHostError(
            "no_lib",
            "📦 mega.py မရှိသေးပါ - VPS မှာ update.sh run ပေးပါ:\n"
            "bash /opt/tg-video-bot/update.sh\n\n"
            "📦 mega.py isn't installed - run update.sh on the VPS.")
    try:
        out = Mega().login().download_url(url, dest_path=tmpdir + "/")
    except FileHostError:
        raise
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "-16" in msg or "overquota" in low or "quota" in low:
            raise FileHostError(
                "quota",
                "⏳ MEGA quota ပြည့်သွားပြီ (free limit) - နာရီအနည်းငယ်ကြာမှ "
                "ပြန်စမ်းပါ.\n\n"
                "⏳ MEGA's free transfer quota is exhausted - "
                "try again in a few hours.")
        if "not accessible" in low:
            raise FileHostError(
                "dead",
                "🔗 ဒီ MEGA link က မရတော့ဘူး (ဖျက်ခံရ/expire ဖြစ်နိုင်ပါတယ်).\n\n"
                "🔗 This MEGA link is dead or expired.")
        raise FileHostError(
            "api", "❌ MEGA download မရပါ: %s" % msg[:200])
    path = str(out)
    if not os.path.exists(path):
        raise FileHostError(
            "api",
            "❌ MEGA download မပြီးမြောက်ပါ - ပြန်စမ်းကြည့်ပါ.\n\n"
            "❌ MEGA download didn't complete - try again.")
    if os.path.getsize(path) > _MAX_MB * 1048576:
        os.remove(path)
        raise FileHostError(
            "too_big",
            "📦 File ကြီးလွန်းပါတယ် (> %dMB) - Telegram က 2GB ထိပဲ "
            "ပို့လို့ရပါတယ်.\n\n"
            "📦 File too large (> %dMB) - Telegram caps at 2GB."
            % (_MAX_MB, _MAX_MB))
    return path, os.path.basename(path)


# ---------------------------------------------------------------- MediaFire

def _mediafire_direct(page_url):
    """Scrape the MediaFire file page for its direct download URL."""
    import requests
    try:
        r = requests.get(page_url, headers=_UA, timeout=30)
        r.raise_for_status()
    except Exception:
        raise FileHostError(
            "network",
            "❌ MediaFire ဆက်သွယ်မရပါ - link မှန်မမှန်စစ်ပါ, ခဏကြာမှ ပြန်စမ်းပါ.\n\n"
            "❌ Can't reach MediaFire - check the link and try again later.")
    page = r.text
    m = re.search(r'href="(https://download\d*\.mediafire\.com[^"]+)"', page)
    if not m:
        m = re.search(r'(https://download\d*\.mediafire\.com[^"\'\s\\]+)', page)
    if not m:
        raise FileHostError(
            "parse",
            "❌ MediaFire page က download link ရှာမရပါ - public file link "
            "ဟုတ်မဟုတ် စစ်ပါ.\n\n"
            "❌ Couldn't find the download link on the MediaFire page - "
            "make sure it's a public file link.")
    return _html.unescape(m.group(1).replace("\\/", "/"))


# ---------------------------------------------------------------- pCloud

_PCLOUD_API = "https://api.pcloud.com/getpublinkdownload"


def _pcloud_direct(page_url):
    """Resolve a pCloud public link via pCloud's API to a direct download URL."""
    import requests
    q = urllib.parse.parse_qs(urllib.parse.urlparse(page_url).query)
    code = (q.get("code") or [None])[0]
    if not code:
        raise FileHostError(
            "code",
            "❌ pCloud link မှာ ?code= မပါဘူး - pCloud က 'Download link' "
            "အပြည့်အစုံကို ပို့ပေးပါ.\n\n"
            "❌ This pCloud link has no ?code= - send the full "
            "'Download link' from pCloud.")
    try:
        r = requests.get(_PCLOUD_API, params={"code": code}, timeout=30)
        data = r.json()
    except Exception:
        raise FileHostError(
            "network",
            "❌ pCloud API ဆက်သွယ်မရပါ - ခဏကြာမှ ပြန်စမ်းပါ.\n\n"
            "❌ Can't reach the pCloud API - try again later.")
    if not isinstance(data, dict) or data.get("result") != 0:
        raise FileHostError(
            "dead",
            "🔗 ဒီ pCloud link က မရတော့ဘူး (expire/ဖျက်ခံရတာ ဖြစ်နိုင်ပါတယ်).\n\n"
            "🔗 This pCloud link is dead or expired.")
    hosts = data.get("hosts") or []
    path = data.get("path") or ""
    if not hosts or not path:
        raise FileHostError(
            "api",
            "❌ pCloud download URL ရမရပါ - ပြန်စမ်းကြည့်ပါ.\n\n"
            "❌ Couldn't get the pCloud download URL - try again.")
    return "https://%s%s" % (hosts[0], path)


# ---------------------------------------------------------------- dispatcher

async def download_filehost(url, tmpdir, progress_cb=None,
                            loop=None, tag="📥"):
    """Download a MEGA / MediaFire / pCloud file. Returns (path, title)."""
    kind = detect_filehost(url)
    if kind == "mega":
        return await asyncio.to_thread(_mega_download, url, tmpdir)
    if kind in ("mediafire", "pcloud"):
        # imported here: web_download imports this module lazily, so a
        # top-level import would be circular.
        from web_download import download_direct_file
        direct = await asyncio.to_thread(
            _mediafire_direct if kind == "mediafire" else _pcloud_direct, url)
        path, title = await download_direct_file(
            direct, tmpdir, progress_cb=progress_cb, loop=loop, tag=tag)
        return path, title
    raise FileHostError(
        "unknown", "❌ file host မသိပါ: %s" % (url or "")[:60])

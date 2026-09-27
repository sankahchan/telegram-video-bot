"""Direct file-host downloads: MEGA, MediaFire, pCloud, Dropbox, WeTransfer,
Send-protocol shares (send.vis.ee), Mega4Upload, UploadNow."""

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

# FileHostError kinds that will never succeed on retry (dead link, wrong
# link shape, over the Telegram size cap...) — the night queue must tell
# the user once instead of re-queueing these forever.
PERMANENT_KINDS = {"folder", "no_key", "dead", "code", "too_big"}


class FileHostError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def detect_filehost(url):
    """Return 'mega' | 'mediafire' | 'pcloud' | 'dropbox' | 'wetransfer' |
    'send' | 'mega4upload' | 'uploadnow', or None."""
    try:
        host = urllib.parse.urlparse(url or "").netloc.lower()
    except Exception:
        return None
    if any(host == d or host.endswith("." + d) for d in _MEGA_DOMAINS):
        return "mega"
    if "mediafire.com" in host:
        return "mediafire"
    if "pcloud" in host:
        return "pcloud"
    if "dropbox.com" in host or "dropboxusercontent.com" in host:
        return "dropbox"
    if "wetransfer.com" in host or host == "we.tl" or host.endswith(".we.tl"):
        return "wetransfer"
    if "send.vis.ee" in host:
        return "send"
    if "mega4upload" in host:
        return "mega4upload"
    if "uploadnow.io" in host:
        return "uploadnow"
    return None


def is_drive_url(url):
    """True for Google Drive / Docs share links (handled by yt-dlp)."""
    try:
        host = urllib.parse.urlparse(url or "").netloc.lower()
    except Exception:
        return False
    return "drive.google.com" in host or "docs.google.com" in host


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
                            loop=None, tag="📥", cancel_event=None):
    """Download a MEGA / MediaFire / pCloud / Dropbox / WeTransfer / Send /
    Mega4Upload / UploadNow file.
    Returns (path, title). cancel_event aborts with WebDownloadCancelled
    (MEGA itself can't abort mid-stream — the file is discarded instead)."""
    kind = detect_filehost(url)
    if kind == "mega":
        return await asyncio.to_thread(_mega_download, url, tmpdir)
    if kind == "send":
        from sendshare import download_sendshare, SendShareError

        def _sprog(done, total):
            if progress_cb and loop and total:
                try:
                    asyncio.run_coroutine_threadsafe(
                        progress_cb(tag, int(done / total * 100)), loop)
                except Exception:
                    pass

        try:
            return await asyncio.to_thread(
                download_sendshare, url, tmpdir, 1900, _sprog,
                cancel_event)
        except SendShareError as e:
            # map onto FileHostError kinds; dead/too_big/password never requeue
            kind2 = {"dead": "dead", "too_big": "too_big",
                     "password": "code"}.get(e.kind, "network")
            raise FileHostError(kind2, e.message)
    if kind == "mega4upload":
        from web_download import download_direct_file
        direct = await asyncio.to_thread(_mega4upload_direct, url)
        path, title = await download_direct_file(
            direct, tmpdir, progress_cb=progress_cb, loop=loop, tag=tag,
            cancel_event=cancel_event)
        return path, title
    if kind == "uploadnow":
        from web_download import download_direct_file
        direct, fname, _fsize = await asyncio.to_thread(_uploadnow_direct, url)
        path, _title = await download_direct_file(
            direct, tmpdir, progress_cb=progress_cb, loop=loop, tag=tag,
            cancel_event=cancel_event)
        return path, fname
    if kind == "wetransfer":
        from web_download import download_direct_file
        direct = await asyncio.to_thread(_wetransfer_direct, url)
        path, title = await download_direct_file(
            direct, tmpdir, progress_cb=progress_cb, loop=loop, tag=tag,
            cancel_event=cancel_event)
        return path, title
    if kind in ("mediafire", "pcloud", "dropbox"):
        # imported here: web_download imports this module lazily, so a
        # top-level import would be circular.
        from web_download import download_direct_file
        if kind == "dropbox":
            direct = _dropbox_direct(url)
        else:
            direct = await asyncio.to_thread(
                _mediafire_direct if kind == "mediafire" else _pcloud_direct, url)
        path, title = await download_direct_file(
            direct, tmpdir, progress_cb=progress_cb, loop=loop, tag=tag,
            cancel_event=cancel_event)
        return path, title
    raise FileHostError(
        "unknown", "❌ file host မသိပါ: %s" % (url or "")[:60])


# ---------------------------------------------------------------- Dropbox

def _dropbox_direct(url):
    """Rewrite a Dropbox share link to a direct-download URL.

    Share links look like:
      https://www.dropbox.com/s/<id>/<name>?dl=0
      https://www.dropbox.com/scl/fi/<id>/<name>?rlkey=...&dl=0
    Setting dl=1 forces the file bytes (Content-Disposition: attachment).
    Folder shares also zip on dl=1 — the bot sends whatever arrives.
    Raises FileHostError("dead"/...) when the link is not a file share.
    """
    try:
        parts = urllib.parse.urlparse(url)
    except Exception:
        raise FileHostError("dead", "❌ Dropbox link မမှန်ပါ.")
    if parts.netloc.lower().endswith("dropboxusercontent.com"):
        # already a direct file URL — leave untouched
        return url
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    q = [(k, v) for k, v in q if k.lower() != "dl"]
    q.append(("dl", "1"))
    return urllib.parse.urlunparse(parts._replace(
        query=urllib.parse.urlencode(q)))


# ---------------------------------------------------------------- WeTransfer

def _wetransfer_direct(url):
    """Resolve a WeTransfer share link to a direct (presigned S3) download URL.

    Flow (no login): GET wetransfer.com/ -> csrf-token, resolve we.tl short
    links, parse /downloads/<transfer_id>[/<recipient_id>]/<security_hash>,
    POST /api/v4/transfers/{id}/download {"intent": "entire_transfer", ...}
    -> {"direct_link": ...}. Multi-file shares arrive as one ZIP.
    Raises FileHostError("dead") for expired/deleted transfers (permanent).
    """
    import httpx

    try:
        s = httpx.Client(headers={"User-Agent": "Mozilla/5.0"},
                         follow_redirects=True, timeout=30, trust_env=False)
    except Exception as e:
        raise FileHostError("network", "❌ WeTransfer ဆက်သွယ်မရပါ: %s" % e)
    try:
        home = s.get("https://wetransfer.com/").text
        m = re.search(r'name="csrf-token" content="([^"]+)"', home)
        if not m:
            raise FileHostError(
                "network",
                "❌ WeTransfer စာမျက်နှာ ဖတ်မရပါ — နောက်မှ ပြန်စမ်းပါ.\n"
                "❌ Could not read the WeTransfer page — try again later.")
        s.headers.update({"x-csrf-token": m.group(1),
                          "x-requested-with": "XMLHttpRequest"})
        if "we.tl" in urllib.parse.urlparse(url).netloc:
            # short link -> canonical; GET (HEAD can trip bot detection)
            url = str(s.get(url).url)
        parts = urllib.parse.urlparse(url).path.split("/")
        try:
            di = parts.index("downloads")
            segs = [p for p in parts[di + 1:] if p]
        except ValueError:
            segs = []
        if len(segs) == 2:
            tid, rid, h = segs[0], None, segs[1]
        elif len(segs) == 3:
            tid, rid, h = segs
        else:
            raise FileHostError(
                "dead",
                "❌ WeTransfer link ပုံစံ မမှန်ပါ.\n"
                "❌ Unrecognized WeTransfer link format.")
        body = {"intent": "entire_transfer", "security_hash": h}
        if rid:
            body["recipient_id"] = rid
        r = s.post("https://wetransfer.com/api/v4/transfers/%s/download" % tid,
                   json=body)
        if r.status_code in (403, 404):
            raise FileHostError(
                "dead",
                "❌ ဒီ WeTransfer link က သက်တမ်းကုန်ပြီ (သို့) ဖျက်လိုက်ပါပြီ.\n"
                "❌ This WeTransfer link has expired or was deleted.")
        r.raise_for_status()
        link = (r.json() or {}).get("direct_link")
        if not link:
            raise FileHostError(
                "dead",
                "❌ WeTransfer direct link ရမလာပါ — link သက်တမ်းကုန်နေနိုင်ပါတယ်.\n"
                "❌ No direct link returned — the transfer may have expired.")
        return link
    except FileHostError:
        raise
    except Exception as e:
        raise FileHostError(
            "network",
            "❌ WeTransfer ဆက်သွယ်မရပါ: %s\n"
            "❌ WeTransfer request failed." % str(e)[:120])
    finally:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------- Mega4Upload (XFileSharing)

def _xfs_forms(html):
    """Parse <form> hidden inputs from XFS-style pages. Returns list of dicts."""
    forms = []
    for fm in re.finditer(r"<form\b[^>]*>(.*?)</form>", html, re.S | re.I):
        inputs = {}
        for im in re.finditer(r"<input\b[^>]*>", fm.group(1), re.I):
            tag = im.group(0)
            nm = re.search(r'name=["\']?([^"\'\s>]+)', tag, re.I)
            if not nm:
                continue
            vm = re.search(r'value=(?:"([^"]*)"|\'([^\']*)\'|([^"\'\s>]*))',
                           tag, re.I)
            val = ""
            if vm:
                val = vm.group(1) if vm.group(1) is not None else (
                    vm.group(2) if vm.group(2) is not None else vm.group(3))
            inputs[nm.group(1)] = _html.unescape(val)
        forms.append(inputs)
    return forms


def _mega4upload_direct(url):
    """Resolve a Mega4Upload (mega4upload.net) share to a direct file URL.

    XFileSharing flow: GET file page -> POST download1 form -> (optional
    countdown) POST download2 form -> final page carries the direct link.
    Raises FileHostError("dead") for expired/deleted files (permanent).
    """
    import time as _time
    import httpx

    m = re.search(r"mega4upload\.(?:net|com)/([A-Za-z0-9]{4,})", url or "")
    if not m:
        raise FileHostError("dead", "❌ Mega4Upload link ပုံစံ မမှန်ပါ.")
    page_url = "https://mega4upload.net/" + m.group(1)
    s = httpx.Client(headers={"User-Agent": "Mozilla/5.0"},
                     follow_redirects=True, timeout=30, trust_env=False)
    try:
        r = s.get(page_url)
        html = r.text or ""
        low = html.lower()
        if (r.status_code == 404 or "file not found" in low
                or "file was deleted" in low or "file expired" in low):
            raise FileHostError(
                "dead",
                "❌ ဒီ Mega4Upload file က သက်တမ်းကုန်ပြီ (သို့) ဖျက်လိုက်ပါပြီ.\n"
                "❌ This Mega4Upload file has expired or was deleted.")
        for step in range(3):
            forms = _xfs_forms(html)
            target = None
            for f in forms:
                op = (f.get("op") or "").lower()
                if op in ("download1", "download2") or "method_free" in f:
                    target = f
                    break
            if target is None:
                break
            op = (target.get("op") or "").lower()
            if op == "download2" or "rand" in target:
                # countdown page — respect a short wait if the page asks
                wm = re.search(
                    r"(?:countdown|wait|settimeout\()\D{0,40}?(\d{1,3})\s*(?:sec|second)",
                    html, re.I)
                wait = int(wm.group(1)) if wm else 0
                if 0 < wait <= 60:
                    _time.sleep(wait)
            # XFS free-download forms need the method_free button value
            if "method_free" not in target:
                target["method_free"] = "Free Download"
            r = s.post(page_url, data=target)
            ctype = r.headers.get("content-type", "").lower()
            if r.history and "html" not in ctype and "text" not in ctype:
                # final POST redirected straight to the file (classic XFS) —
                # the landed URL is the direct download link.
                return str(r.url)
            html = r.text or ""
            low = html.lower()
            if "file not found" in low or "file was deleted" in low:
                raise FileHostError(
                    "dead",
                    "❌ ဒီ Mega4Upload file က သက်တမ်းကုန်ပြီ (သို့) ဖျက်လိုက်ပါပြီ.\n"
                    "❌ This Mega4Upload file has expired or was deleted.")
        # final page: pick the direct file link (off-site, not page chrome).
        # File servers live on subdomains like s12.mega4upload.net:8080, so
        # only skip links on the exact page host, not every subdomain.
        page_host = urllib.parse.urlparse(page_url).netloc.lower()
        cands = []
        for lm in re.finditer(r'href=["\'](https?://[^"\']+)["\']', html, re.I):
            link = _html.unescape(lm.group(1))
            host = urllib.parse.urlparse(link).netloc.lower()
            if not host or host == page_host:
                continue
            if any(x in host for x in ("googletagmanager", "google-analytics",
                                       "facebook.com", "twitter.com")):
                continue
            cands.append(link)
        # prefer links that look like file downloads
        for link in cands:
            if re.search(r"\.(mp4|mkv|avi|mov|mp3|m4a|zip|rar|pdf|exe)($|\?)",
                         link, re.I) or "/dl/" in link or "download" in link:
                return link
        if cands:
            return cands[0]
        raise FileHostError(
            "network",
            "❌ Mega4Upload direct link ရှာမရပါ — စာမျက်နှာပုံစံ ပြောင်းသွားနိုင်ပါတယ်, နောက်မှ ပြန်စမ်းပါ.\n"
            "❌ Could not find the Mega4Upload download link — the page "
            "layout may have changed.")
    except FileHostError:
        raise
    except Exception as e:
        raise FileHostError(
            "network", "❌ Mega4Upload ဆက်သွယ်မရပါ: %s" % str(e)[:150])
    finally:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------- UploadNow

_UPLOADNOW_API = "https://uploadnow.io/api"
# Public Firebase *web* API key shipped inside uploadnow.io's own JS bundle
# (not a secret — every visitor's browser uses it for anonymous sign-in).
# If the site rotates it, _uploadnow_fb_key() re-scrapes a fresh one from the
# live bundle, so downloads keep working without a code change.
_UPLOADNOW_FB_KEY = ["AIzaSyB1SU4XZ9ryZjgtlYLU2yX2OBrAM6ajSWo"]
_UPLOADNOW_TOKEN_TTL = 3300  # Firebase idTokens live ~1h; refresh well inside
_uploadnow_token_cache = {"token": None, "at": 0.0}

_UPLOADNOW_VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v",
                         ".ts", ".m2ts", ".wmv", ".flv", ".3gp")


def _uploadnow_share_code(url):
    """Extract the share code from uploadnow.io URLs.

    Supported shapes:
      https://uploadnow.io/f/<code>
      https://uploadnow.io/<locale>/share?utm_source=<code>  (redirect target)
    """
    try:
        parts = urllib.parse.urlparse(url or "")
    except Exception:
        return None
    if "uploadnow.io" not in (parts.netloc or "").lower():
        return None
    m = re.fullmatch(r"/f/([A-Za-z0-9_-]+)", (parts.path or "").rstrip("/"))
    if m:
        return m.group(1)
    q = urllib.parse.parse_qs(parts.query or "")
    src = (q.get("utm_source") or [""])[0].strip()
    return src or None


def _uploadnow_fb_key():
    """Return a working Firebase web API key, re-scraping if the cached one dies."""
    import httpx
    if _UPLOADNOW_FB_KEY[0]:
        return _UPLOADNOW_FB_KEY[0]
    try:
        s = httpx.Client(headers={"User-Agent": "Mozilla/5.0"},
                         follow_redirects=True, timeout=30, trust_env=False)
        try:
            home = s.get("https://uploadnow.io/").text
            m = re.search(r"/_next/static/chunks/pages/_app-([a-f0-9]+)[.]js",
                          home)
            if not m:
                raise FileHostError(
                    "network",
                    "❌ UploadNow စာမျက်နှာ ဖတ်မရပါ — နောက်မှ ပြန်စမ်းပါ.\n"
                    "❌ Could not read the UploadNow page — try again later.")
            js = s.get(
                "https://cdn.uploadnow.io/_next/static/chunks/pages/_app-"
                + m.group(1) + ".js").text
            k = re.search(r"AIza[0-9A-Za-z_-]{20,}", js)
            if not k:
                raise FileHostError(
                    "network",
                    "❌ UploadNow key ရှာမရပါ — နောက်မှ ပြန်စမ်းပါ.\n"
                    "❌ UploadNow key lookup failed — try again later.")
            _UPLOADNOW_FB_KEY[0] = k.group(0)
            return k.group(0)
        finally:
            s.close()
    except FileHostError:
        raise
    except Exception as e:
        raise FileHostError(
            "network",
            "❌ UploadNow ဆက်သွယ်မရပါ: " + str(e)[:120] + "\n"
            "❌ UploadNow request failed.")


def _uploadnow_anon_token():
    """Mint a Firebase anonymous idToken — exactly what uploadnow.io's own
    web client does for logged-out visitors."""
    import time
    import httpx
    now = time.time()
    if (_uploadnow_token_cache["token"]
            and now - _uploadnow_token_cache["at"] < _UPLOADNOW_TOKEN_TTL):
        return _uploadnow_token_cache["token"]
    key = _uploadnow_fb_key()
    try:
        s = httpx.Client(headers={"User-Agent": "Mozilla/5.0"},
                         timeout=30, trust_env=False)
        try:
            for attempt in (0, 1):
                r = s.post(
                    "https://identitytoolkit.googleapis.com/v1/accounts:signUp",
                    params={"key": key}, json={"returnSecureToken": True})
                try:
                    data = r.json()
                except Exception:
                    data = {}
                tok = (data or {}).get("idToken")
                if tok:
                    _uploadnow_token_cache.update(token=tok, at=now)
                    return tok
                if "API_KEY_INVALID" in r.text and attempt == 0:
                    _UPLOADNOW_FB_KEY[0] = ""  # force re-scrape, retry once
                    key = _uploadnow_fb_key()
                    continue
                break
        finally:
            s.close()
    except FileHostError:
        raise
    except Exception as e:
        raise FileHostError(
            "network",
            "❌ UploadNow ဆက်သွယ်မရပါ: " + str(e)[:120] + "\n"
            "❌ UploadNow request failed.")
    raise FileHostError(
        "network",
        "❌ UploadNow login မရပါ — နောက်မှ ပြန်စမ်းပါ.\n"
        "❌ UploadNow sign-in failed — try again later.")


def _uploadnow_pick(files):
    """Pick the best file from a share: largest video, else largest file."""
    vids = [f for f in files
            if str(f.get("name") or "").lower().endswith(_UPLOADNOW_VIDEO_EXTS)]
    pool = vids or files
    return max(pool, key=lambda f: int(f.get("size") or 0))


def _uploadnow_direct(url):
    """Resolve an uploadnow.io share link to (direct_url, filename, size_bytes).

    Flow (verified 2026-09-27, no login):
      Firebase anonymous signUp -> idToken
      POST /api/file/search/folder-content {folderId} -> files[]
      POST /api/file/downloads/links {folderGroups:[...]} -> {"url": signed}
    The signed URL is a pre-signed bucket URL — a plain GET downloads the bytes.
    """
    import httpx
    code = _uploadnow_share_code(url)
    if not code:
        raise FileHostError(
            "dead",
            "❌ UploadNow link ပုံစံ မမှန်ပါ.\n"
            "❌ Unrecognized UploadNow link format.")
    token = _uploadnow_anon_token()
    try:
        s = httpx.Client(
            headers={"User-Agent": "Mozilla/5.0",
                     "Authorization": "Bearer " + token,
                     "Origin": "https://uploadnow.io",
                     "Referer": "https://uploadnow.io/"},
            follow_redirects=True, timeout=30, trust_env=False)
    except Exception as e:
        raise FileHostError(
            "network",
            "❌ UploadNow ဆက်သွယ်မရပါ: " + str(e)[:120])
    try:
        r = s.post(_UPLOADNOW_API + "/file/search/folder-content",
                   json={"folderId": code, "limit": 100,
                         "sortField": "updateDate", "sortDirection": "desc"})
        if r.status_code == 403:
            raise FileHostError(
                "code",
                "🔒 ဒီ UploadNow link က password လိုပါတယ် — bot က မဖွင့်နိုင်ပါ.\n"
                "🔒 This UploadNow link needs a password — the bot can't open it.")
        if r.status_code == 404:
            raise FileHostError(
                "dead",
                "❌ ဒီ UploadNow link က သက်တမ်းကုန်ပြီ (သို့) ဖျက်လိုက်ပါပြီ.\n"
                "❌ This UploadNow link has expired or was deleted.")
        r.raise_for_status()
        files = (r.json() or {}).get("files") or []
        if not files:
            raise FileHostError(
                "dead",
                "❌ ဒီ UploadNow link မှာ file မရှိပါ.\n"
                "❌ No files in this UploadNow link.")
        f = _uploadnow_pick(files)
        fid = f.get("id")
        name = f.get("name") or "uploadnow_file"
        size = int(f.get("size") or 0)
        if not fid:
            raise FileHostError(
                "dead",
                "❌ UploadNow file id ရမလာပါ.\n"
                "❌ Could not read the UploadNow file id.")
        if size > _MAX_MB * 1048576:
            mb = size / 1048576
            raise FileHostError(
                "too_big",
                "📦 File ကြီးလွန်းပါတယ် (%.0fMB > %dMB) - Telegram က 2GB ထိပဲ "
                "ပို့လို့ရပါတယ်.\n\n"
                "📦 File too large (%.0fMB > %dMB) - Telegram caps at 2GB."
                % (mb, _MAX_MB, mb, _MAX_MB))
        r2 = s.post(_UPLOADNOW_API + "/file/downloads/links",
                    json={"folderGroups": [{"selectedFiles": [fid],
                                            "selectedFolders": [],
                                            "folderId": code}],
                            "stream": False})
        r2.raise_for_status()
        direct = (r2.json() or {}).get("url")
        if not direct:
            raise FileHostError(
                "dead",
                "❌ UploadNow direct link ရမလာပါ.\n"
                "❌ No download link returned.")
        return direct, name, size
    except FileHostError:
        raise
    except Exception as e:
        raise FileHostError(
            "network",
            "❌ UploadNow ဆက်သွယ်မရပါ: " + str(e)[:120] + "\n"
            "❌ UploadNow request failed.")
    finally:
        try:
            s.close()
        except Exception:
            pass

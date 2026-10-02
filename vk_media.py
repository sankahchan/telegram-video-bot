"""VK video extraction without login.

Method (verified 2026-10-02, incl. VPS spike test): the public embed page
https://vk.com/video_ext.php?oid={oid}&id={id} carries direct mp4 URLs
(mp4_144 .. mp4_1080) plus the title. No auth, no API key. yt-dlp's VK
extractor is frequently broken (IncompleteRead on JSON metadata), so this
module is used as a cascade BEFORE yt-dlp in download_web — same pattern
as x_media / tiktok_media.
"""
import json
import re
import urllib.parse

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# vk.com/video-147215218_456245213 , vk.ru/video-.. , video_ext.php?oid=&id=
_VIDEO_RE = re.compile(
    r"(?:vk\.com|vk\.ru|vkvideo\.ru)/video(-?\d+)_(\d+)", re.IGNORECASE)
_Z_RE = re.compile(r"[?&]z=video(-?\d+)_(\d+)", re.IGNORECASE)
_EXT_RE = re.compile(r"video_ext\.php\?oid=(-?\d+)&id=(\d+)", re.IGNORECASE)
_MP4_RE = re.compile(r'"mp4_(\d+)":"([^"]+)"')
_TITLE_RE = re.compile(r'"title":"((?:[^"\\]|\\.)*)"')


class VkMediaError(Exception):
    """kind: not_found | no_media | blocked | network"""
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def is_vk_url(url: str) -> bool:
    u = (url or "").lower()
    return "vk.com" in u or "vk.ru" in u or "vkvideo.ru" in u


def extract_vk_ref(url: str):
    """(oid, video_id) from a VK video URL, or None."""
    u = urllib.parse.unquote(url or "")
    for rx in (_VIDEO_RE, _Z_RE, _EXT_RE):
        m = rx.search(u)
        if m:
            return m.group(1), m.group(2)
    return None


def _unescape(s: str) -> str:
    try:
        return json.loads('"' + s + '"')
    except Exception:
        return s.replace("\\/", "/")


def _pick_mp4(html: str):
    """highest-quality direct mp4 URL -> (quality, url), or None."""
    best = None
    for m in _MP4_RE.finditer(html or ""):
        q = int(m.group(1))
        if best is None or q > best[0]:
            best = (q, _unescape(m.group(2)))
    return best


def _pick_title(html: str) -> str:
    m = _TITLE_RE.search(html or "")
    if not m:
        return "vk_video"
    t = _unescape(m.group(1)).strip()
    return t or "vk_video"


async def extract_vk_media(url: str) -> dict:
    """Fetch the public embed page, return {"url", "title", "quality"}.

    Raises VkMediaError with kind on failure.
    """
    try:
        import httpx
    except ImportError:
        raise VkMediaError("network", "httpx မရှိပါ — update.sh run ပေးပါ")
    ref = extract_vk_ref(url)
    if not ref:
        raise VkMediaError(
            "no_media",
            "❌ VK video link ပုံစံ မမှန်ပါ — ဥပမာ: https://vk.com/video-123_456\n\n"
            "❌ Unrecognized VK video link format — example: https://vk.com/video-123_456")
    oid, vid = ref
    try:
        async with httpx.AsyncClient(timeout=15,
                                     headers={"User-Agent": _UA},
                                     follow_redirects=True) as c:
            r = await c.get("https://vk.com/video_ext.php",
                            params={"oid": oid, "id": vid})
    except Exception as e:
        raise VkMediaError(
            "network",
            f"❌ VK ကို ဆက်သွယ်လို့မရပါ: {type(e).__name__}\n\n"
            f"❌ Couldn't reach VK: {type(e).__name__}")
    html = r.text or ""
    pick = _pick_mp4(html)
    if pick:
        q, media_url = pick
        return {"url": media_url, "title": _pick_title(html), "quality": q}
    low = html.lower()
    if "recaptcha" in low or "showcaptcha" in low or len(html) < 20000:
        raise VkMediaError(
            "blocked",
            "❌ VK က ဒီ VPS IP ကို ခဏ block ထားပါတယ် — နောက်မှ ပြန်စမ်းပါ.\n\n"
            "❌ VK is temporarily blocking this server's IP — please try again later.")
    raise VkMediaError(
        "not_found",
        "❌ ဒီ VK video ကို မတွေ့ပါ — ဖျက်လိုက်တာ / private / link မှားနေတာ ဖြစ်နိုင်ပါတယ်.\n\n"
        "❌ This VK video was not found — it may be deleted, private, or the link is wrong.")

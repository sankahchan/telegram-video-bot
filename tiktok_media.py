"""TikTok media extraction without login.

Cascade:
  1. tikwm public API (fetches from its own servers — works even when
     TikTok's webpage returns "Unexpected response from webpage request"
     for the VPS IP). Returns the watermark-free video.
  2. yt-dlp stays as the fallback in web_download.download_web.

API: GET https://www.tikwm.com/api/?url=<tiktok-url>
  -> {"code":0,"msg":"success","data":{"play":...,"wmplay":...,
      "music":...,"title":...,"duration":...,"size":...}}
"""
import json
import urllib.parse
import urllib.request

TIKWM_API = "https://www.tikwm.com/api/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


class TikTokMediaError(Exception):
    """kind: not_found | private | rate_limited | network."""
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in (url or "").lower()


def _api_get(url: str) -> dict:
    api = TIKWM_API + "?url=" + urllib.parse.quote(url, safe="")
    req = urllib.request.Request(api, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_tiktok_media(url: str) -> dict:
    """Watermark-free TikTok video via tikwm.

    Returns {"kind": "video", "url": <mp4>, "title": ..., "music": <mp3?>}.
    Raises TikTokMediaError on any failure.
    """
    try:
        data = _api_get(url)
    except Exception as e:
        raise TikTokMediaError(
            "network", f"tikwm API မဆက်သွယ်နိုင်ပါ: {e}") from e
    if not isinstance(data, dict) or data.get("code") != 0:
        msg = str((data or {}).get("msg", "unknown error")).lower()
        if any(w in msg for w in ("deleted", "not found", "invalid url",
                                  "unsupported")):
            raise TikTokMediaError(
                "not_found", f"TikTok video ရှာမတွေ့ပါ ({msg})")
        if any(w in msg for w in ("private", "login")):
            raise TikTokMediaError(
                "private", f"TikTok video က private ({msg})")
        if any(w in msg for w in ("rate", "limit", "too many")):
            raise TikTokMediaError(
                "rate_limited", f"tikwm rate limit ({msg})")
        raise TikTokMediaError(
            "network", f"tikwm error: {(data or {}).get('msg', '?')}")
    d = data.get("data") or {}
    play = d.get("play")
    if not play:
        raise TikTokMediaError("network", "tikwm က video URL မပေးပါ")
    title = (d.get("title") or "tiktok_video").strip() or "tiktok_video"
    return {"kind": "video", "url": play, "title": title,
            "music": d.get("music")}

"""YouTube fallback download chain: Cobalt -> Piped -> Invidious.

When yt-dlp is blocked by YouTube's bot-check on datacenter IPs
("Sign in to confirm you're not a bot" / storyboard-only formats), this
module resolves a direct media URL through third-party frontends that run
on non-flagged IPs, so the VPS can still download the file over plain HTTP.

Design notes:
- Only stdlib + httpx (already a dependency). No new packages.
- All parsing/selection logic is pure and unit-tested; HTTP goes through
  the small _get_json/_post_json helpers.
- Instance lists are discovered live (cached 6h in /tmp) with verified
  hardcoded fallbacks, because public instances come and go.
- Cobalt is only used when COBALT_API_URL is configured (self-hosted or a
  permissioned instance) — the public api.cobalt.tools asks third-party
  bots not to use it without permission, so we don't hammer it.
"""
import json
import os
import re
import time
import urllib.parse

import httpx

# --- configuration -----------------------------------------------------------
# Self-hosted / permissioned cobalt API instance, e.g. http://127.0.0.1:9000/
# (docker: ghcr.io/imput/cobalt). Optional — Piped/Invidious need no setup.
COBALT_API_URL = os.environ.get("COBALT_API_URL", "").strip().rstrip("/")
COBALT_API_KEY = os.environ.get("COBALT_API_KEY", "").strip()

INVIDIOUS_LIST_URL = "https://api.invidious.io/instances.json"
PIPED_LIST_URL = "https://piped-instances.kavin.rocks/"

# Verified live 2026-09-27 (from the official instance-list endpoints).
FALLBACK_INVIDIOUS = [
    "https://invidious.f5.si",      # api:true, cors:true
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
]
FALLBACK_PIPED = [
    "https://api.piped.private.coffee",  # 100% 24h uptime at check time
]

_CACHE_FILE = "/tmp/yt_fallback_instances.json"
_CACHE_TTL_S = 6 * 3600
_HTTP_TIMEOUT = 20


class FallbackError(RuntimeError):
    """All fallback services failed (or the URL isn't usable)."""


# --- pure helpers (unit-tested, no network) ----------------------------------
def youtube_video_id(url: str) -> str | None:
    """Extract the 11-char video id from any YouTube URL form."""
    try:
        u = urllib.parse.urlparse(url or "")
    except Exception:
        return None
    host = (u.netloc or "").lower()
    if "youtu.be" in host:
        vid = u.path.strip("/").split("/")[0]
        return vid or None
    if "youtube.com" in host or "youtube-nocookie.com" in host:
        qs = urllib.parse.parse_qs(u.query)
        if qs.get("v"):
            return qs["v"][0]
        m = re.match(r"/(shorts|embed|live|v)/([^/?#]+)", u.path)
        if m:
            return m.group(2)
    return None


def _quality_num(q) -> int:
    """'1080p' -> 1080, 720 -> 720, garbage -> 0."""
    if q is None:
        return 0
    m = re.search(r"(\d+)", str(q))
    return int(m.group(1)) if m else 0


def _target_height(quality: str) -> int:
    return 720 if quality == "low" else 1080


def pick_piped_stream(data: dict, audio_only: bool = False,
                      quality: str = "high") -> tuple:
    """Pick (media_url, title) from a Piped /streams/{id} response."""
    title = (data.get("title") or "youtube_video").strip() or "youtube_video"
    target = _target_height(quality)
    if audio_only:
        cands = [s for s in data.get("audioStreams", [])
                 if s.get("url")]
        # prefer m4a/mp4 audio, highest bitrate
        cands.sort(key=lambda s: (
            0 if "mp4" in str(s.get("mimeType", "")) or
            "m4a" in str(s.get("codec", "")) else 1,
            -int(s.get("bitrate") or 0)))
        if not cands:
            raise FallbackError("piped: no audio streams")
        return cands[0]["url"], title
    # muxed (has audio) mp4 first — a video-only stream would lose audio
    muxed = [s for s in data.get("videoStreams", [])
             if s.get("url") and not s.get("videoOnly")
             and "mp4" in str(s.get("mimeType", ""))]
    ok = [s for s in muxed if _quality_num(s.get("quality")) <= target]
    pool = ok or muxed
    if not pool:
        raise FallbackError("piped: no muxed mp4 streams")
    pool.sort(key=lambda s: -_quality_num(s.get("quality")))
    return pool[0]["url"], title


def pick_invidious_stream(data: dict, audio_only: bool = False,
                          quality: str = "high") -> tuple:
    """Pick (media_url, title) from an Invidious /api/v1/videos/{id} response."""
    title = (data.get("title") or "youtube_video").strip() or "youtube_video"
    target = _target_height(quality)
    if audio_only:
        cands = [s for s in data.get("adaptiveFormats", [])
                 if s.get("url") and str(s.get("type", "")).startswith("audio/")]
        cands.sort(key=lambda s: -int(s.get("bitrate") or 0))
        if not cands:
            raise FallbackError("invidious: no audio formats")
        return cands[0]["url"], title
    # formatStreams are muxed (video+audio) — exactly what we want
    muxed = [s for s in data.get("formatStreams", [])
             if s.get("url") and s.get("container") == "mp4"]
    ok = [s for s in muxed if _quality_num(s.get("qualityLabel")) <= target]
    pool = ok or muxed
    if not pool:
        raise FallbackError("invidious: no muxed mp4 formatStreams")
    pool.sort(key=lambda s: -_quality_num(s.get("qualityLabel")))
    return pool[0]["url"], title


def parse_cobalt_response(data: dict) -> tuple:
    """Parse a cobalt POST / response -> (media_url, title).

    Statuses: stream | redirect | tunnel -> direct url;
              picker -> first item; error -> raise.
    """
    status = data.get("status")
    if status in ("stream", "redirect", "tunnel"):
        url = data.get("url")
        if not url:
            raise FallbackError(f"cobalt: status={status} but no url")
        return url, _cobalt_title(data)
    if status == "picker":
        items = data.get("picker") or []
        if not items or not items[0].get("url"):
            raise FallbackError("cobalt: empty picker")
        return items[0]["url"], _cobalt_title(data)
    err = data.get("error") or {}
    raise FallbackError(f"cobalt: {err.get('code', 'error')} — "
                        f"{err.get('context', {}).get('service', '?')}")


def _cobalt_title(data: dict) -> str:
    t = (data.get("filename") or "").strip()
    if t:
        # cobalt returns a filename like "youtube_dQw4w9WgXcQ_1080p_h264.mp4"
        t = re.sub(r"\.(mp4|webm|m4a|mp3|opus)$", "", t)
        t = re.sub(r"^youtube_[A-Za-z0-9_-]{11}_", "", t)
        t = t.replace("_", " ").strip()
        if t:
            return t[:120]
    return "youtube_video"


def filter_invidious_instances(data) -> list:
    """Filter api.invidious.io/instances.json -> [https uri, ...]."""
    out = []
    if not isinstance(data, list):
        return out
    for entry in data:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        name, info = entry[0], entry[1]
        if not isinstance(info, dict):
            continue
        if info.get("type") != "https":
            continue
        if info.get("api") is False:  # None/unknown -> try anyway
            continue
        mon = info.get("monitor") or {}
        if mon.get("down"):
            continue
        uri = info.get("uri") or (f"https://{name}" if name else None)
        if uri:
            out.append(uri.rstrip("/"))
    return out


def filter_piped_instances(data) -> list:
    """Filter piped-instances.kavin.rocks JSON -> [api_url, ...] by health."""
    if not isinstance(data, list):
        return []
    cands = [d for d in data
             if isinstance(d, dict) and d.get("api_url")
             and d.get("up_to_date")
             and (d.get("uptime_24h") or 0) >= 90]
    cands.sort(key=lambda d: -(d.get("uptime_30d") or 0))
    return [d["api_url"].rstrip("/") for d in cands]


# --- HTTP + discovery (cached) ------------------------------------------------
def _get_json(url: str, timeout: int = _HTTP_TIMEOUT) -> object:
    r = httpx.get(url, timeout=timeout, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def _post_json(url: str, body: dict, headers: dict,
               timeout: int = 25) -> object:
    r = httpx.post(url, json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _cached(key: str, fetcher) -> list:
    """Read-through cache for instance lists (6h TTL, /tmp)."""
    now = time.time()
    try:
        with open(_CACHE_FILE) as f:
            cache = json.load(f)
        hit = cache.get(key)
        if hit and now - hit.get("at", 0) < _CACHE_TTL_S and hit.get("items"):
            return hit["items"]
    except Exception:
        pass
    try:
        items = fetcher() or []
    except Exception as e:
        print(f"⚠️ yt_fallback: instance discovery failed ({e})")
        items = []
    if items:
        try:
            cache = {}
            try:
                with open(_CACHE_FILE) as f:
                    cache = json.load(f)
            except Exception:
                pass
            cache[key] = {"at": now, "items": items}
            with open(_CACHE_FILE, "w") as f:
                json.dump(cache, f)
        except Exception:
            pass
    return items


def invidious_instances() -> list:
    items = _cached("invidious",
                    lambda: filter_invidious_instances(
                        _get_json(INVIDIOUS_LIST_URL, timeout=15)))
    return items or list(FALLBACK_INVIDIOUS)


def piped_instances() -> list:
    items = _cached("piped",
                    lambda: filter_piped_instances(
                        _get_json(PIPED_LIST_URL, timeout=15)))
    return items or list(FALLBACK_PIPED)


# --- resolvers ----------------------------------------------------------------
def cobalt_resolve(url: str, audio_only: bool = False,
                   quality: str = "high") -> tuple:
    """Resolve via configured cobalt instance -> (media_url, title)."""
    if not COBALT_API_URL:
        raise FallbackError("cobalt: COBALT_API_URL not configured")
    body = {"url": url, "downloadMode": "audio" if audio_only else "auto",
            "filenameStyle": "basic"}
    if not audio_only:
        body["videoQuality"] = "720" if quality == "low" else "1080"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if COBALT_API_KEY:
        headers["Authorization"] = f"Api-Key {COBALT_API_KEY}"
    data = _post_json(COBALT_API_URL, body, headers)
    if not isinstance(data, dict):
        raise FallbackError("cobalt: bad response")
    return parse_cobalt_response(data)


def piped_resolve(video_id: str, audio_only: bool = False,
                  quality: str = "high", max_instances: int = 2) -> tuple:
    """Resolve via public Piped API instances -> (media_url, title)."""
    last: Exception | None = None
    for api in piped_instances()[:max_instances]:
        try:
            data = _get_json(f"{api}/streams/{video_id}")
            if not isinstance(data, dict):
                raise FallbackError("piped: bad response")
            return pick_piped_stream(data, audio_only, quality)
        except Exception as e:
            last = e
            print(f"⚠️ yt_fallback: piped {api} failed ({e})")
    raise FallbackError(f"piped: all instances failed ({last})")


def invidious_resolve(video_id: str, audio_only: bool = False,
                      quality: str = "high", max_instances: int = 2) -> tuple:
    """Resolve via public Invidious API instances -> (media_url, title)."""
    last: Exception | None = None
    for inst in invidious_instances()[:max_instances]:
        try:
            data = _get_json(
                f"{inst}/api/v1/videos/{video_id}"
                "?fields=title,formatStreams,adaptiveFormats")
            if not isinstance(data, dict):
                raise FallbackError("invidious: bad response")
            if data.get("error"):
                raise FallbackError(f"invidious: {data['error']}")
            return pick_invidious_stream(data, audio_only, quality)
        except Exception as e:
            last = e
            print(f"⚠️ yt_fallback: invidious {inst} failed ({e})")
    raise FallbackError(f"invidious: all instances failed ({last})")


def youtube_fallback_url(url: str, audio_only: bool = False,
                         quality: str = "high") -> tuple:
    """Full chain: Cobalt (if configured) -> Piped -> Invidious.

    Returns (media_url, title, source_name). Raises FallbackError if all fail.
    """
    vid = youtube_video_id(url)
    if not vid:
        raise FallbackError("not a YouTube URL")
    errors = []
    if COBALT_API_URL:
        try:
            media_url, title = cobalt_resolve(url, audio_only, quality)
            return media_url, title, "cobalt"
        except Exception as e:
            errors.append(f"cobalt: {e}")
    try:
        media_url, title = piped_resolve(vid, audio_only, quality)
        return media_url, title, "piped"
    except Exception as e:
        errors.append(f"piped: {e}")
    try:
        media_url, title = invidious_resolve(vid, audio_only, quality)
        return media_url, title, "invidious"
    except Exception as e:
        errors.append(f"invidious: {e}")
    raise FallbackError("YouTube fallback failed — " + " | ".join(errors))

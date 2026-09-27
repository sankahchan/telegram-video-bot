# embed.py — movie/series streaming via embed providers (v6.8.0)
"""TMDB search + provider cascade (VidNest/VixSrc/VidEasy/...) + yt-dlp HLS download.

Providers need a TMDB id. /stream searches TMDB (needs TMDB_API_KEY env),
resolves the first provider with sources, downloads best quality via yt-dlp
with the provider's Referer/Origin headers.
"""
import asyncio
import os
import re

import httpx

import embed_providers as EP

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TMDB = "https://api.themoviedb.org/3"
MAX_MB = 1900


class EmbedError(Exception):
    def __init__(self, kind: str, message: str):
        self.kind = kind          # no_key | not_found | no_stream | too_big | network
        self.message = message
        super().__init__(message)


def _tmdb_key() -> str:
    k = os.environ.get("TMDB_API_KEY", "").strip()
    if not k:
        raise EmbedError(
            "no_key",
            "🔑 TMDB_API_KEY မရှိသေးပါ.\n"
            "themoviedb.org မှာ အခမဲ့ API key ယူပြီး .env ထဲ\n"
            "TMDB_API_KEY=... ထည့်ပေးပါ.\n\n"
            "🔑 TMDB_API_KEY is not set. Get a free key at themoviedb.org "
            "and add TMDB_API_KEY=... to .env")
    return k


def tmdb_search(query: str, limit: int = 8) -> list:
    """Keyless မရ — TMDB multi-search (movie + tv). Returns [{tmdb_id, media_type, title, year}]."""
    key = _tmdb_key()
    r = httpx.get(f"{TMDB}/search/multi",
                  params={"api_key": key, "query": query,
                          "language": "en-US", "page": 1,
                          "include_adult": "false"},
                  headers={"User-Agent": UA}, timeout=20, trust_env=False)
    r.raise_for_status()
    out = []
    for it in (r.json().get("results") or [])[:limit]:
        mt = it.get("media_type")
        if mt not in ("movie", "tv"):
            continue
        title = it.get("title") or it.get("name") or "?"
        date = it.get("release_date") or it.get("first_air_date") or ""
        out.append({"tmdb_id": it.get("id"), "media_type": mt,
                    "title": title, "year": date[:4]})
    return out


def _quality_rank(q: str) -> int:
    q = (q or "").lower()
    m = re.search(r"(\d{3,4})\s*p?", q)
    if m:
        return int(m.group(1))
    if "4k" in q or "2160" in q:
        return 2160
    if "auto" in q:
        return 720
    return 0


def pick_best(sources: list) -> dict | None:
    """Prefer mp4, then hls; highest quality label first."""
    if not sources:
        return None
    def key(s):
        typ = 0 if s.get("type") == "mp4" else (1 if s.get("type") == "hls" else 2)
        return (typ, -_quality_rank(s.get("quality")))
    return sorted(sources, key=key)[0]


def fmt_size(n) -> str:
    """Human-readable file size; None -> '—'."""
    if n is None:
        return "—"
    n = int(n)
    if n < 1024:
        return f"{n}B"
    if n < 1048576:
        return f"{n / 1024:.1f}KB".replace(".0KB", "KB")
    if n < 1073741824:
        return f"{n / 1048576:.1f}MB".replace(".0MB", "MB")
    return f"{n / 1073741824:.1f}GB".replace(".0GB", "GB")


def probe_source_size(source: dict, client=None, timeout: float = 10) -> int | None:
    """True file size: 1-byte Range GET first (content-range total is
    authoritative even when HEAD lies), HEAD as fallback. HLS -> None (skip).
    Bodies are never read — headers only. Blocking; run in a thread."""
    url = (source or {}).get("url") or ""
    if not url or ".m3u8" in url.lower():
        return None
    headers = {"User-Agent": UA}
    if source.get("referer"):
        headers["Referer"] = source["referer"]
    if source.get("origin"):
        headers["Origin"] = source["origin"]
    own = client is None
    if own:
        client = httpx.Client(headers={"User-Agent": UA}, timeout=timeout,
                              follow_redirects=True, trust_env=False)
    try:
        try:
            h2 = dict(headers)
            h2["Range"] = "bytes=0-0"
            # stream: headers only, never read the body (a 200 may be gigabytes)
            with client.stream("GET", url, headers=h2, timeout=timeout) as r2:
                m = re.search(r"/(\d+)\s*$",
                              r2.headers.get("content-range") or "")
                if m:
                    return int(m.group(1))
                if r2.status_code == 200:
                    cl = r2.headers.get("content-length")
                    if cl and cl.isdigit():
                        return int(cl)
        except Exception:
            pass
        try:
            r = client.head(url, headers=headers, timeout=timeout)
        except Exception:
            return None
        if r.status_code == 200:
            cl = r.headers.get("content-length")
            if cl and cl.isdigit():
                return int(cl)
        return None
    finally:
        if own:
            client.close()


SMALL_WARN_BYTES = 5 * 1048576  # below this, a "movie" file is suspicious


def stream_src_label(src: dict, size, is_best: bool = False) -> str:
    """Button label for the quality+size picker, e.g. '⭐ 1080p • 1.4GB'.
    Suspiciously small files (< 5MB) get a ⚠️ instead of ⭐/📥."""
    if size is not None and size < SMALL_WARN_BYTES:
        icon = "⚠️"
    else:
        icon = "⭐" if is_best else "📥"
    q = ((src.get("quality") or "auto").strip()) or "auto"
    return f"{icon} {q} • {fmt_size(size)}"


def filter_oversize(sources: list, sizes: list) -> tuple:
    """Drop sources whose known size exceeds MAX_MB.
    Returns (kept_sources, kept_sizes, dropped_count)."""
    cap = MAX_MB * 1048576
    kept, ksizes, dropped = [], [], 0
    for s, z in zip(sources, sizes):
        if z is not None and z > cap:
            dropped += 1
            continue
        kept.append(s)
        ksizes.append(z)
    return kept, ksizes, dropped


# (label, extractor) — first provider with sources wins
CASCADE = [
    ("VidNest", EP.extract_vidnest),
    ("VixSrc", EP.extract_vixsrc),
    ("VidEasy", EP.extract_videasy),
    ("VidRock", EP.extract_vidrock),
    ("2Embed", EP.extract_2embed),
    ("VidCore", EP.extract_vidcore_org),
    ("VidLink", EP.extract_vidlink),
]


def _vidsrc_embed_url(tmdb_id: int, media_type: str, season, episode) -> str:
    base = "https://vidsrc.to/embed"
    if media_type == "tv":
        return f"{base}/tv/{tmdb_id}/{season or 1}/{episode or 1}"
    return f"{base}/movie/{tmdb_id}"


def resolve_streams(tmdb_id: int, media_type: str = "movie",
                    season: int | None = None,
                    episode: int | None = None) -> tuple:
    """Run the provider cascade. Returns (provider_label, sources).
    Raises EmbedError(not_found / no_stream). Blocking — run in a thread."""
    last_err = None
    with httpx.Client(headers={"User-Agent": UA}, timeout=25,
                      follow_redirects=True, trust_env=False) as client:
        for label, fn in CASCADE:
            try:
                res = fn(client, tmdb_id, media_type=media_type,
                         season=season, episode=episode) or {}
                srcs = [s for s in (res.get("sources") or []) if s.get("url")]
                if srcs:
                    return label, srcs
            except Exception as e:
                last_err = e
                print(f"⚠️ embed {label} failed for {tmdb_id}: {type(e).__name__}")
                continue
        # VidSrc family last (needs an embed URL, Turnstile risk)
        try:
            res = EP.extract_vidsrc_cloudnestra(
                client, _vidsrc_embed_url(tmdb_id, media_type, season, episode)) or {}
            srcs = [s for s in (res.get("sources") or []) if s.get("url")]
            if srcs:
                return "VidSrc", srcs
        except Exception as e:
            last_err = e
            print(f"⚠️ embed VidSrc failed for {tmdb_id}: {type(e).__name__}")
    raise EmbedError(
        "no_stream",
        "❌ stream ရှာမရပါ — provider အားလုံး fail ဖြစ်ပါတယ်.\n"
        "ခဏနေမှ ပြန်စမ်းကြည့်ပါ.\n\n"
        "❌ No stream found — all providers failed. Try again later.")


def download_embed(source: dict, title: str, tmpdir: str,
                   progress_cb=None, loop=None, tag: str = "📥") -> str:
    """Download one resolved source via yt-dlp (HLS/mp4). Returns file path.
    Blocking — run in a thread (progress via progress_cb like download_web)."""
    from yt_dlp import YoutubeDL
    url = source["url"]
    headers = {}
    if source.get("referer"):
        headers["Referer"] = source["referer"]
    if source.get("origin"):
        headers["Origin"] = source["origin"]
    safe = re.sub(r"[^\w\- ]+", "", title).strip()[:60] or "stream"
    outtmpl = os.path.join(tmpdir, safe + ".%(ext)s")
    opts = {
        "format": "b",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "socket_timeout": 30,
        "retries": 3,
        "noplaylist": True,
        "http_headers": {"User-Agent": UA, **headers},
    }
    if progress_cb and loop:
        def _hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                pct = (done / total * 100) if total else 0
                asyncio.run_coroutine_threadsafe(progress_cb(tag, pct), loop)
        opts["progress_hooks"] = [_hook]
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise EmbedError("no_stream", "❌ download info မရပါ.")
        path = ydl.prepare_filename(info)
        if not os.path.exists(path):
            vid = info.get("id", "")
            for f in os.listdir(tmpdir):
                if f.startswith(str(vid)):
                    path = os.path.join(tmpdir, f)
                    break
    size_mb = os.path.getsize(path) / 1048576
    if size_mb > MAX_MB:
        os.remove(path)
        raise EmbedError(
            "too_big",
            f"❌ file ကြီးလွန်းပါတယ် ({size_mb:.0f}MB > {MAX_MB}MB).\n\n"
            f"❌ File too large ({size_mb:.0f}MB > {MAX_MB}MB).")
    return path


async def resolve_and_download(tmdb_id: int, media_type: str, title: str,
                               season: int | None, episode: int | None,
                               tmpdir: str, progress_cb=None,
                               loop=None, tag: str = "📥") -> tuple:
    """Full pipeline: cascade resolve -> pick best -> download.
    Returns (path, provider_label)."""
    label, sources = await asyncio.to_thread(
        resolve_streams, tmdb_id, media_type, season, episode)
    best = pick_best(sources)
    if not best:
        raise EmbedError("no_stream", "❌ playable source မရှိပါ.")
    suffix = f" S{season}E{episode}" if media_type == "tv" else ""
    path = await asyncio.to_thread(
        download_embed, best, f"{title}{suffix}", tmpdir,
        progress_cb, loop, tag)
    return path, label

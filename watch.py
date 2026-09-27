"""Resolve free-streaming watch pages (andyday.sx and same-template clones)
to provider embed URLs, then to downloadable sources via embed_providers.

Watch page HTML carries window.__OPT = [embed urls...] (index = server).
No AJAX, no tokens — plain HTTP + regex is enough.
"""
import re
import time
from urllib.parse import urlparse, parse_qs, urljoin

import httpx

import embed_providers as EP

WATCH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# index-aligned with window.__OPT on the watch page
SERVER_NAMES = ["vidsrc.mov", "vidsrc.fyi", "vidrock", "vidnest", "vidking",
                "vidlink", "vidfast", "vidup", "videasy", "111movies",
                "2embed", "multiembed", "superflixapi", "peachify"]

WATCH_RE = re.compile(
    r"https?://(?:www\.)?andyday\.sx/watch/(tv|movie)-[A-Za-z0-9\-]+", re.I)

# host fragment -> (call_kind, extractor fn name, display label)
# "url": fn(client, embed_url); "ids": fn(client, tmdb_id, media_type, season, episode)
EMBED_MAP = [
    ("vidsrc.mov", "url", "extract_vidsrc_cloudnestra", "VidSrc"),
    ("vidsrc.fyi", "url", "extract_vidsrc_cloudnestra", "VidSrc"),
    ("vidrock.net", "ids", "extract_vidrock", "VidRock"),
    ("vidnest", "ids", "extract_vidnest", "VidNest"),
    ("vidlink.pro", "ids", "extract_vidlink", "VidLink"),
    ("videasy.net", "ids", "extract_videasy", "VidEasy"),
    ("2embed", "ids", "extract_2embed", "2Embed"),
]


class WatchError(Exception):
    def __init__(self, kind, message):
        self.kind = kind
        self.message = message
        super().__init__(message)


def is_watch_url(url) -> bool:
    return bool(WATCH_RE.search(url or ""))


def parse_watch_url(url) -> dict | None:
    """-> {media_type, season, episode, url} or None."""
    m = WATCH_RE.search(url or "")
    if not m:
        return None
    media_type = "tv" if m.group(1).lower() == "tv" else "movie"
    qs = parse_qs(urlparse(url).query)
    season = int(qs["s"][0]) if qs.get("s", [""])[0].isdigit() else None
    episode = int(qs["e"][0]) if qs.get("e", [""])[0].isdigit() else None
    return {"media_type": media_type, "season": season, "episode": episode,
            "url": m.group(0)}


def extract_embeds_from_html(html: str) -> list:
    """[(server_name, embed_url)] from window.__OPT (index-aligned).
    Falls back to the player iframe / data-embed attribute."""
    urls = []
    m = re.search(r"window\.__OPT\s*=\s*\[(.*?)\]", html or "", re.S)
    if m:
        # v6.13.1: quote-agnostic — site may use single quotes
        urls = re.findall(r'''["'](https?://[^"']+)["']''', m.group(1))
    if not urls:
        m2 = re.search(r'<iframe[^>]+id="embed-iframe"[^>]+src="([^"]+)"',
                       html or "")
        if m2:
            urls = [m2.group(1)]
    if not urls:
        m3 = re.search(r'data-embed="([^"]+)"', html or "")
        if m3:
            urls = [m3.group(1)]
    out = []
    for i, u in enumerate(urls):
        name = SERVER_NAMES[i] if i < len(SERVER_NAMES) else f"server{i}"
        out.append((name, u))
    return out


def extract_watch_title(html: str) -> str | None:
    m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
                  html or "", re.I)
    t = m.group(1) if m else None
    if not t:
        m = re.search(r"<title>(.*?)</title>", html or "", re.S | re.I)
        t = m.group(1).strip() if m else None
    if not t:
        return None
    t = re.sub(r"^Watch\s+", "", t, flags=re.I)
    t = re.sub(r"\s*(TV\s+)?Online\s*-\s*Andyday.*$", "", t, flags=re.I)
    t = re.sub(r"\s*\(\d{4}\).*$", "", t)
    return t.strip() or None


def _page_title(html: str) -> str:
    mt = re.search(r"<title>(.*?)</title>", html or "", re.S | re.I)
    return (re.sub(r"\s+", " ", mt.group(1)).strip()[:80] if mt else "?")


def _looks_like_loading_wall(html: str) -> bool:
    """The site intermittently serves a JS 'Loading...' interstitial instead
    of the server list (2026-09-27: real page at 18:56, wall at 19:07/19:09).
    window.__OPT is only injected by JavaScript afterwards."""
    if not html or "window.__OPT" in html:
        return False
    return _page_title(html).rstrip(".").lower() == "loading"


def fetch_watch_embeds(watch_url: str, season=None, episode=None,
                       client=None, timeout: float = 20) -> tuple:
    """-> (title, [(server_name, embed_url)]). Blocking."""
    parsed = parse_watch_url(watch_url)
    if not parsed:
        raise WatchError("bad_url",
                         "❌ watch link ပုံစံ မမှန်ပါ.\n\n❌ Not a watch link.")
    url = parsed["url"]
    if parsed["media_type"] == "tv" and (season or episode):
        url = f"{url}?s={season or 1}&e={episode or 1}"
    own = client is None
    if own:
        client = httpx.Client(headers={"User-Agent": WATCH_UA},
                              timeout=timeout, follow_redirects=True,
                              trust_env=False)
    try:
        # v6.13.2: retry against the "Loading..." wall with a shared cookie
        # session, and follow meta-refresh redirects — the real page is
        # served intermittently.
        html = ""
        last_err = None
        for _ in range(3):
            try:
                r = client.get(url, headers={"User-Agent": WATCH_UA},
                               timeout=timeout)
                r.raise_for_status()
                html = r.text
                last_err = None
            except Exception as e:
                last_err = e
                time.sleep(5)
                continue
            mr = re.search(
                r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+'
                r'content=["\']\d+;\s*url=([^"\']+)', html, re.I)
            if mr:
                url = urljoin(url, mr.group(1).strip())
                continue
            if _looks_like_loading_wall(html):
                time.sleep(5)
                continue
            break
        if last_err is not None:
            raise WatchError(
                "fetch_fail",
                f"❌ watch page ဆွဲမရပါ: {type(last_err).__name__}\n\n"
                f"❌ Could not fetch the watch page: {type(last_err).__name__}")
    finally:
        if own:
            client.close()
    embeds = extract_embeds_from_html(html)
    if not embeds:
        # v6.13.1: diagnose WHY — the page HTML changed between two fetches
        # 11 minutes apart (2026-09-27: 18:56 had 14 embeds, 19:07 had none).
        # Include the page title so one screenshot reveals a bot-check page,
        # an empty server list, or a changed page format.
        mt = re.search(r"<title>(.*?)</title>", html or "", re.S | re.I)
        ptitle = (re.sub(r"\s+", " ", mt.group(1)).strip()[:80]
                  if mt else "?")
        has_opt = "window.__OPT" in (html or "")
        print(f"⛔ watch embeds: __OPT present={has_opt}, "
              f"title={ptitle!r}, {len(html or '')}b", flush=True)
        if has_opt:
            detail = ("❌ ဒီ episode အတွက် video server list ဗလာဖြစ်နေပါတယ် — "
                      "episode မထွက်သေးတာ (သို့) site က ဖြုတ်ထားတာ ဖြစ်နိုင်ပါတယ်.\n\n"
                      "❌ This episode has no video servers listed — it may "
                      "not be out yet or was removed from the site.")
        elif "just a moment" in ptitle.lower() \
                or "challenge" in ptitle.lower() \
                or "attention required" in ptitle.lower():
            detail = ("❌ site က bot-check စာမျက်နှာ ပြနေပါတယ် — VPS IP ကို "
                      "ခဏ block ထားတာ ဖြစ်နိုင်ပါတယ်, ၁၀-၁၅ မိနစ်ကြာမှ ပြန်စမ်းပါ.\n\n"
                      "❌ The site is showing a bot-check page — the VPS IP "
                      "may be temporarily blocked; try again in 10-15 minutes.")
        elif _looks_like_loading_wall(html):
            # v6.13.2: still walled after 3 retries
            detail = ("❌ site က 'Loading...' ပဲ ပြနေပါတယ် (3 ကြိမ် စမ်းပြီးပြီ) — "
                      "VPS ကို JavaScript check ခံနေရတာပါ. ၁၀-၁၅ မိနစ်ကြာမှ "
                      "ပြန်စမ်းပါ, (သို့) တခြား title စမ်းကြည့်ပါ.\n\n"
                      "❌ The site keeps showing its 'Loading...' wall after "
                      "3 tries — it's putting the VPS through a JavaScript "
                      "check. Try again in 10-15 minutes or try another title.")
        else:
            detail = ("❌ ဒီ page မှာ video server ရှာမရပါ "
                      f"(page title: {ptitle}).\n\n"
                      "❌ No video servers found on this page "
                      f"(page title: {ptitle}).")
        raise WatchError("no_embed", detail)
    title = extract_watch_title(html) or "Watch"
    return title, embeds


def parse_embed_url(embed_url: str) -> tuple | None:
    """-> (tmdb_id, media_type, season, episode) or None."""
    u = embed_url or ""
    m = re.search(r"/(tv|movie)/(\d+)(?:/(\d+)/(\d+))?", u)
    if m:
        media = "tv" if m.group(1) == "tv" else "movie"
        s = int(m.group(3)) if m.group(3) else None
        e = int(m.group(4)) if m.group(4) else None
        return int(m.group(2)), media, s, e
    m = re.search(r"embedtv/(\d+)&s=(\d+)&e=(\d+)", u)
    if m:
        return int(m.group(1)), "tv", int(m.group(2)), int(m.group(3))
    m = re.search(r"/embed/(\d+)", u)
    if m:
        return int(m.group(1)), "movie", None, None
    m = re.search(r"[?&]video_id=(\d+)", u)
    if m:
        s = re.search(r"[?&]s=(\d+)", u)
        e = re.search(r"[?&]e=(\d+)", u)
        return (int(m.group(1)), "tv" if s else "movie",
                int(s.group(1)) if s else None,
                int(e.group(1)) if e else None)
    return None


def extract_embed_sources(embed_url: str, client) -> tuple:
    """Run the matching provider extractor on an embed URL.
    -> (display_label, sources). Raises WatchError."""
    hit = next((row for row in EMBED_MAP if row[0] in (embed_url or "")), None)
    if not hit:
        host = urlparse(embed_url or "").netloc or "this server"
        raise WatchError(
            "unsupported",
            f"❌ {host} server မရသေးပါ — တခြား server စမ်းကြည့်ပါ.\n\n"
            f"❌ {host} is not supported yet.")
    _frag, kind, fn_name, label = hit
    fn = getattr(EP, fn_name, None)
    if not fn:
        raise WatchError("unsupported",
                         f"❌ {label} extractor မရှိပါ.\n\n"
                         f"❌ {label} extractor missing.")
    try:
        if kind == "url":
            res = fn(client, embed_url) or {}
        else:
            ids = parse_embed_url(embed_url)
            if not ids:
                raise WatchError("bad_embed",
                                 "❌ embed link ပုံစံ မမှန်ပါ.\n\n"
                                 "❌ Unrecognized embed URL.")
            tid, media, s, e = ids
            res = fn(client, tid, media_type=media, season=s, episode=e) or {}
    except WatchError:
        raise
    except Exception as e:
        raise WatchError(
            "extract_fail",
            f"❌ {label} ဆီက stream ထုတ်မရပါ: {type(e).__name__}\n\n"
            f"❌ {label} extraction failed: {type(e).__name__}")
    srcs = [x for x in (res.get("sources") or []) if x.get("url")]
    if not srcs:
        raise WatchError("no_stream",
                         f"❌ {label} မှာ stream မရှိပါ.\n\n"
                         f"❌ No streams from {label}.")
    return label, srcs

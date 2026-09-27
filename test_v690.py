# test_v690.py — v6.9.0 watch-page resolver (mocked HTML, no network)
import sys

sys.path.insert(0, ".")

import httpx

import watch
from watch import (is_watch_url, parse_watch_url, extract_embeds_from_html,
                   extract_watch_title, parse_embed_url, fetch_watch_embeds,
                   extract_embed_sources, WatchError, SERVER_NAMES)

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name} {extra}")


# ---- URL detect/parse ----
check("detect tv", is_watch_url("https://andyday.sx/watch/tv-lioness-3gs9fwmn"))
check("detect movie", is_watch_url("http://www.andyday.sx/watch/movie-dune-abc123"))
check("reject other", not is_watch_url("https://youtube.com/watch?v=1"))
check("reject stale route", not is_watch_url("https://andyday.sx/watch-tv-lioness-3gs9fwmn"))
p = parse_watch_url("https://andyday.sx/watch/tv-lioness-3gs9fwmn?s=2&e=5")
check("parse tv s/e", p and (p["media_type"], p["season"], p["episode"]) == ("tv", 2, 5), p)
p = parse_watch_url("https://andyday.sx/watch/movie-dune-abc123")
check("parse movie", p and p["media_type"] == "movie" and p["season"] is None)
check("parse none", parse_watch_url("https://example.com/x") is None)

# ---- __OPT extraction ----
HTML = """<html><head><title>Watch Lioness TV Online - Andyday</title>
<meta property="og:title" content="Watch Lioness TV Online - Andyday">
</head><body>
<div id="player" data-embed="https://www.vidking.net/embed/tv/113962/1/1"></div>
<iframe id="embed-iframe" src="https://vidsrc.mov/embed/tv/113962/1/1"></iframe>
<script>window.__OPT=["https://vidsrc.mov/embed/tv/113962/1/1","https://vidsrc.fyi/embed/tv/113962/1/1","https://vidrock.net/tv/113962/1/1","https://vidnest.fun/tv/113962/1/1","https://www.vidking.net/embed/tv/113962/1/1","https://vidlink.pro/tv/113962/1/1?autoplay=true&title=true","https://vidfast.pro/tv/113962/1/1?autoplay=true","https://vidup.to/tv/113962/1/1?autoPlay=true","https://player.videasy.net/tv/113962/1/1","https://111movies.com/tv/113962/1/1","https://www.2embed.cc/embedtv/113962&s=1&e=1","https://multiembed.mov/?video_id=113962&tmdb=1&s=1&e=1","https://superflixapi.co/serie/113962/1/1","https://peachify.top/embed/tv/113962/1/1"];</script>
</body></html>"""

emb = extract_embeds_from_html(HTML)
check("opt count", len(emb) == 14, len(emb))
check("opt index names",
      emb[0][0] == "vidsrc.mov" and emb[3][0] == "vidnest" and emb[13][0] == "peachify")
check("opt urls", emb[2][1] == "https://vidrock.net/tv/113962/1/1")

HTML_IFRAME = '<iframe id="embed-iframe" src="https://vidsrc.mov/embed/tv/1/1/1"></iframe>'
emb = extract_embeds_from_html(HTML_IFRAME)
check("iframe fallback", emb == [("vidsrc.mov", "https://vidsrc.mov/embed/tv/1/1/1")], emb)

HTML_DE = '<div id="player" data-embed="https://x.test/e"></div>'
emb = extract_embeds_from_html(HTML_DE)
check("data-embed fallback", emb == [("vidsrc.mov", "https://x.test/e")], emb)
check("no embeds", extract_embeds_from_html("<html></html>") == [])

# ---- title ----
check("title clean", extract_watch_title(HTML) == "Lioness",
      repr(extract_watch_title(HTML)))
check("title movie year",
      extract_watch_title("<title>Watch Dune (2021) Online - Andyday</title>") == "Dune")
check("title none", extract_watch_title("<html></html>") is None)

# ---- parse_embed_url ----
check("embed tv", parse_embed_url("https://vidsrc.mov/embed/tv/113962/1/1") == (113962, "tv", 1, 1))
check("embed movie", parse_embed_url("https://vidsrc.mov/embed/movie/438631") == (438631, "movie", None, None))
check("embed vidrock", parse_embed_url("https://vidrock.net/tv/113962/2/3") == (113962, "tv", 2, 3))
check("embed vidlink", parse_embed_url("https://vidlink.pro/tv/113962/1/1?autoplay=true&title=true") == (113962, "tv", 1, 1))
check("embed 2embed", parse_embed_url("https://www.2embed.cc/embedtv/113962&s=1&e=1") == (113962, "tv", 1, 1))
check("embed multiembed", parse_embed_url("https://multiembed.mov/?video_id=113962&tmdb=1&s=1&e=1") == (113962, "tv", 1, 1))
check("embed unknown", parse_embed_url("https://x.test/nope") is None)

# ---- fetch_watch_embeds (mocked transport) ----
def h_ok(request):
    return httpx.Response(200, text=HTML)


c = httpx.Client(transport=httpx.MockTransport(h_ok))
title, embeds = fetch_watch_embeds("https://andyday.sx/watch/tv-lioness-3gs9fwmn",
                                   client=c)
check("fetch title+embeds", title == "Lioness" and len(embeds) == 14,
      (title, len(embeds)))
c.close()


def h_ok_se(request):
    assert "s=2" in str(request.url) and "e=5" in str(request.url), request.url
    return httpx.Response(200, text=HTML)


c = httpx.Client(transport=httpx.MockTransport(h_ok_se))
fetch_watch_embeds("https://andyday.sx/watch/tv-lioness-3gs9fwmn",
                   season=2, episode=5, client=c)
check("fetch s/e query", True)
c.close()


def h_404(request):
    return httpx.Response(404, text="nope")


c = httpx.Client(transport=httpx.MockTransport(h_404))
try:
    fetch_watch_embeds("https://andyday.sx/watch/tv-x-1", client=c)
    check("fetch 404 raises", False)
except WatchError as e:
    check("fetch 404 raises", e.kind == "fetch_fail", e.kind)
c.close()

# ---- extract_embed_sources (stubbed extractors) ----
import embed_providers as EP

orig = {}
for fn in ("extract_vidsrc_cloudnestra", "extract_vidrock", "extract_vidnest",
            "extract_vidlink", "extract_videasy", "extract_2embed"):
    orig[fn] = getattr(EP, fn)


def _fake_url(client, embed_url):
    assert "vidsrc.mov" in embed_url, embed_url
    return {"sources": [{"url": "http://cdn/x.mp4", "quality": "1080p",
                         "type": "mp4", "referer": None, "origin": None}]}


def _fake_ids(client, tmdb_id, media_type="movie", season=None, episode=None):
    assert tmdb_id == 113962 and media_type == "tv" and (season, episode) == (1, 1)
    return {"sources": [{"url": "http://cdn/y.mp4", "quality": "720p",
                         "type": "mp4", "referer": None, "origin": None}]}


def _fake_empty(client, tmdb_id, media_type="movie", season=None, episode=None):
    return {"sources": []}


try:
    EP.extract_vidsrc_cloudnestra = _fake_url
    EP.extract_vidrock = _fake_ids
    EP.extract_vidnest = _fake_empty

    c = httpx.Client(transport=httpx.MockTransport(h_ok))
    label, srcs = extract_embed_sources("https://vidsrc.mov/embed/tv/113962/1/1", c)
    check("dispatch url-kind", label == "VidSrc" and srcs[0]["url"] == "http://cdn/x.mp4")
    label, srcs = extract_embed_sources("https://vidrock.net/tv/113962/1/1", c)
    check("dispatch ids-kind", label == "VidRock" and srcs[0]["quality"] == "720p")
    try:
        extract_embed_sources("https://vidnest.fun/tv/113962/1/1", c)
        check("empty sources raises", False)
    except WatchError as e:
        check("empty sources raises", e.kind == "no_stream", e.kind)
    try:
        extract_embed_sources("https://www.vidking.net/embed/tv/113962/1/1", c)
        check("unsupported raises", False)
    except WatchError as e:
        check("unsupported raises", e.kind == "unsupported", e.kind)
    c.close()
finally:
    for fn, f in orig.items():
        setattr(EP, fn, f)

# ---- every EMBED_MAP fn exists ----
import embed_providers
missing = [fn for _, _, fn, _ in watch.EMBED_MAP
           if not callable(getattr(embed_providers, fn, None))]
check("map fns exist", not missing, missing)

# ---- bot.py wiring (static) ----
bot_src = open("bot.py").read()
check("bot has _watch_offer", "async def _watch_offer(" in bot_src)
check("bot has wstream_pick", "async def wstream_pick(" in bot_src)
check("bot has watchlink_cmd", "async def watchlink_cmd(" in bot_src)
check("bot routes wstream",
      're.fullmatch(r"wstream:([0-9a-f]{10}):(\\d+)"' in bot_src)
pline = [l for l in bot_src.splitlines() if "pattern=r\"^(" in l][0]
check("bot pattern includes wstream", "wstream:" in pline, pline[:60])
check("bot registers watchlink", '("watchlink", watchlink_cmd)' in bot_src)
check("bot auto-detects watch urls", "is_watch_url" in bot_src)
check("bot _WATCH_SOURCES registry", "_WATCH_SOURCES" in bot_src)
seg = bot_src.split("async def _watch_offer(")[1].split("async def wstream_pick(")[0]
check("offer skips dead", "provider_looks_dead" in seg)
check("offer reuses stream download", "_stream_download" in
      bot_src.split("async def wstream_pick(")[1].split("async def watchlink_cmd(")[0])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)

"""v6.3.0: YouTube fallback chain (Cobalt -> Piped -> Invidious) — network-free."""
import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

# Network-free tests: stub httpx (unavailable in this sandbox; present on VPS
# via requirements.txt). _get_json/_post_json are never called — resolvers are
# monkeypatched below.
import types as _types

_httpx_stub = _types.ModuleType("httpx")


class _HTTPError(Exception):
    pass


_httpx_stub.HTTPError = _HTTPError
sys.modules["httpx"] = _httpx_stub

import yt_fallback as yf  # noqa: E402  (httpx stubbed above for network-free tests)
from web_download import _is_botwall_error, _retryable_yt_error  # noqa: E402

PASS = []


def check(name, cond):
    PASS.append(name)
    print(("✅ " if cond else "❌ ") + name)
    assert cond, name


# --- 1. video id extraction ---------------------------------------------------
check("watch?v=", yf.youtube_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ")
check("youtu.be", yf.youtube_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ")
check("shorts", yf.youtube_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ")
check("embed", yf.youtube_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ")
check("live", yf.youtube_video_id("https://www.youtube.com/live/dQw4w9WgXcQ") == "dQw4w9WgXcQ")
check("music", yf.youtube_video_id("https://music.youtube.com/watch?v=dQw4w9WgXcQ&x=1") == "dQw4w9WgXcQ")
check("non-yt", yf.youtube_video_id("https://example.com/x") is None)
check("empty", yf.youtube_video_id("") is None)

# --- 2. piped picking ---------------------------------------------------------
PIPED = {
    "title": "Test Video",
    "videoStreams": [
        {"url": "http://v/1080vo", "quality": "1080p", "mimeType": "video/mp4", "videoOnly": True},
        {"url": "http://v/1080", "quality": "1080p", "mimeType": "video/mp4", "videoOnly": False},
        {"url": "http://v/720", "quality": "720p", "mimeType": "video/mp4", "videoOnly": False},
        {"url": "http://v/480w", "quality": "480p", "mimeType": "video/webm", "videoOnly": False},
    ],
    "audioStreams": [
        {"url": "http://a/low", "mimeType": "audio/mp4", "bitrate": 64000, "codec": "mp4a"},
        {"url": "http://a/high", "mimeType": "audio/mp4", "bitrate": 128000, "codec": "mp4a"},
    ],
}
u, t = yf.pick_piped_stream(PIPED, False, "high")
check("piped high picks 1080 muxed mp4", u == "http://v/1080" and t == "Test Video")
u, _ = yf.pick_piped_stream(PIPED, False, "low")
check("piped low caps at 720", u == "http://v/720")
u, _ = yf.pick_piped_stream(PIPED, True, "high")
check("piped audio picks best bitrate", u == "http://a/high")
try:
    yf.pick_piped_stream({"title": "x", "videoStreams": []}, False, "high")
    check("piped no-stream raises", False)
except yf.FallbackError:
    check("piped no-stream raises", True)

# --- 3. invidious picking ------------------------------------------------------
INV = {
    "title": "Inv Video",
    "formatStreams": [
        {"url": "http://i/1080", "qualityLabel": "1080p", "container": "mp4"},
        {"url": "http://i/720", "qualityLabel": "720p", "container": "mp4"},
        {"url": "http://i/720w", "qualityLabel": "720p", "container": "webm"},
    ],
    "adaptiveFormats": [
        {"url": "http://ia/1", "type": "audio/mp4; codecs=mp4a", "bitrate": 128000},
        {"url": "http://ia/2", "type": "audio/webm; codecs=opus", "bitrate": 64000},
    ],
}
u, t = yf.pick_invidious_stream(INV, False, "high")
check("invidious high picks 1080 mp4", u == "http://i/1080" and t == "Inv Video")
u, _ = yf.pick_invidious_stream(INV, False, "low")
check("invidious low caps at 720 mp4", u == "http://i/720")
u, _ = yf.pick_invidious_stream(INV, True, "high")
check("invidious audio picks best", u == "http://ia/1")

# --- 4. cobalt response parsing ------------------------------------------------
u, _ = yf.parse_cobalt_response({"status": "stream", "url": "http://c/v.mp4",
                                 "filename": "youtube_dQw4w9WgXcQ_1080p_h264.mp4"})
check("cobalt stream", u == "http://c/v.mp4")
u, _ = yf.parse_cobalt_response({"status": "tunnel", "url": "http://c/t"})
check("cobalt tunnel", u == "http://c/t")
u, _ = yf.parse_cobalt_response({"status": "redirect", "url": "http://c/r"})
check("cobalt redirect", u == "http://c/r")
u, _ = yf.parse_cobalt_response({"status": "picker",
                                 "picker": [{"url": "http://c/p1"}, {"url": "http://c/p2"}]})
check("cobalt picker takes first", u == "http://c/p1")
try:
    yf.parse_cobalt_response({"status": "error",
                              "error": {"code": "error.api.youtube.login"}})
    check("cobalt error raises", False)
except yf.FallbackError as e:
    check("cobalt error raises", "error.api.youtube.login" in str(e))

# --- 5. instance filtering ------------------------------------------------------
INV_LIST = [
    ["inv.nadeko.net", {"type": "https", "api": False, "uri": "https://inv.nadeko.net",
                        "monitor": {"down": False}}],
    ["invidious.f5.si", {"type": "https", "api": True, "uri": "https://invidious.f5.si",
                         "monitor": {"down": False}}],
    ["dead.example", {"type": "https", "api": True, "uri": "https://dead.example",
                      "monitor": {"down": True}}],
    ["onion.example", {"type": "onion", "uri": "http://onion.example"}],
]
got = yf.filter_invidious_instances(INV_LIST)
check("invidious filter keeps api:true, drops down/onion/api:false",
      got == ["https://invidious.f5.si"])

PIPED_LIST = [
    {"api_url": "https://api.good.example", "up_to_date": True,
     "uptime_24h": 100, "uptime_30d": 99.0},
    {"api_url": "https://api.stale.example", "up_to_date": False,
     "uptime_24h": 100, "uptime_30d": 99.9},
    {"api_url": "https://api.flaky.example", "up_to_date": True,
     "uptime_24h": 50, "uptime_30d": 99.9},
]
got = yf.filter_piped_instances(PIPED_LIST)
check("piped filter keeps healthy+current only", got == ["https://api.good.example"])

# --- 6. orchestrator order + error aggregation (monkeypatched) ------------------
calls = []


def fake_piped(vid, audio_only=False, quality="high", max_instances=2):
    calls.append("piped")
    return "http://p/v.mp4", "Piped Title"


def fake_inv(vid, audio_only=False, quality="high", max_instances=2):
    calls.append("invidious")
    return "http://i/v.mp4", "Inv Title"


yf.piped_resolve, yf.invidious_resolve = fake_piped, fake_inv
yf.COBALT_API_URL = ""  # not configured -> skipped
u, t, src = yf.youtube_fallback_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
check("chain order: piped first", (u, t, src) == ("http://p/v.mp4", "Piped Title", "piped")
      and calls == ["piped"])


def boom(*a, **k):
    raise yf.FallbackError("down")


yf.piped_resolve = boom
u, t, src = yf.youtube_fallback_url("https://youtu.be/dQw4w9WgXcQ")
check("piped fail -> invidious", src == "invidious" and calls[-1] == "invidious")
yf.invidious_resolve = boom
try:
    yf.youtube_fallback_url("https://youtu.be/dQw4w9WgXcQ")
    check("all fail raises", False)
except yf.FallbackError as e:
    check("all fail raises", "piped" in str(e) and "invidious" in str(e))
try:
    yf.youtube_fallback_url("https://example.com/nope")
    check("non-youtube raises", False)
except yf.FallbackError:
    check("non-youtube raises", True)

# --- 7. wiring: download_web calls the fallback for YouTube --------------------
src = open(os.path.join(REPO, "web_download.py")).read()
check("web_download imports youtube_fallback_url",
      "from yt_fallback import youtube_fallback_url" in src)
check("fallback hooked on _is_youtube failure path",
      "youtube_fallback_url, url, audio_only, quality" in src)
check("fallback result verified+normalized",
      "fallback file failed verify" in src)

# --- 8. v6.3.1: bot-wall short-circuits to the fallback (not instant raise) ----
check("botwall markers defined", "_BOTWALL_MARKERS" in src)
check("_is_botwall_error helper defined", "def _is_botwall_error" in src)
check("botwall breaks to fallback", "botwalled = True" in src)
check("botwall flag breaks outer loop", "if result or botwalled:" in src)
check("botwall error no longer in instant-raise path",
      "if _is_youtube(url) and _is_botwall_error(e):" in src)
# the old bug: bot-check error raised before the fallback block could run
check("fallback block reachable after botwall",
      src.index("botwalled = True") < src.index("youtube_fallback_url, url"))


# --- 9. v6.3.2: unbuffered logs so fallback diagnostics reach the journal ----
inst = open(os.path.join(REPO, "install.sh")).read()
check("install.sh runs python -u", "venv/bin/python -u " in inst)
upd = open(os.path.join(REPO, "update.sh")).read()
check("update.sh patches existing service to python -u",
      "python -u" in upd and "daemon-reload" in upd)
check("fallback prints flush immediately",
      src.count("flush=True") >= 3)


# --- 10. v6.3.3: update.sh re-execs itself after git pull ----------------------
upd = open(os.path.join(REPO, "update.sh")).read()
check("update.sh re-execs after pull",
      'exec bash "$0"' in upd and "UPDATE_REEXEC" in upd)
check("re-exec happens after git pull",
      upd.index("git pull") < upd.index('exec bash "$0"'))
check("service -u patch still present",
      "python -u" in upd and "daemon-reload" in upd)

print(f"\nPASS: {len(PASS)} checks")

# --- 11. v6.3.4: curly-apostrophe bot-wall message (U+2019) --------------------
CURLY = ("ERROR: [youtube] EQmKsw80WrE: Sign in to confirm you\u2019re not a bot. "
         "Use --cookies-from-browser or --cookies for the authentication.")
check("botwall detected with curly apostrophe U+2019",
      _is_botwall_error(Exception(CURLY)))
check("curly bot-wall NOT treated as plain retryable",
      not _retryable_yt_error(Exception(CURLY)))
check("botwall still detected with ASCII apostrophe",
      _is_botwall_error(Exception(CURLY.replace("\u2019", "'"))))
check("non-botwall error not flagged",
      not _is_botwall_error(Exception("ERROR: Video unavailable")))

print(f"\nPASS: {len(PASS)} checks")

# test_v682.py — v6.8.2 stream quality+size picker (embed.py helpers + bot.py wiring)
import re
import sys

import httpx

sys.path.insert(0, ".")

from embed import (fmt_size, probe_source_size, stream_src_label,
                   filter_oversize, pick_best, MAX_MB)

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name} {extra}")


# ---- fmt_size ----
check("fmt None -> —", fmt_size(None) == "—")
check("fmt 0 -> 0B", fmt_size(0) == "0B")
check("fmt 500 -> 500B", fmt_size(500) == "500B")
check("fmt 1023 -> 1023B", fmt_size(1023) == "1023B")
check("fmt 1024 -> 1KB", fmt_size(1024) == "1KB")
check("fmt 1536 -> 1.5KB", fmt_size(1536) == "1.5KB")
check("fmt 1MB -> 1MB", fmt_size(1048576) == "1MB")
check("fmt 1.5MB", fmt_size(1572864) == "1.5MB")
check("fmt 1GB -> 1GB", fmt_size(1073741824) == "1GB")
check("fmt 2.5GB", fmt_size(int(2.5 * 1073741824)) == "2.5GB")


# ---- probe_source_size (mocked transport; Range GET is tried first) ----
def mkclient(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def h_range_authoritative(request):
    # server lies on HEAD, tells truth on Range GET
    if request.method == "GET":
        assert request.headers.get("range") == "bytes=0-0", request.headers.get("range")
        return httpx.Response(206, headers={"content-range": "bytes 0-0/5555555555"})
    return httpx.Response(200, headers={"content-length": "12345678"})


c = mkclient(h_range_authoritative)
check("probe prefers content-range over HEAD",
      probe_source_size({"url": "https://x.test/v.mp4"}, client=c) == 5555555555)
c.close()


def h_range_ignored(request):
    # no Range support: 200 + content-length on the GET itself
    if request.method == "GET":
        return httpx.Response(200, headers={"content-length": "777"},
                              content=iter([b"x"]))
    return httpx.Response(405, headers={})


c = mkclient(h_range_ignored)
check("probe Range-ignored -> GET content-length",
      probe_source_size({"url": "https://x.test/v.mp4"}, client=c) == 777)
c.close()


def h_range_403(request):
    if request.method == "GET":
        return httpx.Response(403, headers={})
    return httpx.Response(200, headers={"content-length": "424242"})


c = mkclient(h_range_403)
check("probe Range 403 -> HEAD fallback",
      probe_source_size({"url": "https://x.test/v.mp4"}, client=c) == 424242)
c.close()


def h_no_length(request):
    if request.method == "HEAD":
        return httpx.Response(200, headers={})
    # streaming body -> no content-length header at all
    return httpx.Response(200, headers={}, content=iter([b"hello"]))


c = mkclient(h_no_length)
check("probe no length anywhere -> None",
      probe_source_size({"url": "https://x.test/v.mp4"}, client=c) is None)
c.close()


def h_should_not_run(request):  # pragma: no cover
    raise AssertionError("HLS must not hit network")


c = mkclient(h_should_not_run)
check("probe HLS skipped (no request)",
      probe_source_size({"url": "https://x.test/master.m3u8?x=1"}, client=c) is None)
c.close()


seen = {}


def h_referer(request):
    seen["referer"] = request.headers.get("referer")
    seen["ua"] = request.headers.get("user-agent")
    return httpx.Response(200, headers={"content-length": "42"})


c = mkclient(h_referer)
probe_source_size({"url": "https://x.test/v.mp4",
                   "referer": "https://ref.test/",
                   "origin": "https://ref.test/"}, client=c)
c.close()
check("probe forwards Referer", seen.get("referer") == "https://ref.test/", seen)
check("probe sends UA", bool(seen.get("ua")))


def h_head_boom(request):  # pragma: no cover
    raise httpx.ConnectError("down")


c = mkclient(h_head_boom)
check("probe network error -> None",
      probe_source_size({"url": "https://x.test/v.mp4"}, client=c) is None)
c.close()


# ---- stream_src_label ----
check("label best",
      stream_src_label({"quality": "1080p"}, 1500000000, True) == "⭐ 1080p • 1.4GB")
check("label plain",
      stream_src_label({"quality": "720p"}, 800 * 1048576) == "📥 720p • 800MB")
check("label unknown size",
      stream_src_label({"quality": "auto"}, None) == "📥 auto • —")
check("label blank quality",
      stream_src_label({"quality": "  "}, 500) == "⚠️ auto • 500B")
check("label small warns",
      stream_src_label({"quality": "360p"}, 952832) == "⚠️ 360p • 930.5KB")
check("label small best warns",
      stream_src_label({"quality": "1080p"}, 952832, True) == "⚠️ 1080p • 930.5KB")


# ---- filter_oversize ----
srcs = [{"url": "a", "quality": "1080p"}, {"url": "b", "quality": "720p"},
        {"url": "c", "quality": "480p"}, {"url": "d", "quality": "4k"}]
sizes = [100 * 1048576, None, MAX_MB * 1048576, (MAX_MB + 1) * 1048576]
kept, ksizes, dropped = filter_oversize(srcs, sizes)
check("oversize drops 1", dropped == 1 and len(kept) == 3, f"dropped={dropped}")
check("oversize keeps order", [s["url"] for s in kept] == ["a", "b", "c"])
check("oversize boundary kept", ksizes[2] == MAX_MB * 1048576)


# ---- pick_best sanity (mp4 preferred) ----
s_mp4 = {"url": "x.mp4", "quality": "720p", "type": "mp4"}
s_hls = {"url": "y.m3u8", "quality": "1080p", "type": "hls"}
check("pick_best prefers mp4", pick_best([s_hls, s_mp4]) is s_mp4)


# ---- sstream callback regex ----
pat = r"sstream:(\d+):(\d+)"
m = re.fullmatch(pat, "sstream:123456:2")
check("sstream regex ok", m and m.groups() == ("123456", "2"))
check("sstream regex rejects",
      re.fullmatch(pat, "sstream:abc:1") is None
      and re.fullmatch(pat, "stream:movie:1") is None)


# ---- bot.py wiring (static) ----
bot_src = open("bot.py").read()
check("bot has _stream_offer", "async def _stream_offer(" in bot_src)
check("bot has sstream_pick", "async def sstream_pick(" in bot_src)
check("bot routes sstream", 'r"sstream:(\\d+):(\\d+)"' in bot_src)
check("bot pattern includes sstream", "sstream:" in bot_src.split(
    'pattern=r"^(')[1].split('"))')[0] if 'pattern=r"^(' in bot_src else "sstream:" in bot_src)
check("bot _STREAM_SOURCES", "_STREAM_SOURCES: dict" in bot_src)
check("picker callback format", 'f"sstream:{tmdb_id}:{i}"' in bot_src)
check("download_embed reuse", "download_embed, source" in bot_src)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)

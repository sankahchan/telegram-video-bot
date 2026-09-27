# test_v683.py — v6.8.3 dead-provider skipping (collect_provider_streams + provider_looks_dead)
import sys

sys.path.insert(0, ".")

import embed
from embed import (provider_looks_dead, collect_provider_streams,
                   resolve_streams, EmbedError, SMALL_WARN_BYTES)

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name} {extra}")


# ---- provider_looks_dead ----
check("dead: all tiny", provider_looks_dead([952832, 952832]) is True)
check("dead: tiny + unknown", provider_looks_dead([952832, None]) is True)
check("alive: one healthy", provider_looks_dead([952832, 1500000000]) is False)
check("alive: all unknown", provider_looks_dead([None, None]) is False)
check("alive: empty", provider_looks_dead([]) is False)
check("alive: exactly at warn line",
      provider_looks_dead([SMALL_WARN_BYTES]) is False)
check("dead: just under warn line",
      provider_looks_dead([SMALL_WARN_BYTES - 1]) is True)


# ---- collect_provider_streams (stubbed providers, no network) ----
def _src(url, q="1080p", t="mp4"):
    return {"url": url, "quality": q, "type": t,
            "referer": None, "origin": None}


def _p1(client, tmdb_id, media_type="movie", season=None, episode=None):
    return {"sources": [_src("http://p1/x.mp4")]}


def _p2(client, tmdb_id, media_type="movie", season=None, episode=None):
    return {"sources": [_src("http://p2/y.mp4", "720p")]}


def _p3boom(client, tmdb_id, media_type="movie", season=None, episode=None):
    raise RuntimeError("boom")


def _p4empty(client, tmdb_id, media_type="movie", season=None, episode=None):
    return {"sources": [{"url": "", "quality": "1080p"}]}


orig_cascade = embed.CASCADE
orig_cloud = embed.EP.extract_vidsrc_cloudnestra
try:
    embed.CASCADE = [("P1", _p1), ("P3boom", _p3boom), ("P4empty", _p4empty),
                     ("P2", _p2)]
    embed.EP.extract_vidsrc_cloudnestra = lambda c, url: {"sources": []}
    res = collect_provider_streams(123)
    check("collect order+skip",
          [l for l, _ in res] == ["P1", "P2"], [l for l, _ in res])
    check("collect sources kept",
          res[0][1][0]["url"] == "http://p1/x.mp4")

    label, srcs = resolve_streams(123)
    check("resolve first wins", label == "P1" and srcs[0]["url"] == "http://p1/x.mp4")

    embed.CASCADE = []
    try:
        resolve_streams(123)
        check("resolve empty raises", False)
    except EmbedError as e:
        check("resolve empty raises", e.kind == "no_stream", e.kind)
finally:
    embed.CASCADE = orig_cascade
    embed.EP.extract_vidsrc_cloudnestra = orig_cloud


# ---- bot.py wiring (static) ----
bot_src = open("bot.py").read()
seg = bot_src.split("async def _stream_offer(")[1].split("async def sstream_pick(")[0]
check("offer collects providers", "collect_provider_streams" in seg)
check("offer checks dead", "provider_looks_dead" in seg)
check("offer dead-links error", "Only dead links found" in seg)
check("offer skipped note", "Skipped dead links from" in seg)
check("offer no old resolve", "resolve_streams" not in seg)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)

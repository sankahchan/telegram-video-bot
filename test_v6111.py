"""v6.11.1 — Chan's javhd.icu link end-to-end simulation.

Simulates the real site behavior discovered 2026-09-27:
- ?tape=2 -> hglink.to embed (JS "Loading..." wall, no direct media)
- ?tape=1 -> turbovidhls embed (plain HTML with direct .m3u8)
- ?tape=3 -> bingezove embed (redirects, no media)

Asserts download_web resolves the direct m3u8 and tries it FIRST,
without any real network (mocked _fetch_html + FakeYDL).
Run: python3 test_v6111.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import web_download as wd
import yt_dlp

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


M3U8 = "https://cdn3.turboviplay.com/data3/68d006bb304dd/68d006bb304dd.m3u8"


def page(iframe_src):
    return (f'<html><body><div class="player">'
            f'<IFRAME SRC="{iframe_src}" FRAMEBORDER=0></IFRAME>'
            f'</div><iframe src="https://a.magsrv.com/iframe.php?idzone=1"></iframe>'
            f'</body></html>')


PAGES = {
    "?tape=2": page("https://hglink.to/e/hosj6oztfdr5"),
    "?tape=1": page("https://turbovidhls.com/t/68d006bb304dd"),
    "?tape=3": page("https://bingezove.com/embed/4guyw107lyg1"),
}
EMBEDS = {
    "hglink.to": "<html><head><title>Loading...</title></head><body>wait...</body></html>",
    "turbovidhls.com": f'<html><body><script>var f="{M3U8}";</script></body></html>',
    "bingezove.com": "<html><body>redirect</body></html>",
}


async def fake_fetch(url, timeout=15):
    for k, v in PAGES.items():
        if k in url:
            return v
    for k, v in EMBEDS.items():
        if k in url:
            return v
    raise ConnectionError("unknown url " + url)


calls = []
tmp = tempfile.mkdtemp()


class FakeYDL:
    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=True):
        calls.append(url)
        assert url == M3U8, f"expected direct m3u8 first, got {url}"
        p = os.path.join(tmp, "vid.mp4")
        open(p, "wb").write(b"x" * 64)
        return {"id": "vid", "title": "JUR-466", "ext": "mp4"}

    def prepare_filename(self, info):
        return os.path.join(tmp, "vid.mp4")


print("== javhd ?tape=2 full flow ==")
orig_fetch = wd._fetch_html
orig_ydl = yt_dlp.YoutubeDL
orig_verify = wd.verify_web_video
orig_norm = wd.normalize_web_video
orig_probe = wd._hls_first_segment_ok
wd._fetch_html = fake_fetch
yt_dlp.YoutubeDL = FakeYDL
wd.verify_web_video = lambda p: (True, "")
wd.normalize_web_video = lambda p: p
# v6.13.0: the HLS segment probe does real network — stub it so the
# ordering simulation stays offline (the probe itself is covered in
# test_v6130.py against a local HTTP server)
wd._hls_first_segment_ok = lambda url: True
try:
    url = "https://javhd.icu/video/jav-hd-uncensored-leaked-jur-466-nanami-tina/?tape=2"
    path, title = asyncio.run(wd.download_web(url, tmp))
    check("tape=2 (JS-walled) skipped to tape=1's m3u8", calls == [M3U8], calls)
    check("single yt-dlp call, no 6x retry", len(calls) == 1, calls)
    check("video returned", os.path.exists(path) and title == "JUR-466", (path, title))

    # tape variants order: pasted first
    tv = wd._tape_variants(url)
    check("pasted tape first", tv[0] == url and len(tv) == 3, tv)

    # v6.13.0: dead HLS (placeholder segments, e.g. turbovid PNGs) is
    # dropped BEFORE yt-dlp runs — fail fast with a clear bilingual error
    # instead of a long doomed download + unplayable file.
    async def fake_resolve_dead(url):
        return (["https://cdn.example.com/dead.m3u8"], [])
    wd._resolve_embeds = fake_resolve_dead
    wd._hls_first_segment_ok = lambda url: False
    calls.clear()
    try:
        asyncio.run(wd.download_web("https://example.com/videopage", tmp))
        check("dead HLS raises", False, "no exception")
    except RuntimeError as e:
        check("dead HLS raises bilingual block error",
              "block" in str(e).lower(), str(e)[:80])
    check("yt-dlp never called for dead HLS", calls == [], calls)
finally:
    wd._fetch_html = orig_fetch
    yt_dlp.YoutubeDL = orig_ydl
    wd.verify_web_video = orig_verify
    wd.normalize_web_video = orig_norm
    wd._hls_first_segment_ok = orig_probe

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)

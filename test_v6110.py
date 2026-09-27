"""v6.11.x tests — deep embed resolution, tape variants, candidate loop.

- _page_iframes: all iframes minus ad hosts, protocol-relative, dedup
- _direct_media_urls: m3u8/mp4 scraping incl. \\/ escapes
- _tape_variants: ?tape=N sibling expansion
- _resolve_embeds (mocked httpx): media + embed extraction
- download_web (FakeYDL): candidate order, unsupported->next, single
  client attempt for non-YouTube, cancel honored
Run: python3 test_v6110.py
"""
import asyncio
import os
import sys
import tempfile
import threading

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


def _run(coro):
    return asyncio.run(coro)


# ---------- _page_iframes ----------
print("== _page_iframes ==")
HTML = """
<html><body>
<IFRAME SRC="https://hglink.to/e/hosj6oztfdr5" FRAMEBORDER=0></IFRAME>
<iframe src="https://turbovidhls.com/t/68d006bb304dd"></iframe>
<iframe src="//doodstream.com/e/abc123"></iframe>
<iframe src="https://a.magsrv.com/iframe.php?idzone=1"></iframe>
<iframe src="https://tsyndicate.com/iframes2/x.html"></iframe>
<iframe src="/local/player.html"></iframe>
<iframe src="https://turbovidhls.com/t/68d006bb304dd"></iframe>
</body></html>"""
srcs = wd._page_iframes(HTML)
check("finds uppercase IFRAME", any("hglink.to/e/hosj6oztfdr5" in s for s in srcs), srcs)
check("finds turbovid", any("turbovidhls.com" in s for s in srcs), srcs)
check("protocol-relative -> https", any(s == "https://doodstream.com/e/abc123" for s in srcs), srcs)
check("ignores magsrv ad", not any("magsrv" in s for s in srcs))
check("ignores tsyndicate ad", not any("tsyndicate" in s for s in srcs))
check("ignores relative src", not any("local/player" in s for s in srcs))
check("dedups", len(srcs) == len(set(srcs)) == 3, srcs)
check("empty html -> []", wd._page_iframes("<html></html>") == [])

# ---------- _direct_media_urls ----------
print("== _direct_media_urls ==")
MHTML = ('<script>var src="https:\\/\\/cdn3.turboviplay.com\\/data3\\/ab\\/ab.m3u8?x=1";</script>'
         '<video><source src="https://cdn.example.com/v.mp4"></video>')
mu = wd._direct_media_urls(MHTML)
check("m3u8 unescaped", "https://cdn3.turboviplay.com/data3/ab/ab.m3u8?x=1" in mu, mu)
check("mp4 found", "https://cdn.example.com/v.mp4" in mu, mu)
check("none -> []", wd._direct_media_urls("<html>hi</html>") == [])

# ---------- _tape_variants ----------
print("== _tape_variants ==")
tv = wd._tape_variants("https://javhd.icu/video/x/?tape=2")
check("tape variants", tv == [
    "https://javhd.icu/video/x/?tape=2",
    "https://javhd.icu/video/x/?tape=1",
    "https://javhd.icu/video/x/?tape=3"], tv)
check("no tape -> [url]", wd._tape_variants("https://example.com/v") == ["https://example.com/v"])
check("&tape form", wd._tape_variants("https://e.com/v?a=1&tape=2") == [
    "https://e.com/v?a=1&tape=2", "https://e.com/v?a=1&tape=1", "https://e.com/v?a=1&tape=3"])

# ---------- _resolve_embeds (mocked _fetch_html) ----------
print("== _resolve_embeds ==")
EMBED_HTML = '<html><body><video src="https://cdn3.turboviplay.com/data3/ab/ab.m3u8"></video></body></html>'


async def _fake_fetch(url, timeout=15):
    if "videopage" in url:
        return HTML
    if "turbovidhls" in url or "hglink" in url or "doodstream" in url:
        return EMBED_HTML
    raise ConnectionError("down")


orig_fetch = wd._fetch_html
wd._fetch_html = _fake_fetch
try:
    media, embeds = _run(wd._resolve_embeds("https://example.com/videopage"))
    check("media resolved", media == ["https://cdn3.turboviplay.com/data3/ab/ab.m3u8"], (media, embeds))
    check("embeds listed", len(embeds) == 3 and all(e.startswith("https://") for e in embeds), embeds)
    check("no ad embeds", not any("magsrv" in e or "tsyndicate" in e for e in embeds))
    m2, e2 = _run(wd._resolve_embeds("https://example.com/v.mp4"))
    check("direct file skipped", (m2, e2) == ([], []))
finally:
    wd._fetch_html = orig_fetch

# ---------- _is_unsupported_url ----------
print("== _is_unsupported_url ==")
check("matches message", wd._is_unsupported_url(Exception("Unsupported URL: https://x")))
check("matches class name", wd._is_unsupported_url(type("UnsupportedError", (Exception,), {})()))
check("other error false", not wd._is_unsupported_url(RuntimeError("boom")))

# ---------- download_web with FakeYDL ----------
print("== download_web candidates ==")
calls = []
tmp = tempfile.mkdtemp()


class FakeYDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=True):
        calls.append(url)
        if "badembed" in url:
            raise Exception("Unsupported URL: badembed")
        if "boompage" in url:
            raise RuntimeError("boom")
        if "retrypage" in url:
            raise Exception("HTTP Error 429: too many")
        p = os.path.join(tmp, "abc123.mp4")
        with open(p, "wb") as f:
            f.write(b"x" * 64)
        return {"id": "abc123", "title": "T", "ext": "mp4"}

    def prepare_filename(self, info):
        return os.path.join(tmp, f"{info['id']}.{info['ext']}")


orig_ydl = yt_dlp.YoutubeDL
orig_resolve = wd._resolve_embeds
orig_tape = wd._tape_variants
orig_verify = wd.verify_web_video
orig_norm = wd.normalize_web_video
orig_probe = wd._hls_first_segment_ok
yt_dlp.YoutubeDL = FakeYDL
wd.verify_web_video = lambda p: (True, "")
wd.normalize_web_video = lambda p: p
# v6.13.0: the HLS segment probe does real network — stub it here so the
# candidate-order tests stay offline (covered for real in test_v6130.py)
wd._hls_first_segment_ok = lambda url: True
wd._tape_variants = lambda url: [url]  # isolate candidate-order tests
try:
    async def fake_resolve(url):
        return (["https://cdn.example.com/direct.m3u8"],
                ["https://streambad.com/e/badembed"])

    wd._resolve_embeds = fake_resolve
    calls.clear()
    path, title = _run(wd.download_web("https://example.com/videopage", tmp))
    check("direct media tried first", calls[0] == "https://cdn.example.com/direct.m3u8", calls)
    check("only one call on success", len(calls) == 1, calls)
    check("returns path", os.path.exists(path) and title == "T")

    async def fake_resolve2(url):
        return ([], ["https://streambad.com/e/badembed"])

    wd._resolve_embeds = fake_resolve2
    calls.clear()
    path, title = _run(wd.download_web("https://example.com/videopage", tmp))
    check("unsupported embed -> page url next",
          calls == ["https://streambad.com/e/badembed", "https://example.com/videopage"], calls)

    # non-YouTube: single client attempt on hard failure
    async def no_media(url):
        return ([], [])

    wd._resolve_embeds = no_media
    calls.clear()
    try:
        _run(wd.download_web("https://example.com/boompage", tmp))
        check("hard failure raises", False)
    except RuntimeError as e:
        check("hard failure raises", "boom" in str(e))
    check("non-YouTube single attempt (no 6x clients)", len(calls) == 1, calls)

    # cancel between candidates
    wd._resolve_embeds = fake_resolve
    calls.clear()
    ev = threading.Event()
    ev.set()
    try:
        _run(wd.download_web("https://example.com/videopage", tmp, cancel_event=ev))
        check("cancel raises", False)
    except wd.WebDownloadCancelled:
        check("cancel raises", True)
    check("cancel: no yt-dlp call started", calls == [], calls)

    # YouTube keeps 6 client variants (retryable error -> 6 clients x 2 fmts
    # + 1 _diagnose_formats probe)
    wd._resolve_embeds = no_media
    calls.clear()
    try:
        _run(wd.download_web("https://youtube.com/watch?v=retrypage", tmp))
        check("yt retryable raises", False)
    except Exception as e:
        check("yt retryable raises", "429" in str(e), str(e)[:60])
    check("YouTube still tries 6 clients",
          len(calls) == 13 and all(c == "https://youtube.com/watch?v=retrypage" for c in calls),
          len(calls))
finally:
    yt_dlp.YoutubeDL = orig_ydl
    wd._resolve_embeds = orig_resolve
    wd._tape_variants = orig_tape
    wd.verify_web_video = orig_verify
    wd.normalize_web_video = orig_norm
    wd._hls_first_segment_ok = orig_probe

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)

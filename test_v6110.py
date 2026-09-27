"""v6.11.0 tests — embed fast-path, single-attempt non-YouTube, cancel, watchdog.

- _embed_srcs: extracts known video-host iframes, ignores ads/other hosts
- _scan_page_embeds: mocked HTTP — html/non-html/exception paths
- download_web (FakeYDL): embed tried BEFORE page URL; unsupported embed ->
  next candidate; non-YouTube = single client attempt; cancel honored
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


# ---------- _embed_srcs ----------
print("== _embed_srcs ==")
HTML = """
<html><body>
<iframe src="https://streamtape.com/e/vxxAj906xWHDew/"></iframe>
<iframe src="//doodstream.com/e/abc123"></iframe>
<iframe src="https://a.magsrv.com/iframe.php?idzone=5736398"></iframe>
<iframe src="https://www.youtube.com/embed/xyz"></iframe>
<iframe src="/local/player.html"></iframe>
<video src="https://filemoon.sx/e/vid1"></video>
<iframe src="https://streamtape.com/e/vxxAj906xWHDew/"></iframe>
</body></html>"""
srcs = wd._embed_srcs(HTML)
check("finds streamtape", any("streamtape.com/e/vxxAj906xWHDew" in s for s in srcs), srcs)
check("protocol-relative -> https", any(s == "https://doodstream.com/e/abc123" for s in srcs), srcs)
check("finds filemoon video tag", any("filemoon.sx/e/vid1" in s for s in srcs), srcs)
check("ignores ad iframe", not any("magsrv" in s for s in srcs))
check("ignores youtube iframe", not any("youtube" in s for s in srcs))
check("ignores relative src", not any("local/player" in s for s in srcs))
check("dedups", len(srcs) == len(set(srcs)) == 3, srcs)
check("empty html -> []", wd._embed_srcs("<html></html>") == [])

# ---------- _scan_page_embeds (mocked httpx) ----------
print("== _scan_page_embeds ==")


class _FakeResp:
    def __init__(self, text, ctype="text/html"):
        self.text = text
        self.headers = {"content-type": ctype}


class _FakeClient:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        if self._exc:
            raise self._exc
        return self._resp


def _run(coro):
    return asyncio.run(coro)


orig_client = wd.httpx.AsyncClient
try:
    wd.httpx.AsyncClient = lambda **k: _FakeClient(_FakeResp(HTML))
    got = _run(wd._scan_page_embeds("https://example.com/v"))
    check("scan returns embeds", len(got) == 3, got)

    wd.httpx.AsyncClient = lambda **k: _FakeClient(_FakeResp("%PDF-1.4", "application/pdf"))
    check("non-html -> []", _run(wd._scan_page_embeds("https://example.com/f")) == [])

    wd.httpx.AsyncClient = lambda **k: _FakeClient(exc=ConnectionError("down"))
    check("exception -> []", _run(wd._scan_page_embeds("https://example.com/v")) == [])

    check("direct file skipped", _run(wd._scan_page_embeds("https://example.com/v.mp4")) == [])
finally:
    wd.httpx.AsyncClient = orig_client

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
orig_scan = wd._scan_page_embeds
orig_verify = wd.verify_web_video
orig_norm = wd.normalize_web_video
yt_dlp.YoutubeDL = FakeYDL
wd.verify_web_video = lambda p: (True, "")
wd.normalize_web_video = lambda p: p
try:
    async def fake_scan(url):
        return ["https://streambad.com/e/badembed", "https://streamtape.com/e/good123"]

    wd._scan_page_embeds = fake_scan
    calls.clear()
    path, title = _run(wd.download_web("https://example.com/videopage", tmp))
    check("embed tried before page", calls[0] == "https://streambad.com/e/badembed", calls)
    check("unsupported embed -> next candidate", calls[1] == "https://streamtape.com/e/good123", calls)
    check("page url never needed", len(calls) == 2, calls)
    check("returns path", os.path.exists(path) and title == "T")

    # non-YouTube: single client attempt on hard failure
    async def no_embeds(url):
        return []
    wd._scan_page_embeds = no_embeds
    calls.clear()
    try:
        _run(wd.download_web("https://example.com/boompage", tmp))
        check("hard failure raises", False)
    except RuntimeError as e:
        check("hard failure raises", "boom" in str(e))
    check("non-YouTube single attempt (no 6x clients)", len(calls) == 1, calls)

    # cancel between candidates
    wd._scan_page_embeds = fake_scan
    calls.clear()
    ev = threading.Event()
    ev.set()
    try:
        _run(wd.download_web("https://example.com/videopage", tmp, cancel_event=ev))
        check("cancel raises", False)
    except wd.WebDownloadCancelled:
        check("cancel raises", True)
    check("cancel: no yt-dlp call started", calls == [], calls)

    # YouTube keeps 6 client variants (retryable error -> 6 clients x 2 fmts)
    wd._scan_page_embeds = no_embeds
    calls.clear()
    try:
        _run(wd.download_web("https://youtube.com/watch?v=retrypage", tmp))
        check("yt retryable raises", False)
    except Exception as e:
        check("yt retryable raises", "429" in str(e), str(e)[:60])
    # 12 = 6 clients x 2 fmts in the retry loop, +1 = _diagnose_formats probe
    check("YouTube still tries 6 clients", len(calls) == 13 and
          all(c == "https://youtube.com/watch?v=retrypage" for c in calls), calls)
finally:
    yt_dlp.YoutubeDL = orig_ydl
    wd._scan_page_embeds = orig_scan
    wd.verify_web_video = orig_verify
    wd.normalize_web_video = orig_norm

print(f"\n{ PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)

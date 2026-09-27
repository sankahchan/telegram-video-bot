"""v6.13.0 tests — HLS segment validation + parallel watch-server probing.

Covers the two live incidents of 2026-09-27:
1) turbovid HLS served PNG placeholder "segments" to datacenter IPs -> a
   "successful" download produced an unplayable 0:00 file. Fix: probe the
   first segment's magic bytes before the expensive yt-dlp run, drop dead
   playlists, and reject zero-duration files in verify_web_video.
2) andyday.sx watch page froze at "watch page ဖတ်နေပါတယ်..." for 6+ min
   because 14 servers were probed sequentially (14 x 25s timeouts).
   Fix: parallel batched probing with progress updates.
"""
import asyncio
import functools
import http.server
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from web_download import _hls_first_segment_ok, verify_web_video  # noqa: E402

PNG_HEAD = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)   # fake PNG segment
TS_HEAD = b"\x47" + b"\x00" * 187                    # MPEG-TS sync byte
FTYP_HEAD = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100  # fMP4 init


class _Handler(http.server.BaseHTTPRequestHandler):
    mode = "png"  # class-level switch per test

    def _send(self, code, ctype, body):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        # honor Range: bytes=0-2047 (probe only reads the head)
        self.wfile.write(raw[:2048] if "Range" in self.headers else raw)

    def do_GET(self):
        mode = _Handler.mode
        if self.path == "/master.m3u8":
            self._send(200, "application/vnd.apple.mpegurl",
                       "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n"
                       "/variant.m3u8\n")
        elif self.path == "/variant.m3u8":
            self._send(200, "application/vnd.apple.mpegurl",
                       "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                       "#EXTINF:6.0,\n/seg0.ts\n#EXTINF:6.0,\n/seg1.ts\n")
        elif self.path == "/direct.m3u8":  # media playlist, no master hop
            self._send(200, "application/vnd.apple.mpegurl",
                       "#EXTM3U\n#EXTINF:6.0,\n/seg0.ts\n")
        elif self.path.startswith("/seg"):
            if mode == "png":
                self._send(200, "image/png", PNG_HEAD)
            elif mode == "ts":
                self._send(200, "video/mp2t", TS_HEAD)
            elif mode == "ftyp":
                self._send(200, "video/mp4", FTYP_HEAD)
            elif mode == "denied":
                self._send(429, "text/html", "<html>rate limited</html>")
            elif mode == "html":
                self._send(200, "text/html", "<html>bot check</html>")
        else:
            self._send(404, "text/plain", "nope")

    def log_message(self, *a):
        pass


_server = None
_base = None


def _start_server():
    global _server, _base
    if _server is not None:
        return _base
    _server = http.server.HTTPServer(("127.0.0.1", 0),
                                     _Handler)
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    _base = f"http://127.0.0.1:{_server.server_address[1]}"
    return _base


def _probe(mode, playlist="/master.m3u8"):
    _Handler.mode = mode
    base = _start_server()
    return _hls_first_segment_ok(base + playlist)


def test_png_segments_rejected():
    assert _probe("png") is False


def test_ts_segments_accepted():
    assert _probe("ts") is True


def test_ftyp_segments_accepted():
    assert _probe("ftyp") is True


def test_429_segments_rejected():
    assert _probe("denied") is False


def test_html_segments_rejected():
    assert _probe("html") is False


def test_direct_media_playlist_ts():
    assert _probe("ts", "/direct.m3u8") is True


def test_direct_media_playlist_png():
    assert _probe("png", "/direct.m3u8") is False


def test_bad_playlist_url_rejected():
    base = _start_server()
    assert _hls_first_segment_ok(base + "/nope.m3u8") is False


# ---- verify_web_video: zero-duration hole (v6.13.0) -------------------------
def test_verify_rejects_png_masquerading_as_mp4():
    import shutil as _sh
    import tempfile
    if not _sh.which("ffprobe"):
        print("SKIP test_verify_rejects_png_masquerading_as_mp4 (no ffprobe)")
        return
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "fake.mp4")
        with open(p, "wb") as f:
            f.write(PNG_HEAD * 3000)  # > 100KB of PNG data, .mp4 name
        ok, reason = verify_web_video(p)
        assert ok is False, f"should reject, got: {reason}"


# ---- bot.py helpers: loaded without importing bot (needs venv deps) ---------
_bot_ns = None


def _bot_fn(name):
    """Extract one top-level (async) function from bot.py and exec it in an
    isolated namespace with stubs for its cross-module helpers."""
    global _bot_ns
    import ast
    if _bot_ns is None:
        _bot_ns = {
            "asyncio": asyncio, "time": time,
            # stubs for helpers imported from other modules in bot.py
            "pot_server_hint": lambda: "",
            "storyboard_only": lambda s: False,
        }
    if name not in _bot_ns:
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "bot.py")).read()
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == name:
                seg = ast.get_source_segment(src, node)
                exec(compile(ast.parse(seg), "bot.py", "exec"), _bot_ns)
                break
        assert name in _bot_ns, f"{name} not found in bot.py"
    return _bot_ns[name]


# ---- friendly_web_error passthrough (v6.13.0) -------------------------------
def test_friendly_web_error_passthrough():
    friendly_web_error = _bot_fn("friendly_web_error")
    msg = ("❌ video source က download block လုပ်ထားပါတယ်\n\n"
           "❌ The video host is blocking downloads")
    out = friendly_web_error(RuntimeError(msg))
    assert out == msg, f"expected passthrough, got: {out!r}"


# ---- _pick_watch_server: parallel batches, order kept -----------------------
def test_pick_watch_server_parallel_and_ordered():
    _pick_watch_server = _bot_fn("_pick_watch_server")

    calls = []

    async def fake_try(item):
        name, _url = item
        calls.append(name)
        # server "s3" is healthy but slow; "s1" is fast but dead
        await asyncio.sleep(0.4 if name == "s3" else 0.05)
        if name == "s1":
            return (name, "dead", None)
        if name == "s2":
            return (name, "unsupported", None)
        if name == "s3":
            return (name, "ok", ("LBL", ["u"], [1], 0))
        return (name, "dead", None)

    progress = []

    async def prog(done, total):
        progress.append((done, total))

    embeds = [(f"s{i}", f"http://x/{i}") for i in range(1, 9)]
    t0 = time.time()
    payload, skipped, unsupported = asyncio.run(
        _pick_watch_server(embeds, fake_try, prog, batch=4))
    dt = time.time() - t0

    assert payload == ("LBL", ["u"], [1], 0)
    assert skipped == ["s1"], f"skipped={skipped}"
    assert unsupported == ["s2"], f"unsupported={unsupported}"
    # batch 1 (s1..s4) ran in parallel: ~0.4s not ~1.0s+; and we stopped
    # after the first batch because s3 was healthy (s5..s8 never probed)
    assert dt < 1.5, f"not parallel or not early-exit: {dt:.2f}s"
    assert calls == ["s1", "s2", "s3", "s4"], f"calls={calls}"
    assert progress == [(4, 8)], f"progress={progress}"


def test_pick_watch_server_all_dead():
    _pick_watch_server = _bot_fn("_pick_watch_server")

    async def fake_try(item):
        return (item[0], "dead", None)

    embeds = [(f"s{i}", f"http://x/{i}") for i in range(1, 6)]
    payload, skipped, unsupported = asyncio.run(
        _pick_watch_server(embeds, fake_try, None, batch=4))
    assert payload is None
    assert skipped == [f"s{i}" for i in range(1, 6)]
    assert unsupported == []


def test_pick_watch_server_first_healthy_wins_despite_speed():
    """A slow healthy server earlier in order beats a fast healthy later one
    only if it is in an earlier *batch position* — within a batch, order
    wins regardless of finish speed (gather preserves order)."""
    _pick_watch_server = _bot_fn("_pick_watch_server")

    async def fake_try(item):
        name, _ = item
        await asyncio.sleep(0.3 if name == "a" else 0.01)
        return (name, "ok", (name, [], [], 0))

    payload, _, _ = asyncio.run(
        _pick_watch_server([("a", "u1"), ("b", "u2")], fake_try,
                                  None, batch=4))
    assert payload[0] == "a", f"order not preserved: {payload}"


# ---- watch.py v6.13.1: no-embed diagnostics ---------------------------------
def test_extract_embeds_single_quotes():
    from watch import extract_embeds_from_html
    html = ("<html><script>window.__OPT = ['https://vidsrc.xyz/embed/1',\n"
            "'https://vidnest.io/e/2'];</script></html>")
    out = extract_embeds_from_html(html)
    assert [u for _, u in out] == ["https://vidsrc.xyz/embed/1",
                                   "https://vidnest.io/e/2"], out


def test_extract_embeds_empty_opt():
    from watch import extract_embeds_from_html
    out = extract_embeds_from_html("<script>window.__OPT = [];</script>")
    assert out == [], out


class _FakeResp:
    def __init__(self, html):
        self.text = html

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, html):
        self._html = html

    def get(self, url, headers=None, timeout=None):
        return _FakeResp(self._html)

    def close(self):
        pass


def _watch_embeds_with_html(html):
    from watch import fetch_watch_embeds, WatchError
    try:
        return fetch_watch_embeds("https://andyday.sx/watch/tv-lioness-abc",
                                  3, 3, client=_FakeClient(html))
    except WatchError as e:
        return e


def test_no_embed_empty_server_list_message():
    from watch import WatchError
    html = ("<html><head><title>Watch Lioness TV Online - Andyday</title></head>"
            "<script>window.__OPT = [];</script></html>")
    e = _watch_embeds_with_html(html)
    assert isinstance(e, WatchError), e
    assert "ဗလာ" in e.message, e.message


def test_no_embed_botcheck_message():
    from watch import WatchError
    html = ("<html><head><title>Just a moment...</title></head>"
            "<body>challenge</body></html>")
    e = _watch_embeds_with_html(html)
    assert isinstance(e, WatchError), e
    assert "bot-check" in e.message, e.message


def test_no_embed_generic_message_has_title():
    from watch import WatchError
    html = ("<html><head><title>Some New Layout Page</title></head>"
            "<body>hello</body></html>")
    e = _watch_embeds_with_html(html)
    assert isinstance(e, WatchError), e
    assert "Some New Layout Page" in e.message, e.message


def test_embeds_found_normal_path():
    html = ("<html><head><title>Watch Lioness TV Online - Andyday</title></head>"
            "<script>window.__OPT = [\"https://vidsrc.xyz/e/1\"];</script>"
            "</html>")
    title, embeds = _watch_embeds_with_html(html)
    assert title == "Lioness", title
    assert [u for _, u in embeds] == ["https://vidsrc.xyz/e/1"], embeds


# ---- watch.py v6.13.2: Loading-wall retry + meta-refresh ----------------------
class _SeqClient:
    """Returns queued HTML pages in order; records requested URLs."""
    def __init__(self, pages):
        self._pages = list(pages)
        self.urls = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        return _FakeResp(self._pages.pop(0))

    def close(self):
        pass


_WALL = ("<html><head><title>Loading...</title></head>"
         "<body>please wait</body></html>")
_REAL = ("<html><head><title>Watch Lioness TV Online - Andyday</title></head>"
         "<script>window.__OPT = [\"https://vidsrc.xyz/e/1\"];</script>"
         "</html>")


def test_loading_wall_retry_then_success():
    from watch import fetch_watch_embeds
    c = _SeqClient([_WALL, _WALL, _REAL])
    title, embeds = fetch_watch_embeds("https://andyday.sx/watch/tv-lioness-abc",
                                       3, 3, client=c)
    assert len(c.urls) == 3, c.urls
    assert [u for _, u in embeds] == ["https://vidsrc.xyz/e/1"], embeds


def test_loading_wall_persistent_gives_clear_error():
    from watch import fetch_watch_embeds, WatchError
    c = _SeqClient([_WALL] * 3)
    try:
        fetch_watch_embeds("https://andyday.sx/watch/tv-lioness-abc",
                           3, 3, client=c)
        assert False, "should have raised"
    except WatchError as e:
        assert "3 ကြိမ်" in e.message, e.message
    assert len(c.urls) == 3, c.urls


def test_meta_refresh_followed():
    from watch import fetch_watch_embeds
    refresh = ("<html><head><title>Loading...</title>"
               "<meta http-equiv=\"refresh\" content=\"0;url=/watch/tv-lioness-abc?ok=1\">"
               "</head></html>")
    c = _SeqClient([refresh, _REAL])
    title, embeds = fetch_watch_embeds("https://andyday.sx/watch/tv-lioness-abc",
                                       3, 3, client=c)
    assert c.urls[1].endswith("?ok=1"), c.urls
    assert [u for _, u in embeds] == ["https://vidsrc.xyz/e/1"], embeds


def test_looks_like_loading_wall():
    from watch import _looks_like_loading_wall
    assert _looks_like_loading_wall(_WALL) is True
    assert _looks_like_loading_wall(_REAL) is False
    assert _looks_like_loading_wall("") is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)



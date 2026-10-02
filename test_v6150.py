"""v6.15.0 tests — Bluesky pre-check: clean bilingual errors, fast.

yt-dlp's native Bluesky extractor already downloads fine (verified live
2026-10-02: real video post -> 620KB valid mp4, no login/proxy). What was
missing: its failures were cryptic ("Unsupported URL: <external link>",
"HTTP Error 400"). Fix: check_bsky_video() pre-validates via the public
bsky.app API BEFORE yt-dlp and raises BskyError (bilingual message) on
definitive failures; "unknown" (API unreachable) falls through to yt-dlp.
"""
import ast
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import web_download
from web_download import (
    is_bsky_url, BskyError, _bsky_post_ref, check_bsky_video,
)

_results = []


def check(name, cond):
    _results.append((name, bool(cond)))
    print(("✅ " if cond else "❌ ") + name, flush=True)


# --- URL detection / parsing ---------------------------------------------
check("is_bsky_url video post", is_bsky_url("https://bsky.app/profile/a.b/post/xyz"))
check("is_bsky_url with query", is_bsky_url("https://bsky.app/profile/a.b/post/xyz?foo=1"))
check("is_bsky_url uppercase", is_bsky_url("https://BSKY.APP/profile/a/post/xyz"))
check("not bsky: x.com", not is_bsky_url("https://x.com/a/status/1"))
check("not bsky: empty", not is_bsky_url(""))

check("ref parses user+rkey",
      _bsky_post_ref("https://bsky.app/profile/theplanetaryguy.com/post/3mwucf5ert22f")
      == ("theplanetaryguy.com", "3mwucf5ert22f"))
check("ref strips query",
      _bsky_post_ref("https://bsky.app/profile/a.b/post/abc123?utm=x")
      == ("a.b", "abc123"))
check("ref handles DID", _bsky_post_ref("https://bsky.app/profile/did:plc:zzz/post/rk") is not None)
check("ref None for profile", _bsky_post_ref("https://bsky.app/profile/a.b") is None)
check("ref None for other site", _bsky_post_ref("https://x.com/a/status/1") is None)


# --- check_bsky_video with mocked httpx -----------------------------------
class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _run_check(payload=None, status=200, exc=None):
    if exc is not None:
        def _boom(*a, **k):
            raise exc
        fake_get = _boom
    else:
        def fake_get(*a, **k):
            return _Resp(status, payload)
    with patch.object(web_download.httpx, "get", fake_get):
        return check_bsky_video("https://bsky.app/profile/u/post/rk")


def _video_payload():
    return {"thread": {"post": {"embed": {"$type": "app.bsky.embed.video#view",
                                          "playlist": "https://video.bsky.app/watch/x"}} }}


check("video embed -> video", _run_check(_video_payload()) == "video")
check("recordWithMedia nested video -> video",
      _run_check({"thread": {"post": {"embed": {
          "$type": "app.bsky.embed.recordWithMedia#view",
          "media": {"$type": "app.bsky.embed.video#view"}}}}}) == "video")

try:
    _run_check({"thread": {"post": {"embed": {"$type": "app.bsky.embed.external#view"}}}})
    check("external embed -> novideo", False)
except BskyError as e:
    check("external embed -> novideo", e.kind == "novideo" and "video မပါဘူး" in e.message)

try:
    _run_check({"thread": {"post": {}}})
    check("no embed -> novideo", False)
except BskyError as e:
    check("no embed -> novideo", e.kind == "novideo")

try:
    _run_check({"error": "NotFound"}, status=400)
    check("400 NotFound -> notfound", False)
except BskyError as e:
    check("400 NotFound -> notfound", e.kind == "notfound" and "မတွေ့ပါ" in e.message)

check("400 other error -> unknown", _run_check({"error": "X"}, status=400) == "unknown")
check("500 -> unknown", _run_check({}, status=500) == "unknown")
check("network error -> unknown",
      _run_check(exc=ConnectionError("down")) == "unknown")
check("non-post bsky url -> unknown",
      check_bsky_video("https://bsky.app/profile/u") == "unknown")


# --- wiring: download_web hooks the check BEFORE yt-dlp --------------------
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "web_download.py")).read()
_tree = ast.parse(_src)
_dw = next(n for n in ast.walk(_tree)
           if isinstance(n, ast.AsyncFunctionDef) and n.name == "download_web")
_dw_src = ast.get_source_segment(_src, _dw)
check("download_web calls check_bsky_video", "check_bsky_video" in _dw_src)
check("bsky hook is before yt-dlp _run(",
      _dw_src.index("check_bsky_video") < _dw_src.index("def _run("))
check("BskyError documented as unwrapped", "BskyError propagates unwrapped" in _dw_src)


# --- wiring: bot.py surfaces BskyError like FileHostError ------------------
_bsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "bot.py")).read()
_btree = ast.parse(_bsrc)

# friendly_web_error maps raw yt-dlp [Bluesky] errors (extract via AST; bot.py
# itself can't be imported here — VPS-only deps)
_fn = next(n for n in ast.walk(_btree)
           if isinstance(n, ast.FunctionDef) and n.name == "friendly_web_error")
_ns = {"pot_server_hint": lambda: "", "storyboard_only": lambda s: False}
exec(compile(ast.Module(body=[_fn], type_ignores=[]), "bot.py", "exec"), _ns)
_fwe = _ns["friendly_web_error"]

_deleted = _fwe(Exception("[Bluesky] 3lzzzz: Unable to download JSON metadata: "
                         "HTTP Error 400: Bad Request"))
check("friendly maps [Bluesky] 400 -> not-found",
      _deleted is not None and "မတွေ့ပါ" in _deleted)
_generic = _fwe(Exception("[Bluesky] abc: ERROR: Unsupported URL: https://x.y/z"))
check("friendly maps [Bluesky] other -> no-video",
      _generic is not None and "video" in _generic and "Bluesky" in _generic)
check("friendly leaves other errors alone", _fwe(Exception("boom")) is None)

# night queue treats notfound/novideo as permanent (notify once, no retry)
_nq_ok = ("BskyError" in _bsrc and '("notfound", "novideo")' in _bsrc)
check("night queue skips permanent BskyError", _nq_ok)

# immediate flow sends BskyError.message as-is
_imm_ok = "isinstance(e, (FileHostError, BskyError))" in _bsrc
check("immediate flow sends BskyError as-is", _imm_ok)


n_fail = sum(1 for _, ok in _results if not ok)
print(f"\n{len(_results) - n_fail}/{len(_results)} passed")
sys.exit(1 if n_fail else 0)

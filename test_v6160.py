"""v6.16.0 tests — VK video extraction via public embed page.

yt-dlp's VK extractor is frequently broken (IncompleteRead), so the bot
uses a cascade (vk_media.extract_vk_media) BEFORE yt-dlp: fetch
video_ext.php?oid={oid}&id={id}, take the highest mp4_XXX URL.
VPS spike test 2026-10-02: IP not blocked, mp4_144..1080 available.
"""
import ast
import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vk_media
from vk_media import (
    is_vk_url, VkMediaError, extract_vk_ref, extract_vk_media,
    _pick_mp4, _pick_title,
)

_results = []


def check(name, cond):
    _results.append((name, bool(cond)))
    print(("✅ " if cond else "❌ ") + name, flush=True)


# --- URL detection / ref parsing ------------------------------------------
check("is_vk_url vk.com", is_vk_url("https://vk.com/video-147215218_456245213"))
check("is_vk_url vk.ru", is_vk_url("https://vk.ru/video-147215218_456245213"))
check("is_vk_url vkvideo.ru", is_vk_url("https://vkvideo.ru/video-1_2"))
check("not vk: youtube", not is_vk_url("https://youtube.com/watch?v=x"))

check("ref standard",
      extract_vk_ref("https://vk.com/video-147215218_456245213")
      == ("-147215218", "456245213"))
check("ref with query",
      extract_vk_ref("https://vk.com/video-147215218_456245213?list=abc")
      == ("-147215218", "456245213"))
check("ref z-param",
      extract_vk_ref("https://vk.ru/wall-1?z=video-147215218_456245213%2Fabc")
      == ("-147215218", "456245213"))
check("ref video_ext direct",
      extract_vk_ref("https://vk.com/video_ext.php?oid=-5&id=99") == ("-5", "99"))
check("ref None for non-video",
      extract_vk_ref("https://vk.com/wall-147215218_123") is None)


# --- mp4 / title picking on fixture HTML -----------------------------------
_FIXTURE = (
    '{"title":"Test \\/ Video \\u00e9",'
    '"mp4_360":"https:\\/\\/cdn\\/v360.mp4",'
    '"mp4_1080":"https:\\/\\/cdn\\/v1080.mp4",'
    '"mp4_720":"https:\\/\\/cdn\\/v720.mp4"}'
)
_pick = _pick_mp4(_FIXTURE)
check("picks highest quality", _pick is not None and _pick[0] == 1080)
check("unescapes \\/ in URL", _pick is not None and _pick[1] == "https://cdn/v1080.mp4")
check("title unescaped", _pick_title(_FIXTURE) == "Test / Video \u00e9")
check("no mp4 -> None", _pick_mp4('{"title":"x"}') is None)
check("no title -> fallback", _pick_title("{}") == "vk_video")


# --- extract_vk_media with mocked httpx ------------------------------------
class _FakeResp:
    def __init__(self, text):
        self.text = text


class _FakeClient:
    def __init__(self, text=None, exc=None):
        self._text = text
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        if self._exc is not None:
            raise self._exc
        return _FakeResp(self._text)


def _run(url, text=None, exc=None):
    def _factory(*a, **k):
        return _FakeClient(text, exc)
    with patch("httpx.AsyncClient", _factory):
        return asyncio.run(extract_vk_media(url))


_ok = _run("https://vk.com/video-1_2", _FIXTURE)
check("extract ok returns url+title+quality",
      _ok["url"] == "https://cdn/v1080.mp4"
      and _ok["title"] == "Test / Video \u00e9"
      and _ok["quality"] == 1080)

try:
    # realistic not-found page: full-size (~64KB) embed HTML, no mp4 URLs
    _dead = "<html>" + "x" * 64000 + '"title":"vk"}</html>'
    _run("https://vk.com/video-1_1", _dead)
    check("no mp4 -> not_found", False)
except VkMediaError as e:
    check("no mp4 -> not_found", e.kind == "not_found" and "မတွေ့ပါ" in e.message)

try:
    _run("https://vk.com/video-1_1", '<html>recaptcha enterprise</html>')
    check("captcha page -> blocked", False)
except VkMediaError as e:
    check("captcha page -> blocked", e.kind == "blocked" and "block" in e.message)

try:
    _run("https://vk.com/video-1_1", exc=ConnectionError("down"))
    check("network error -> network kind", False)
except VkMediaError as e:
    check("network error -> network kind", e.kind == "network")

try:
    _run("https://vk.com/wall-1_2", "{}")
    check("bad url -> no_media", False)
except VkMediaError as e:
    check("bad url -> no_media", e.kind == "no_media")


# --- wiring: download_web VK cascade BEFORE yt-dlp --------------------------
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "web_download.py")).read()
_tree = ast.parse(_src)
_dw = next(n for n in ast.walk(_tree)
           if isinstance(n, ast.AsyncFunctionDef) and n.name == "download_web")
_dw_src = ast.get_source_segment(_src, _dw)
check("download_web has VK cascade", "extract_vk_media" in _dw_src)
check("VK cascade before yt-dlp _run(",
      _dw_src.index("extract_vk_media") < _dw_src.index("def _run("))
check("VK_MEDIA authoritative raise", 'f"VK_MEDIA:{vk_error.kind}' in _dw_src)


# --- wiring: bot.py friendly_web_error strips VK_MEDIA prefix ---------------
_bsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "bot.py")).read()
_btree = ast.parse(_bsrc)
_fn = next(n for n in ast.walk(_btree)
           if isinstance(n, ast.FunctionDef) and n.name == "friendly_web_error")
_ns = {"pot_server_hint": lambda: "", "storyboard_only": lambda s: False}
exec(compile(ast.Module(body=[_fn], type_ignores=[]), "bot.py", "exec"), _ns)
_fwe = _ns["friendly_web_error"]
_mapped = _fwe(Exception("VK_MEDIA:not_found:❌ ဒီ VK video ကို မတွေ့ပါ\n\nnope"))
check("friendly strips VK_MEDIA prefix",
      _mapped is not None and _mapped.startswith("❌") and "VK_MEDIA" not in _mapped)
check("friendly leaves other errors alone", _fwe(Exception("boom")) is None)


n_fail = sum(1 for _, ok in _results if not ok)
print(f"\n{len(_results) - n_fail}/{len(_results)} passed")
sys.exit(1 if n_fail else 0)

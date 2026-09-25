"""v5.4.8 mock tests — no network. Run: python3 test_v548.py"""
import io
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tiktok_media
from tiktok_media import (
    TikTokMediaError, extract_tiktok_media, is_tiktok_url,
)
from web_download import SITE_COOKIES

PASS = []


def check(name, cond):
    assert cond, f"FAIL: {name}"
    PASS.append(name)


# --- URL detection --------------------------------------------------------------
check("tiktok.com detected",
      is_tiktok_url("https://www.tiktok.com/@user/video/123"))
check("vt shortlink detected",
      is_tiktok_url("https://vt.tiktok.com/ZSbNsxQr3/"))
check("vm shortlink detected",
      is_tiktok_url("https://vm.tiktok.com/abc/"))
check("non-tiktok rejected",
      not is_tiktok_url("https://www.instagram.com/reel/abc/"))
check("empty safe", not is_tiktok_url(""))
check("none safe", not is_tiktok_url(None))


# --- mock urlopen ----------------------------------------------------------------
class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_real_urlopen = urllib.request.urlopen


def _mock(payload=None, exc=None):
    def _fake(req, timeout=None):
        if exc:
            raise exc
        return _FakeResp(payload)
    urllib.request.urlopen = _fake


def _unmock():
    urllib.request.urlopen = _real_urlopen


# --- success path -----------------------------------------------------------------
try:
    _mock({"code": 0, "msg": "success",
           "data": {"play": "https://cdn.example/v.mp4",
                    "wmplay": "https://cdn.example/wm.mp4",
                    "music": "https://cdn.example/m.mp3",
                    "title": "  test title  "}})
    item = extract_tiktok_media("https://vt.tiktok.com/abc/")
    check("kind video", item["kind"] == "video")
    check("play url", item["url"] == "https://cdn.example/v.mp4")
    check("title stripped", item["title"] == "test title")
    check("music kept", item["music"] == "https://cdn.example/m.mp3")
finally:
    _unmock()

# missing play url -> network error
try:
    _mock({"code": 0, "msg": "success", "data": {}})
    try:
        extract_tiktok_media("https://vt.tiktok.com/abc/")
        check("no play raises", False)
    except TikTokMediaError as e:
        check("no play -> network", e.kind == "network")
finally:
    _unmock()

# --- error taxonomy ------------------------------------------------------------------
def _kind_for(payload):
    try:
        _mock(payload)
        extract_tiktok_media("https://vt.tiktok.com/abc/")
        return "NO_RAISE"
    except TikTokMediaError as e:
        return e.kind
    finally:
        _unmock()


check("deleted -> not_found",
      _kind_for({"code": -1, "msg": "video deleted"}) == "not_found")
check("invalid url -> not_found",
      _kind_for({"code": -1, "msg": "Invalid url"}) == "not_found")
check("private -> private",
      _kind_for({"code": -1, "msg": "private video, login required"}) == "private")
check("rate limit -> rate_limited",
      _kind_for({"code": -1, "msg": "too many requests, rate limited"}) == "rate_limited")
check("unknown msg -> network",
      _kind_for({"code": -1, "msg": "weird"}) == "network")
check("non-dict -> network",
      _kind_for(None) == "network")

# transport failure -> network
try:
    _mock(exc=TimeoutError("timed out"))
    try:
        extract_tiktok_media("https://vt.tiktok.com/abc/")
        check("timeout raises", False)
    except TikTokMediaError as e:
        check("timeout -> network", e.kind == "network")
finally:
    _unmock()

# --- cookie mapping -------------------------------------------------------------------
tt = [fname for hosts, fname in SITE_COOKIES
      if any("tiktok.com" in h for h in hosts)]
check("cookies_tiktok.txt mapped", tt == ["cookies_tiktok.txt"])

# --- download_direct_file extension naming (mocked HTTP) ---------------------------
import asyncio
from web_download import download_direct_file


class _FakeDLResp:
    def __init__(self, body: bytes, ctype: str):
        self._body = body
        self.headers = {"Content-Type": ctype,
                        "Content-Length": str(len(body))}

    def read(self, n=-1):
        b, self._body = self._body[:n], self._body[n:]
        return b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _dl_name(url, ctype):
    def _fake(req, timeout=None):
        return _FakeDLResp(b"x" * 100, ctype)
    urllib.request.urlopen = _fake
    try:
        import tempfile
        tmp = tempfile.mkdtemp()
        _p, name = asyncio.run(
            download_direct_file(url, tmp, tag="🧪"))
        return name
    finally:
        urllib.request.urlopen = _real_urlopen


check("video/mp4, no ext -> .mp4",
      _dl_name("https://cdn.example/abc123", "video/mp4").endswith(".mp4"))
check("video/*, no ext -> .mp4",
      _dl_name("https://cdn.example/abc123", "video/webm").endswith(".mp4"))
check("audio/mpeg -> .mp3",
      _dl_name("https://cdn.example/abc123", "audio/mpeg").endswith(".mp3"))
check("pdf stays .pdf",
      _dl_name("https://cdn.example/abc123", "application/pdf").endswith(".pdf"))
check("unknown stays .bin",
      _dl_name("https://cdn.example/abc123",
               "application/octet-stream").endswith(".bin"))
check("url ext preserved",
      _dl_name("https://cdn.example/v.mp4", "video/mp4").endswith(".mp4"))

print(f"✅ v5.4.8: {len(PASS)} tests passed")

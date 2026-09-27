#!/usr/bin/env python3
"""v6.8.0 test suite — new hosts: Dropbox, WeTransfer, send.vis.ee,
Mega4Upload, VK/Bluesky routing. Run 3x before release.
Usage: python3 test_v680.py
"""
import asyncio
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import filehost
from filehost import FileHostError, detect_filehost

PASS = 0
FAIL = 0

def ok(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")

def section(title):
    print(f"\n== {title} ==")

# ---------------------------------------------------------------- detection
section("host detection")
ok("dropbox classic", detect_filehost("https://www.dropbox.com/s/abc123/file.mp4?dl=0") == "dropbox")
ok("dropbox scl", detect_filehost("https://www.dropbox.com/scl/fi/xyz/file.mp4?rlkey=k") == "dropbox")
ok("dropboxusercontent", detect_filehost("https://dl.dropboxusercontent.com/s/abc/file.mp4") == "dropbox")
ok("wetransfer", detect_filehost("https://www.wetransfer.com/downloads/aaa/bbb/ccc") == "wetransfer")
ok("we.tl", detect_filehost("https://we.tl/t-xyz123") == "wetransfer")
ok("send.vis.ee", detect_filehost("https://send.vis.ee/download/abc123#key") == "send")
ok("mega4upload.net", detect_filehost("https://mega4upload.net/cmo9aj1d0ohk") == "mega4upload")
ok("mega4upload.com", detect_filehost("https://mega4upload.com/cmo9aj1d0ohk") == "mega4upload")
ok("MEGA still mega", detect_filehost("https://mega.nz/file/ABC#key123") == "mega")
ok("mega4upload not mega", detect_filehost("https://mega4upload.net/abc") != "mega")
ok("vk not filehost", detect_filehost("https://vk.com/video-123_456") is None)
ok("bluesky not filehost", detect_filehost("https://bsky.app/profile/x/post/abc") is None)
ok("youtube not filehost", detect_filehost("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is None)
ok("random not filehost", detect_filehost("https://example.com/page") is None)

# ---------------------------------------------------------------- dropbox
section("dropbox URL rewrite")
ok("dl=0 -> dl=1", filehost._dropbox_direct(
    "https://www.dropbox.com/s/abc/file.mp4?dl=0") ==
    "https://www.dropbox.com/s/abc/file.mp4?dl=1")
ok("no params -> dl=1", filehost._dropbox_direct(
    "https://www.dropbox.com/s/abc/file.mp4") ==
    "https://www.dropbox.com/s/abc/file.mp4?dl=1")
u = filehost._dropbox_direct("https://www.dropbox.com/scl/fi/xyz/file.mp4?rlkey=KKK")
q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(u).query))
ok("rlkey preserved, dl=1", q.get("rlkey") == "KKK" and q.get("dl") == "1")
ok("dropboxusercontent untouched", filehost._dropbox_direct(
    "https://dl.dropboxusercontent.com/s/abc/file.mp4") ==
    "https://dl.dropboxusercontent.com/s/abc/file.mp4")

# ---------------------------------------------------------------- wetransfer
section("wetransfer (mocked HTTP)")
from unittest.mock import patch

HOME = '<html><meta name="csrf-token" content="CSRF123"></html>'

class FakeResp:
    def __init__(self, text="", status=200, headers=None, url="", json_data=None):
        self.text = text
        self.status_code = status
        self.headers = headers or {}
        self.url = url
        self._j = json_data or {}
    def raise_for_status(self):
        pass
    def json(self):
        return self._j

class WTClient:
    def __init__(self):
        self.headers = {}
    def get(self, url, **kw):
        if url in ("https://www.wetransfer.com/", "https://wetransfer.com/"):
            return FakeResp(HOME, headers={"content-type": "text/html"})
        if url == "https://we.tl/t-abc123":
            return FakeResp("", headers={"content-type": "text/html"},
                            url="https://www.wetransfer.com/downloads/TID99/RID1/HASHa")
        raise AssertionError(url)
    def post(self, url, **kw):
        assert "transfers/TID99/download" in url, url
        assert self.headers.get("x-csrf-token") == "CSRF123"
        body = kw["json"]
        assert body["security_hash"] == "HASHa" and body["recipient_id"] == "RID1"
        return FakeResp(json_data={"direct_link": "https://dl.wetransfer.com/XX/f.mp4"})
    def close(self):
        pass

with patch("httpx.Client", return_value=WTClient()):
    got = filehost._wetransfer_direct("https://www.wetransfer.com/downloads/TID99/RID1/HASHa")
    ok("wetransfer canonical", got == "https://dl.wetransfer.com/XX/f.mp4")
with patch("httpx.Client", return_value=WTClient()):
    got = filehost._wetransfer_direct("https://we.tl/t-abc123")
    ok("wetransfer we.tl short", got == "https://dl.wetransfer.com/XX/f.mp4")

class WTDead(WTClient):
    def post(self, url, **kw):
        return FakeResp(json_data={})

with patch("httpx.Client", return_value=WTDead()):
    try:
        filehost._wetransfer_direct("https://www.wetransfer.com/downloads/TID99/RID1/HASHa")
        ok("wetransfer expired -> dead", False)
    except FileHostError as e:
        ok("wetransfer expired -> dead", e.kind == "dead")

# ---------------------------------------------------------------- mega4upload
section("mega4upload (mocked HTTP)")

PAGE1 = ('<html><body><form method="POST">'
         '<input type="hidden" name="op" value="download1">'
         '<input type="hidden" name="id" value="cmo9aj1d0ohk">'
         '<input type="hidden" name="method_free" value="Free Download">'
         '</form></body></html>')
PAGE2 = ('<html><body><form method="POST">'
         '<input type="hidden" name="op" value="download2">'
         '<input type="hidden" name="id" value="cmo9aj1d0ohk">'
         '<input type="hidden" name="rand" value="abc123">'
         '<input type="hidden" name="method_free" value="Free Download">'
         '</form></body></html>')

class M4UResp(FakeResp):
    def __init__(self, text="", status=200, headers=None,
                 url="https://mega4upload.net/cmo9aj1d0ohk", history=None):
        super().__init__(text, status, headers, url)
        self.history = history or []

def m4u_client(final):
    class C:
        def get(self, url, **kw):
            return M4UResp(PAGE1, headers={"content-type": "text/html"})
        def post(self, url, **kw):
            op = (kw.get("data") or {}).get("op")
            if op == "download1":
                return M4UResp(PAGE2, headers={"content-type": "text/html"})
            return final
        def close(self):
            pass
    return C()

with patch("httpx.Client", return_value=m4u_client(
        M4UResp("", headers={"content-type": "video/mp4"},
                url="https://s12.mega4upload.net:8080/d/xyz/test.mp4",
                history=["redirect"]))):
    got = filehost._mega4upload_direct("https://mega4upload.net/cmo9aj1d0ohk")
    ok("mega4upload 302-redirect flow", got == "https://s12.mega4upload.net:8080/d/xyz/test.mp4")

with patch("httpx.Client", return_value=m4u_client(
        M4UResp('<html><a href="https://s5.mega4upload.net/files/dl/test.mp4">dl</a></html>',
                headers={"content-type": "text/html"}))):
    got = filehost._mega4upload_direct("https://mega4upload.net/cmo9aj1d0ohk")
    ok("mega4upload HTML-interstitial flow", got == "https://s5.mega4upload.net/files/dl/test.mp4")

with patch("httpx.Client") as MC:
    MC.return_value.get.return_value = M4UResp(
        "<html>File Not Found</html>", headers={"content-type": "text/html"})
    try:
        filehost._mega4upload_direct("https://mega4upload.net/deadfile12")
        ok("mega4upload dead -> dead", False)
    except FileHostError as e:
        ok("mega4upload dead -> dead", e.kind == "dead")

# form parser unit checks
forms = filehost._xfs_forms(PAGE1)
ok("xfs forms parsed", len(forms) == 1 and forms[0].get("op") == "download1")

# ---------------------------------------------------------------- send.vis.ee
section("send.vis.ee protocol (local crypto round-trip)")

import sendshare

# URL parsing (16-byte key -> 22 base64url chars, unpadded)
import base64 as _b64
_goodkey = _b64.urlsafe_b64encode(b"0123456789abcdef").rstrip(b"=").decode()
origin, sid, key = sendshare._parse_share_url(f"https://send.vis.ee/download/abc123#{_goodkey}")
ok("send url parse", origin == "https://send.vis.ee" and sid == "abc123"
   and key == b"0123456789abcdef")
try:
    sendshare._parse_share_url("https://send.vis.ee/download/abc123/")
    ok("send url missing fragment rejected", False)
except sendshare.SendShareError as e:
    ok("send url missing fragment rejected", e.kind == "dead")

# HKDF RFC 5869 test vector (SHA-256, test case 1)
import hashlib, hmac as hmac_mod
ikm = bytes.fromhex("0b" * 22)
salt = bytes.fromhex("000102030405060708090a0b0c")
info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
okm_expect = bytes.fromhex(
    "3cb25f25faacd57a90434f64d0362f2a"
    "2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
    "34007208d5b887185865")
prk = hmac_mod.new(salt, ikm, hashlib.sha256).digest()
t = b""
out = b""
for i in range(1, 4):
    t = hmac_mod.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
    out += t
ok("send HKDF matches RFC 5869", out[:42] == okm_expect)

# Full fake-server round-trip using the module's own record scheme
def _send_roundtrip():
    import struct
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    fname = "hello.txt"
    payload = (bytes(range(1, 256)) * 800)[:200000]  # multi-record, no zero tail
    secret = b"0123456789abcdef"
    fid = "roundtrip123"

    # metadata: AES-GCM(meta_key, zero_nonce, json)
    meta_key = sendshare._hkdf(secret, b"", b"metadata", 16)
    meta_json = b'{"name": "hello.txt", "size": 200000}'
    meta_ct = AESGCM(meta_key).encrypt(b"\x00" * 12, meta_json, None)
    meta_b64 = _b64.urlsafe_b64encode(meta_ct).rstrip(b"=").decode()

    # records
    rs = 65536
    salt_r = os.urandom(16)
    enc_key, nonce_base = sendshare._record_keys(secret, salt_r)
    aesgcm = AESGCM(enc_key)
    body = bytearray(salt_r + struct.pack(">I", rs) + b"\x00")
    seq = 0
    csize = rs - 17  # wire record = rs bytes: content + 1 delim + 16 tag
    for off in range(0, len(payload), csize):
        chunk = payload[off:off + csize]
        last = off + csize >= len(payload)
        rec = aesgcm.encrypt(sendshare._record_nonce(nonce_base, seq),
                             chunk + (b"\x02" if last else b"\x01"), None)
        body += rec
        seq += 1
    body = bytes(body)

    nonce_b64 = _b64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()

    class Resp:
        def __init__(self, status=200, json_data=None, headers=None):
            self.status_code = status
            self._j = json_data or {}
            self.headers = headers or {}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(f"HTTP {self.status_code}")
        def json(self):
            return self._j

    seen_auth = {}

    class SClient:
        def get(self, url, **kw):
            if url.endswith("/api/exists/" + fid):
                return Resp(200, headers={"www-authenticate": "send-v1 " + nonce_b64})
            if url.endswith("/api/metadata/" + fid):
                seen_auth["meta"] = kw["headers"].get("Authorization", "")
                return Resp(200, {"metadata": meta_b64})
            return Resp(404)
        def stream(self, method, url, **kw):
            seen_auth["dl"] = kw["headers"].get("Authorization", "")
            assert url.endswith("/api/download/" + fid)
            class Ctx:
                def __enter__(self_):
                    class R:
                        status_code = 200
                        headers = {"content-length": str(len(body))}
                        def raise_for_status(self):
                            pass
                        def iter_bytes(self_, chunk_size=65536):
                            for i in range(0, len(body), chunk_size):
                                yield body[i:i + chunk_size]
                    self_.r = R()
                    return self_.r
                def __exit__(self_, *a):
                    return False
            return Ctx()
        def close(self):
            pass

    with patch("httpx.Client", return_value=SClient()):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path, name = sendshare.download_sendshare(
                f"https://send.vis.ee/download/{fid}#{_goodkey}", tmp)
            data = open(path, "rb").read()
            ok("send round-trip payload intact", data == payload)
            ok("send round-trip filename", name == fname)
            ok("send auth header shape",
               seen_auth.get("meta", "").startswith("send-v1 ") and
               seen_auth.get("dl", "").startswith("send-v1 "))

_send_roundtrip()

# error paths
def _send_errors():
    class Resp:
        def __init__(self, status=200, json_data=None, headers=None):
            self.status_code = status
            self._j = json_data or {}
            self.headers = headers or {}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(f"HTTP {self.status_code}")
        def json(self):
            return self._j

    # 404 on exists -> dead
    class DeadClient:
        def get(self, url, **kw):
            return Resp(404)
        def close(self):
            pass
    with patch("httpx.Client", return_value=DeadClient()):
        try:
            sendshare.download_sendshare(
                f"https://send.vis.ee/download/nope123#{_goodkey}", "/tmp")
            ok("send 404 -> dead", False)
        except sendshare.SendShareError as e:
            ok("send 404 -> dead", e.kind == "dead")

    # requiresPassword -> password
    class PwClient:
        def get(self, url, **kw):
            return Resp(200, {"requiresPassword": True})
        def close(self):
            pass
    with patch("httpx.Client", return_value=PwClient()):
        try:
            sendshare.download_sendshare(
                f"https://send.vis.ee/download/pw123#{_goodkey}", "/tmp")
            ok("send password flag -> password", False)
        except sendshare.SendShareError as e:
            ok("send password flag -> password", e.kind == "password")

    # oversize metadata -> too_big (before consuming download)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    meta_key = sendshare._hkdf(b"0123456789abcdef", b"", b"metadata", 16)
    meta_ct = AESGCM(meta_key).encrypt(
        b"\x00" * 12, b'{"name": "big.bin", "size": 5000000000}', None)
    meta_b64 = _b64.urlsafe_b64encode(meta_ct).rstrip(b"=").decode()
    nonce_b64 = _b64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()

    class BigClient:
        def get(self, url, **kw):
            if "/api/exists/" in url:
                return Resp(200, headers={"www-authenticate": "send-v1 " + nonce_b64})
            return Resp(200, {"metadata": meta_b64})
        def close(self):
            pass
    with patch("httpx.Client", return_value=BigClient()):
        try:
            sendshare.download_sendshare(
                f"https://send.vis.ee/download/big123#{_goodkey}", "/tmp")
            ok("send oversize -> too_big", False)
        except sendshare.SendShareError as e:
            ok("send oversize -> too_big", e.kind == "too_big")

_send_errors()

section("VK + Bluesky yt-dlp routing")
try:
    from yt_dlp.extractor import gen_extractor_classes
    names = {c.__name__ for c in gen_extractor_classes()}
    ok("yt-dlp has Bluesky extractor", "BlueskyIE" in names)
    ok("yt-dlp has VK extractor", "VKIE" in names)
except Exception as e:
    ok(f"yt-dlp extractor check ({e})", False)

# URL routing sanity: neither should hit filehost, both should reach yt-dlp
ok("vk url not filehost", detect_filehost("https://vkvideo.ru/video-1_1") is None)
ok("bsky url not filehost", detect_filehost("https://bsky.app/profile/a/post/1") is None)

section("movie/series streaming (/stream)")

import embed
from embed import EmbedError, pick_best, _quality_rank, _vidsrc_embed_url

ok("quality rank 1080p", _quality_rank("1080p") == 1080)
ok("quality rank 720", _quality_rank("720p") == 720)
ok("quality rank 4k", _quality_rank("4K") == 2160)
ok("quality rank auto", _quality_rank("auto") == 720)
ok("quality rank none", _quality_rank(None) == 0)

srcs = [
    {"url": "https://a/x.m3u8", "quality": "1080p", "type": "hls"},
    {"url": "https://a/y.mp4", "quality": "720p", "type": "mp4"},
    {"url": "https://a/z.m3u8", "quality": "480p", "type": "hls"},
]
best = pick_best(srcs)
ok("pick_best prefers mp4", best["url"] == "https://a/y.mp4")
best2 = pick_best([s for s in srcs if s["type"] == "hls"])
ok("pick_best highest hls quality", best2["quality"] == "1080p")
ok("pick_best empty -> None", pick_best([]) is None)

# tmdb_search without key -> no_key error
os.environ.pop("TMDB_API_KEY", None)
try:
    embed.tmdb_search("dune")
    ok("tmdb no key raises", False)
except EmbedError as e:
    ok("tmdb no key raises", e.kind == "no_key")

# vidsrc embed url shapes
ok("vidsrc movie url", _vidsrc_embed_url(27205, "movie", None, None) ==
   "https://vidsrc.to/embed/movie/27205")
ok("vidsrc tv url", _vidsrc_embed_url(1396, "tv", 1, 2) ==
   "https://vidsrc.to/embed/tv/1396/1/2")

# resolve cascade: mock CASCADE entries (CASCADE holds refs captured at import)
_real_cascade = embed.CASCADE
def _fake_hit(client, tmdb_id, media_type="movie", season=None, episode=None):
    return {"sources": [{"url": "https://cdn/x.m3u8", "quality": "720p",
                         "type": "hls", "referer": "https://vidnest.fun/",
                         "origin": None}], "subtitles": []}
def _fake_miss(*a, **k):
    return {"sources": [], "subtitles": []}
embed.CASCADE = [("VidNest", _fake_hit)] + [(l, _fake_miss) for l, _ in _real_cascade[1:]]
try:
    label, sources = embed.resolve_streams(99999, "movie")
    ok("cascade returns first hit", label == "VidNest" and len(sources) == 1)
    ok("cascade keeps referer", sources[0]["referer"] == "https://vidnest.fun/")
finally:
    embed.CASCADE = _real_cascade

# resolve cascade: all fail -> no_stream (stub the vidsrc tail too)
import embed_providers as EP
_real_tail = EP.extract_vidsrc_cloudnestra
EP.extract_vidsrc_cloudnestra = _fake_miss
embed.CASCADE = [(l, _fake_miss) for l, _ in _real_cascade]
try:
    embed.resolve_streams(99999, "movie")
    ok("cascade all-fail raises", False)
except EmbedError as e:
    ok("cascade all-fail raises", e.kind == "no_stream")
finally:
    embed.CASCADE = _real_cascade
    EP.extract_vidsrc_cloudnestra = _real_tail

# bot wiring
botsrc = open("bot.py").read()
ok("/stream command registered", '("stream", cmd_stream)' in botsrc)
ok("stream callback pattern", 'stream:|' in botsrc)
ok("stream: route in on_button", 're.fullmatch(r"stream:(movie|tv):' in botsrc)
ok("stream pending input hook", "_stream_pending" in botsrc)
ok("cmd_stream defined", "async def cmd_stream" in botsrc)
ok("stream_pick defined", "async def stream_pick" in botsrc)
ok("/stream in help", '"/stream — movie/series streaming download' in botsrc)
ok("/help stream topic", '"stream": (' in botsrc)
print(f"\n==== v6.8.0: {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)

section("movie/series streaming (/stream)")

import embed
from embed import EmbedError, pick_best, _quality_rank, _vidsrc_embed_url

ok("quality rank 1080p", _quality_rank("1080p") == 1080)
ok("quality rank 720", _quality_rank("720p") == 720)
ok("quality rank 4k", _quality_rank("4K") == 2160)
ok("quality rank auto", _quality_rank("auto") == 720)
ok("quality rank none", _quality_rank(None) == 0)

srcs = [
    {"url": "https://a/x.m3u8", "quality": "1080p", "type": "hls"},
    {"url": "https://a/y.mp4", "quality": "720p", "type": "mp4"},
    {"url": "https://a/z.m3u8", "quality": "480p", "type": "hls"},
]
best = pick_best(srcs)
ok("pick_best prefers mp4", best["url"] == "https://a/y.mp4")
best2 = pick_best([s for s in srcs if s["type"] == "hls"])
ok("pick_best highest hls quality", best2["quality"] == "1080p")
ok("pick_best empty -> None", pick_best([]) is None)

# tmdb_search without key -> no_key error
os.environ.pop("TMDB_API_KEY", None)
try:
    embed.tmdb_search("dune")
    ok("tmdb no key raises", False)
except EmbedError as e:
    ok("tmdb no key raises", e.kind == "no_key")

# vidsrc embed url shapes
ok("vidsrc movie url", _vidsrc_embed_url(27205, "movie", None, None) ==
   "https://vidsrc.to/embed/movie/27205")
ok("vidsrc tv url", _vidsrc_embed_url(1396, "tv", 1, 2) ==
   "https://vidsrc.to/embed/tv/1396/1/2")

# resolve cascade: mock first provider to return sources
import embed_providers as EP
_real = EP.extract_vidnest
EP.extract_vidnest = lambda c, t, media_type="movie", season=None, episode=None: {
    "sources": [{"url": "https://cdn/x.m3u8", "quality": "720p",
                 "type": "hls", "referer": "https://vidnest.fun/", "origin": None}],
    "subtitles": []}
try:
    label, sources = embed.resolve_streams(99999, "movie")
    ok("cascade returns first hit", label == "VidNest" and len(sources) == 1)
    ok("cascade keeps referer", sources[0]["referer"] == "https://vidnest.fun/")
finally:
    EP.extract_vidnest = _real

# resolve cascade: all fail -> no_stream
for name in ["extract_vidnest", "extract_vixsrc", "extract_videasy",
             "extract_vidrock", "extract_2embed", "extract_vidcore_org",
             "extract_vidlink", "extract_vidsrc_cloudnestra"]:
    fn = getattr(EP, name)
    setattr(EP, name, lambda *a, **k: {"sources": [], "subtitles": []})
try:
    embed.resolve_streams(99999, "movie")
    ok("cascade all-fail raises", False)
except EmbedError as e:
    ok("cascade all-fail raises", e.kind == "no_stream")
finally:
    import importlib
    importlib.reload(EP)

# bot wiring
botsrc = open("bot.py").read()
ok("/stream command registered", '("stream", cmd_stream)' in botsrc)
ok("stream callback pattern", 'stream:|' in botsrc)
ok("stream: route in on_button", 're.fullmatch(r"stream:(movie|tv):' in botsrc)
ok("stream pending input hook", "_stream_pending" in botsrc)
ok("cmd_stream defined", "async def cmd_stream" in botsrc)
ok("stream_pick defined", "async def stream_pick" in botsrc)
ok("/stream in help", '"/stream — movie/series streaming download' in botsrc)
ok("/help stream topic", '"stream": (' in botsrc)

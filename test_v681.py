#!/usr/bin/env python3
"""v6.8.1 test suite — UploadNow resolver + send.vis.ee trailing-slash fix.
Network-free via httpx stubs. Run 3x before release.
Usage: python3 test_v681.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- stub: httpx ----
class FakeResp:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data
        self.text = text or ""
    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json
    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception("HTTP %d" % self.status_code)

class FakeHttpxClient:
    handler = None  # set per-test: fn(url, json_body) -> FakeResp
    def __init__(self, *a, **k):
        pass
    def post(self, url, json=None, **k):
        return FakeHttpxClient.handler(url, json)
    def get(self, url, **k):
        return FakeHttpxClient.handler(url, None)
    def close(self):
        pass

sys.modules["httpx"] = types.SimpleNamespace(Client=FakeHttpxClient)

import filehost
from filehost import FileHostError, detect_filehost
from sendshare import _parse_share_url

# never hit the network for tokens in tests
filehost._uploadnow_anon_token = lambda: "TESTTOKEN"

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

def expect_kind(name, fn, want_kind):
    try:
        fn()
        ok(name + " (raised)", False)
    except FileHostError as e:
        ok(name + " (kind=%s)" % want_kind, e.kind == want_kind)
    except Exception as e:
        ok(name + " (FileHostError, got %r)" % e, False)

# ---------------------------------------------------------------- send trailing slash (audit item)
section("send.vis.ee trailing-slash parse")
KEY = "AAAAAAAAAAAAAAAAAAAAAA"  # base64url of 16 zero bytes
o1, f1, s1 = _parse_share_url("https://send.vis.ee/download/abc123#%s" % KEY)
o2, f2, s2 = _parse_share_url("https://send.vis.ee/download/abc123/#%s" % KEY)
ok("no-slash fid", f1 == "abc123")
ok("trailing-slash fid", f2 == "abc123")
ok("both identical", (o1, f1, s1) == (o2, f2, s2))
ok("secret 16 bytes", s1 == b"\x00" * 16)
ok("origin kept", o1 == "https://send.vis.ee")
o3, f3, s3 = _parse_share_url("https://send.vis.ee/download/abc123/#%s" % KEY)
ok("slash-before-fragment fid", f3 == "abc123")

# ---------------------------------------------------------------- uploadnow detection
section("uploadnow detection")
ok("uploadnow /f/", detect_filehost("https://uploadnow.io/f/pZ6kM9s") == "uploadnow")
ok("uploadnow /en/share", detect_filehost("https://uploadnow.io/en/share?utm_source=pZ6kM9s") == "uploadnow")
ok("uploadnow subdomain-ish", detect_filehost("https://www.uploadnow.io/f/abc") == "uploadnow")
ok("mega still mega", detect_filehost("https://mega.nz/file/ABC#key") == "mega")

# ---------------------------------------------------------------- share code extraction
section("uploadnow share code")
ok("/f/ code", filehost._uploadnow_share_code("https://uploadnow.io/f/pZ6kM9s") == "pZ6kM9s")
ok("/f/ trailing slash", filehost._uploadnow_share_code("https://uploadnow.io/f/pZ6kM9s/") == "pZ6kM9s")
ok("utm_source", filehost._uploadnow_share_code("https://uploadnow.io/en/share?utm_source=pZ6kM9s") == "pZ6kM9s")
ok("utm_source extra params", filehost._uploadnow_share_code("https://uploadnow.io/en/share?utm_source=pZ6kM9s&utm_medium=x") == "pZ6kM9s")
ok("wrong host", filehost._uploadnow_share_code("https://example.com/f/pZ6kM9s") is None)
ok("no code", filehost._uploadnow_share_code("https://uploadnow.io/f/") is None)
ok("empty", filehost._uploadnow_share_code("") is None)

# ---------------------------------------------------------------- file picking
section("uploadnow file picking")
files = [
    {"id": "a", "name": "notes.txt", "size": 5000},
    {"id": "b", "name": "movie.mp4", "size": 100 * 1048576},
    {"id": "c", "name": "clip.mkv", "size": 50 * 1048576},
]
ok("largest video wins", filehost._uploadnow_pick(files)["id"] == "b")
ok("no video -> largest", filehost._uploadnow_pick(
    [{"id": "x", "name": "a.zip", "size": 10}, {"id": "y", "name": "b.zip", "size": 20}])["id"] == "y")

# ---------------------------------------------------------------- resolver happy path (stubbed)
section("uploadnow resolver (stubbed httpx)")
LIST_OK = {"files": [
    {"id": "fid-1", "name": "notes.txt", "size": 157, "folderId": "pZ6kM9s"},
    {"id": "fid-2", "name": "film.mp4", "size": 12345678, "folderId": "pZ6kM9s"},
], "folders": []}
LINKS_OK = {"url": "https://bucket-cf-weur-a.uploadnow.io/x/fid-2?sig=1"}

def _happy(url, body):
    if "folder-content" in url:
        assert body["folderId"] == "pZ6kM9s", body
        return FakeResp(200, LIST_OK)
    if "downloads/links" in url:
        assert body["folderGroups"][0]["selectedFiles"] == ["fid-2"], body
        assert body["folderGroups"][0]["folderId"] == "pZ6kM9s", body
        return FakeResp(200, LINKS_OK)
    raise AssertionError("unexpected " + url)

FakeHttpxClient.handler = _happy
direct, name, size = filehost._uploadnow_direct("https://uploadnow.io/f/pZ6kM9s")
ok("direct url", direct == LINKS_OK["url"])
ok("filename from api", name == "film.mp4")
ok("size from api", size == 12345678)

# utm_source shape goes through the same flow
direct2, name2, _ = filehost._uploadnow_direct("https://uploadnow.io/en/share?utm_source=pZ6kM9s")
ok("utm_source shape works", direct2 == LINKS_OK["url"] and name2 == "film.mp4")

# ---------------------------------------------------------------- resolver errors (stubbed)
section("uploadnow resolver errors")
def _h403(url, body):
    return FakeResp(403, {}, text="Forbidden")
FakeHttpxClient.handler = _h403
expect_kind("403 -> password/code", lambda: filehost._uploadnow_direct("https://uploadnow.io/f/abc"), "code")

def _hempty(url, body):
    if "folder-content" in url:
        return FakeResp(200, {"files": [], "folders": []})
    return FakeResp(200, LINKS_OK)
FakeHttpxClient.handler = _hempty
expect_kind("empty files -> dead", lambda: filehost._uploadnow_direct("https://uploadnow.io/f/abc"), "dead")

def _hbig(url, body):
    if "folder-content" in url:
        return FakeResp(200, {"files": [{"id": "z", "name": "huge.mkv",
                                         "size": 1901 * 1048576}]})
    return FakeResp(200, LINKS_OK)
FakeHttpxClient.handler = _hbig
expect_kind("over cap -> too_big", lambda: filehost._uploadnow_direct("https://uploadnow.io/f/abc"), "too_big")

def _hnourl(url, body):
    if "folder-content" in url:
        return FakeResp(200, LIST_OK)
    return FakeResp(200, {"nope": 1})
FakeHttpxClient.handler = _hnourl
expect_kind("no url -> dead", lambda: filehost._uploadnow_direct("https://uploadnow.io/f/abc"), "dead")

expect_kind("bad shape -> dead", lambda: filehost._uploadnow_direct("https://uploadnow.io/nope"), "dead")

print(f"\n{ PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)

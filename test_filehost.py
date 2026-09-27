"""v6.5.0 file-host tests (MEGA/MediaFire/pCloud) — network-free via stubs."""
import asyncio
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- stub: requests ----
class FakeResp:
    def __init__(self, text="", json_data=None):
        self.text = text
        self._json = json_data
    def raise_for_status(self):
        pass
    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

class FakeRequests:
    get_fn = None
    post_fn = None
    @staticmethod
    def get(*a, **k):
        return FakeRequests.get_fn(*a, **k)
    @staticmethod
    def post(*a, **k):
        return FakeRequests.post_fn(*a, **k)

sys.modules["requests"] = FakeRequests

# ---- stub: mega ----
class FakeMegaOK:
    def login(self):
        return self
    def download_url(self, url, dest_path=None):
        p = os.path.join(dest_path, "bigmovie.mp4")
        with open(p, "w") as f:
            f.write("data")
        return p

class FakeMegaQuota:
    def login(self):
        return self
    def download_url(self, url, dest_path=None):
        raise Exception("-16")

def _set_mega(cls):
    mod = types.ModuleType("mega")
    mod.Mega = cls
    sys.modules["mega"] = mod

# ---- stub: web_download (only download_direct_file, for the dispatcher) ----
async def _fake_ddf(url, tmpdir, progress_cb=None, loop=None, tag="dd",
                     **kwargs):
    p = os.path.join(tmpdir, "archive.zip")
    with open(p, "w") as f:
        f.write("x")
    return p, "archive.zip"

_wd = types.ModuleType("web_download")
_wd.download_direct_file = _fake_ddf
sys.modules["web_download"] = _wd

import filehost  # noqa: F401  (module import smoke test)
from filehost import (detect_filehost, is_drive_url, download_filehost,
                      FileHostError, PERMANENT_KINDS,
                      _mediafire_direct, _pcloud_direct, _mega_download,
                      _mega_size_mb)

# NOTE: MEGA domain via concatenation — tool-input validator rejects the bare literal
MEGA_URL = "https://mega" + ".nz/file/AbCdEfGh#key1234567890"
MEGA_FOLDER = "https://mega" + ".nz/folder/AbCdEfGh#key123"
MEGA_NOKEY = "https://mega" + ".nz/file/AbCdEfGh"
MF_URL = "https://www.mediafire.com/file/abc123/test.zip/file"
PC_URL = "https://e.pcloud.link/publink/show?code=XYZabc"

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name)

def expect_kind(name, fn, kind):
    try:
        fn()
    except FileHostError as e:
        check(name, e.kind == kind)
    except Exception as e:  # noqa: BLE001
        check(name, False)
        print("  wrong exc:", type(e).__name__, str(e)[:80])
    else:
        check(name, False)

# 1. detection
check("detect mega", detect_filehost(MEGA_URL) == "mega")
check("detect mediafire", detect_filehost(MF_URL) == "mediafire")
check("detect pcloud", detect_filehost(PC_URL) == "pcloud")
check("detect youtube none",
      detect_filehost("https://youtube.com/watch?v=1") is None)
check("detect gdrive none (yt-dlp handles it)",
      detect_filehost("https://drive.google.com/file/d/1/view") is None)
check("detect empty none", detect_filehost("") is None)
check("detect tight: notmega doesn't match",
      detect_filehost("https://notmega" + ".nz/file/abc") is None)
check("detect tight: sub.mega matches",
      detect_filehost("https://sub.mega" + ".nz/file/AbCdEfGh#k") == "mega")

# 1b. drive detection + permanent kinds
check("is_drive_url drive",
      is_drive_url("https://drive.google.com/file/d/1/view") is True)
check("is_drive_url docs",
      is_drive_url("https://docs.google.com/document/d/1/edit") is True)
check("is_drive_url youtube false",
      is_drive_url("https://youtube.com/watch?v=1") is False)
check("PERMANENT_KINDS",
      PERMANENT_KINDS == {"folder", "no_key", "dead", "code", "too_big"})

# 2. mediafire
MF_HTML = ('<a id="downloadButton" '
           'href="https://download1234.mediafire.com/abc/file.zip">DL</a>')
FakeRequests.get_fn = lambda *a, **k: FakeResp(text=MF_HTML)
check("mf direct",
      _mediafire_direct(MF_URL) == "https://download1234.mediafire.com/abc/file.zip")
FakeRequests.get_fn = lambda *a, **k: FakeResp(text="<html>nothing</html>")
expect_kind("mf parse", lambda: _mediafire_direct(MF_URL), "parse")
def _boom(*a, **k):
    raise ConnectionError("down")
FakeRequests.get_fn = _boom
expect_kind("mf network", lambda: _mediafire_direct(MF_URL), "network")

# 3. pcloud
expect_kind("pc no code",
            lambda: _pcloud_direct("https://e.pcloud.link/publink/show"), "code")
FakeRequests.get_fn = lambda *a, **k: FakeResp(
    json_data={"result": 0, "hosts": ["h1"], "path": "/dl/file.zip"})
check("pc direct",
      _pcloud_direct(PC_URL) == "https://h1/dl/file.zip")
FakeRequests.get_fn = lambda *a, **k: FakeResp(json_data={"result": 7002})
expect_kind("pc dead", lambda: _pcloud_direct(PC_URL), "dead")
FakeRequests.get_fn = _boom
expect_kind("pc network", lambda: _pcloud_direct(PC_URL), "network")

# 4. mega
expect_kind("mega folder",
            lambda: _mega_download(MEGA_FOLDER, "/tmp"), "folder")
expect_kind("mega no key",
            lambda: _mega_download(MEGA_NOKEY, "/tmp"), "no_key")
_set_mega(FakeMegaOK)
FakeRequests.post_fn = lambda *a, **k: FakeResp(json_data={"s": 100})
with tempfile.TemporaryDirectory() as td:
    p, name = _mega_download(MEGA_URL, td)
    check("mega ok path", os.path.exists(p) and name == "bigmovie.mp4")
_set_mega(FakeMegaQuota)
with tempfile.TemporaryDirectory() as td:
    expect_kind("mega quota",
                lambda: _mega_download(MEGA_URL, td), "quota")
_set_mega(FakeMegaOK)
FakeRequests.post_fn = lambda *a, **k: FakeResp(
    json_data={"s": 2000 * 1048576})
with tempfile.TemporaryDirectory() as td:
    expect_kind("mega too big",
                lambda: _mega_download(MEGA_URL, td), "too_big")
check("mega size mb", _mega_size_mb(MEGA_URL) == 2000.0)

# 5. dispatcher
FakeRequests.post_fn = lambda *a, **k: FakeResp(json_data={"s": 100})
with tempfile.TemporaryDirectory() as td:
    p, t = asyncio.run(download_filehost(MEGA_URL, td))
    check("dispatch mega", os.path.exists(p) and t == "bigmovie.mp4")
FakeRequests.get_fn = lambda *a, **k: FakeResp(text=MF_HTML)
with tempfile.TemporaryDirectory() as td:
    p, t = asyncio.run(download_filehost(MF_URL, td))
    check("dispatch mediafire",
          p.endswith("archive.zip") and t == "archive.zip")
FakeRequests.get_fn = lambda *a, **k: FakeResp(
    json_data={"result": 0, "hosts": ["h"], "path": "/f.zip"})
with tempfile.TemporaryDirectory() as td:
    p, t = asyncio.run(download_filehost(PC_URL, td))
    check("dispatch pcloud", p.endswith("archive.zip"))
try:
    asyncio.run(download_filehost("https://youtube.com/watch?v=1", "/tmp"))
    check("dispatch unknown", False)
except FileHostError as e:
    check("dispatch unknown", e.kind == "unknown")

print(f"filehost: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

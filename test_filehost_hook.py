"""v6.5.0: download_web routes file hosts before yt-dlp; probe_size -> None."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import types as _types
_httpx_stub = _types.ModuleType("httpx")
class _HTTPError(Exception):
    pass
_httpx_stub.HTTPError = _HTTPError
sys.modules["httpx"] = _httpx_stub


import web_download as wd
import filehost as fh

MEGA_URL = "https://mega" + ".nz/file/AbCdEfGh#key1234567890"
MF_URL = "https://www.mediafire.com/file/abc123/test.zip/file"

called = {}
async def fake_fh(url, tmpdir, progress_cb=None, loop=None, tag="dd",
                  **kwargs):
    called["url"] = url
    p = os.path.join(tmpdir, "f.zip")
    with open(p, "w") as f:
        f.write("x")
    return p, "f.zip"

fh.download_filehost = fake_fh

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name)

with tempfile.TemporaryDirectory() as td:
    p, t = asyncio.run(wd.download_web(MEGA_URL, td))
    check("hook routes mega", called.get("url") == MEGA_URL and t == "f.zip")
    called.clear()
    p, t = asyncio.run(wd.download_web(MF_URL, td))
    check("hook routes mediafire", called.get("url") == MF_URL and t == "f.zip")

check("probe_size mega -> None",
      asyncio.run(wd.probe_size(MEGA_URL)) is None)
check("probe_size mediafire -> None",
      asyncio.run(wd.probe_size(MF_URL)) is None)
check("probe_size pcloud -> None",
      asyncio.run(wd.probe_size(
          "https://e.pcloud.link/publink/show?code=X")) is None)

# FileHostError must propagate unwrapped (kind intact) so callers can
# branch on it — e.g. the night queue skips permanent failures.
async def fake_fh_boom(url, tmpdir, progress_cb=None, loop=None, tag="dd",
                       **kwargs):
    raise fh.FileHostError("dead", "boom-msg")

fh.download_filehost = fake_fh_boom
with tempfile.TemporaryDirectory() as td:
    try:
        asyncio.run(wd.download_web(MEGA_URL, td))
        check("FileHostError unwrapped", False)
    except fh.FileHostError as e:
        check("FileHostError unwrapped",
              e.kind == "dead" and "boom-msg" in e.message)
    except Exception as e:  # noqa: BLE001
        check("FileHostError unwrapped", False)
        print("  wrong exc:", type(e).__name__)

print(f"hook: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

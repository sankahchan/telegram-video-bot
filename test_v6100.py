# test_v6100.py — v6.10.0 cancel web downloads (mocked IO, no network)
import asyncio
import sys
import threading
import urllib.request

sys.path.insert(0, ".")

import web_download
from web_download import (WebDownloadCancelled, download_direct_file,
                          download_web, _hook)
import filehost
import sendshare
import embed
import inspect

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name} {extra}")


# ---- exception ----
check("exc exists", issubclass(WebDownloadCancelled, Exception))
check("exc bilingual",
      "ရပ်လိုက်ပါပြီ" in WebDownloadCancelled.CANCEL_MSG
      and "cancelled" in WebDownloadCancelled.CANCEL_MSG.lower())

# ---- _hook ----
ev = threading.Event()
h = _hook(None, None, "📥", ev)
try:
    h({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 10})
    check("hook no-cancel ok", True)
except WebDownloadCancelled:
    check("hook no-cancel ok", False)
ev.set()
try:
    h({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 10})
    check("hook raises on cancel", False)
except WebDownloadCancelled:
    check("hook raises on cancel", True)
# hook without event (old callers) still fine
h2 = _hook(None, None, "📥")
try:
    h2({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 10})
    check("hook backwards compat", True)
except Exception as e:
    check("hook backwards compat", False, type(e).__name__)


# ---- download_direct_file ----
class FakeResp:
    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self.headers = headers or {"Content-Length": str(sum(len(c) for c in chunks))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


orig_urlopen = urllib.request.urlopen


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


import tempfile
# pre-set event -> immediate WebDownloadCancelled, no retry storm
urllib.request.urlopen = lambda req, timeout=60: FakeResp([b"x" * 100])
ev = threading.Event()
ev.set()
try:
    with tempfile.TemporaryDirectory() as td:
        run(download_direct_file("http://x.test/f.bin", td, cancel_event=ev))
    check("direct pre-cancel raises", False)
except WebDownloadCancelled:
    check("direct pre-cancel raises", True)
except Exception as e:
    check("direct pre-cancel raises", False, f"{type(e).__name__}: {e}")

# cancel mid-download
ev2 = threading.Event()


class SlowResp(FakeResp):
    def read(self, n):
        if self._chunks:
            ev2.set()  # user presses cancel after first chunk
        return super().read(n)


urllib.request.urlopen = lambda req, timeout=60: SlowResp([b"a" * 100] * 50)
try:
    with tempfile.TemporaryDirectory() as td:
        run(download_direct_file("http://x.test/f.bin", td, cancel_event=ev2))
    check("direct mid-cancel raises", False)
except WebDownloadCancelled:
    check("direct mid-cancel raises", True)
except Exception as e:
    check("direct mid-cancel raises", False, f"{type(e).__name__}: {e}")

# normal download still works
urllib.request.urlopen = lambda req, timeout=60: FakeResp([b"z" * 100] * 10)
try:
    import os
    with tempfile.TemporaryDirectory() as td:
        path, name = run(download_direct_file("http://x.test/f.bin", td))
        ok = name == "f.bin" and os.path.getsize(path) == 1000
    check("direct normal ok", ok)
except Exception as e:
    check("direct normal ok", False, f"{type(e).__name__}: {e}")
finally:
    urllib.request.urlopen = orig_urlopen

# ---- signatures ----
check("download_web cancel param",
      "cancel_event" in inspect.signature(download_web).parameters)
check("download_direct_file cancel param",
      "cancel_event" in inspect.signature(download_direct_file).parameters)
check("download_filehost cancel param",
      "cancel_event" in inspect.signature(filehost.download_filehost).parameters)
check("download_sendshare cancel param",
      "cancel_event" in inspect.signature(sendshare.download_sendshare).parameters)
check("download_embed cancel param",
      "cancel_event" in inspect.signature(embed.download_embed).parameters)
check("resolve_and_download cancel param",
      "cancel_event" in inspect.signature(embed.resolve_and_download).parameters)

# ---- pass-through wiring (static) ----
wd = open("web_download.py").read()
check("wd hook gets event", "_hook(progress_cb, loop, tag, cancel_event)" in wd)
check("wd filehost gets event", "cancel_event=cancel_event" in wd)
fh = open("filehost.py").read()
check("fh sendshare gets event", "download_sendshare, url, tmpdir, 1900, _sprog,\n                cancel_event)" in fh)
ss = open("sendshare.py").read()
check("sendshare checks event", "cancel_event.is_set()" in ss)
em = open("embed.py").read()
check("embed hook checks event", "cancel_event is not None and cancel_event.is_set()" in em)

# ---- bot.py wiring (static) ----
bot_src = open("bot.py").read()
check("bot imports WebDownloadCancelled", "WebDownloadCancelled" in bot_src)
check("bot registers web downloads",
      '"kind": "web"' in bot_src and "_active_downloads[w_token]" in bot_src)
check("bot registers stream downloads", '"kind": "stream"' in bot_src)
check("bot handles web cancel",
      "isinstance(e, WebDownloadCancelled)" in bot_src)
check("bot dl shows all kinds", 'kind_icon = {"torrent": "🧲", "web": "🌐", "stream": "🎬"}' in bot_src)
check("bot dl no torrent-only text", "ဒေါင်းနေတဲ့ torrent မရှိပါ" not in bot_src)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)

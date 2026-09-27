"""v6.14.0 tests — headless-Chromium fallback for Cloudflare challenges.

2026-09-28 02:21 KST: andyday.sx served a full Cloudflare managed JS
challenge even after the v6.13.4 hv=1 trick, so the watch flow died with a
"try again later" message. v6.14.0 renders the page in headless Chromium
(Playwright) when the challenge is detected, waits for window.__OPT, and
continues with the normal embed extraction.

playwright is NOT installed in this test env — every test fakes it via
sys.modules (or asserts graceful degradation when it's missing).
"""
import os
import sys
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import watch
import watch_browser
from watch_browser import (browser_available,
                           fetch_watch_html_via_browser)
from watch import WatchError, fetch_watch_embeds

REAL_HTML = ('<html><head><title>Lioness (2023)</title>'
             '<meta property="og:title" content="Lioness (2023)"></head>'
             '<body><script>window.__OPT=["https://vidnest.com/embed/1",'
             '"https://vidlink.pro/embed/2"];</script></body></html>')
CF_HTML = ('<html><head><title>andyday.sx</title>'
           '<script src="/challenge-platform/scripts/jsd/main.js"></script>'
           '</head><body>__CF$cvParams</body></html>')

_results = []


def check(name, cond):
    _results.append((name, bool(cond)))
    print(("✅ " if cond else "❌ ") + name, flush=True)


def _install_fake_playwright(evaluate_fn=None, launch_exc=None,
                             goto_exc=None):
    """Fake `playwright.sync_api.sync_playwright` in sys.modules."""
    calls = {"launched": 0, "closed": 0, "goto": 0}

    class _Page:
        def goto(self, url, wait_until=None, timeout=None):
            calls["goto"] += 1
            if goto_exc:
                raise goto_exc
        def evaluate(self, js):
            return evaluate_fn(js) if evaluate_fn else False
        def content(self):
            return REAL_HTML

    class _Ctx:
        def new_page(self):
            return _Page()

    class _Browser:
        def new_context(self, **kw):
            calls["ctx_kw"] = kw
            return _Ctx()
        def close(self):
            calls["closed"] += 1

    class _Chromium:
        def launch(self, **kw):
            calls["launched"] += 1
            calls["launch_kw"] = kw
            if launch_exc:
                raise launch_exc
            return _Browser()

    class _PW:
        chromium = _Chromium()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: _PW()
    pkg = types.ModuleType("playwright")
    pkg.sync_api = mod
    sys.modules["playwright"] = pkg
    sys.modules["playwright.sync_api"] = mod
    return calls


def _remove_fake_playwright():
    sys.modules.pop("playwright", None)
    sys.modules.pop("playwright.sync_api", None)


# 1: no playwright installed -> browser_available() False, fetch -> None
_remove_fake_playwright()
check("no playwright: browser_available() False",
      browser_available() is False)
check("no playwright: fetch returns None",
      fetch_watch_html_via_browser("https://x.test/", timeout=5) is None)

# 2: challenge clears -> HTML returned, browser closed, root-safe flags
calls = _install_fake_playwright(evaluate_fn=lambda js: True)
check("playwright present: browser_available() True",
      browser_available() is True)
html = fetch_watch_html_via_browser("https://andyday.sx/watch/tv-x",
                                    timeout=10)
check("challenge clears: real HTML returned",
      html is not None and "window.__OPT" in html)
check("browser launched once and closed",
      calls["launched"] == 1 and calls["closed"] == 1)
check("--no-sandbox passed (bot runs as root)",
      "--no-sandbox" in calls["launch_kw"].get("args", []))
check("--disable-dev-shm-usage passed (tiny /dev/shm)",
      "--disable-dev-shm-usage" in calls["launch_kw"].get("args", []))
_remove_fake_playwright()

# 3: challenge never clears -> None after timeout (no hang)
calls = _install_fake_playwright(evaluate_fn=lambda js: False)
import time
t0 = time.time()
html = fetch_watch_html_via_browser("https://andyday.sx/watch/tv-x",
                                    timeout=4)
dt = time.time() - t0
check("challenge persists: None returned", html is None)
check("bounded wait (no hang)", dt < 15)
check("browser still closed on timeout", calls["closed"] == 1)
_remove_fake_playwright()

# 4: launch explodes -> None, never raises
calls = _install_fake_playwright(launch_exc=RuntimeError("no chrome"))
try:
    html = fetch_watch_html_via_browser("https://x.test/", timeout=5)
    check("launch failure: None, no raise", html is None)
except Exception:
    check("launch failure: None, no raise", False)
_remove_fake_playwright()

# 5: lock busy (another user mid-fallback) -> skip, None
calls = _install_fake_playwright(evaluate_fn=lambda js: True)
watch_browser._browser_lock.acquire()
try:
    html = fetch_watch_html_via_browser("https://x.test/", timeout=5)
    check("lock busy: skipped with None", html is None)
    check("lock busy: no browser launched", calls["launched"] == 0)
finally:
    watch_browser._browser_lock.release()
_remove_fake_playwright()


class _FakeResp:
    def __init__(self, text):
        self.text = text
    def raise_for_status(self):
        pass


class _FakeClient:
    """httpx stand-in: always serves the Cloudflare challenge wall."""
    def __init__(self):
        self.cookies = self
    def set(self, *a, **k):
        pass
    def get(self, url, headers=None, timeout=None):
        return _FakeResp(CF_HTML)
    def close(self):
        pass


# 6: integration — CF wall + browser recovers -> embeds returned
orig_fetch = watch.fetch_watch_html_via_browser
orig_avail = watch.browser_available
watch.fetch_watch_html_via_browser = lambda url, timeout=90: REAL_HTML
watch.browser_available = lambda: True
try:
    title, embeds = fetch_watch_embeds(
        "https://andyday.sx/watch/tv-lioness-xyz", client=_FakeClient())
    check("CF wall + browser: embeds recovered",
          len(embeds) == 2 and embeds[0][0] == "vidsrc.mov")
    check("CF wall + browser: title parsed", title == "Lioness")  # year stripped by extract_watch_title
finally:
    watch.fetch_watch_html_via_browser = orig_fetch
    watch.browser_available = orig_avail

# 7: integration — CF wall + browser fails, playwright present -> clear msg
watch.fetch_watch_html_via_browser = lambda url, timeout=90: None
watch.browser_available = lambda: True
try:
    fetch_watch_embeds("https://andyday.sx/watch/tv-lioness-xyz",
                       client=_FakeClient())
    check("CF wall + browser fail: WatchError raised", False)
except WatchError as e:
    check("CF wall + browser fail: WatchError raised", True)
    check("message says browser tried",
          "browser နဲ့ စမ်းပေမယ့်" in str(e))
finally:
    watch.fetch_watch_html_via_browser = orig_fetch
    watch.browser_available = orig_avail

# 8: integration — CF wall, no playwright -> old "no browser" message
watch.fetch_watch_html_via_browser = lambda url, timeout=90: None
watch.browser_available = lambda: False
try:
    fetch_watch_embeds("https://andyday.sx/watch/tv-lioness-xyz",
                       client=_FakeClient())
    check("CF wall, no playwright: WatchError raised", False)
except WatchError as e:
    check("CF wall, no playwright: WatchError raised", True)
    check("message says VPS has no browser",
          "browser မရှိလို့" in str(e))
finally:
    watch.fetch_watch_html_via_browser = orig_fetch
    watch.browser_available = orig_avail

print()
failed = [n for n, ok in _results if not ok]
print(f"{len(_results) - len(failed)}/{len(_results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)

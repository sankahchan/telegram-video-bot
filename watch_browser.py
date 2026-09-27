"""v6.14.0 — headless-Chromium fallback for Cloudflare-challenged watch pages.

On 2026-09-28 02:21 KST andyday.sx served a full Cloudflare managed JS
challenge to the VPS even after the v6.13.4 hv=1 cookie trick — plain HTTP
cannot pass that, so the watch flow died with a "try again later" message.
This module renders the page in a real (headless) Chromium via Playwright,
waits for the challenge to clear (window.__OPT appears), and returns the
final HTML so the normal embed extraction can continue.

Design notes:
- playwright is OPTIONAL. Nothing here imports it at module load; every
  entry point degrades to None/False when it isn't installed, so the bot
  keeps its old behavior on a VPS without the browser.
- All functions are BLOCKING (playwright sync API) — callers run them in a
  worker thread (fetch_watch_embeds already runs via asyncio.to_thread).
- One browser at a time (module lock): the VPS is small (Outline + bot),
  and a Cloudflare fallback is rare — serialize instead of risking RAM
  exhaustion when two users hit a challenge simultaneously.
- Chromium refuses to run as root without --no-sandbox, and the bot's
  systemd service runs as root -> both flags are always passed, plus
  --disable-dev-shm-usage (tiny /dev/shm on VPSes).
"""

import threading
import time

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_LAUNCH_ARGS = [
    "--no-sandbox",                    # bot runs as root (systemd, no User=)
    "--disable-dev-shm-usage",         # /dev/shm is tiny on VPSes
    "--disable-blink-features=AutomationControlled",
]

_browser_lock = threading.Lock()


def browser_available() -> bool:
    """True when playwright is importable (pip package installed)."""
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def fetch_watch_html_via_browser(url: str, timeout: float = 90) -> str | None:
    """Render *url* in headless Chromium, wait for the Cloudflare challenge
    to clear, return the final page HTML — or None when the browser is
    unavailable, busy, or the challenge never clears. Never raises."""
    try:
        from playwright.sync_api import sync_playwright  # noqa
    except Exception as e:
        print(f"🌐 browser fallback: playwright not installed ({e})",
              flush=True)
        return None
    if _browser_lock.locked():
        print("🌐 browser fallback: another browser run in progress — "
              "skipping", flush=True)
        return None
    with _browser_lock:
        try:
            return _render(url, timeout)
        except Exception as e:
            print(f"🌐 browser fallback failed: {type(e).__name__}: {e}",
                  flush=True)
            return None


def _render(url: str, timeout: float) -> str | None:
    from playwright.sync_api import sync_playwright
    deadline = time.time() + timeout
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        try:
            ctx = browser.new_context(
                user_agent=BROWSER_UA,
                viewport={"width": 1366, "height": 768},
                locale="en-US",
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Cloudflare's challenge auto-clears and lands on the real page;
            # window.__OPT (the server list) only exists after its JS runs.
            while time.time() < deadline:
                try:
                    ok = page.evaluate(
                        "() => typeof window.__OPT !== 'undefined'")
                except Exception:
                    ok = False
                if ok:
                    html = page.content()
                    print(f"✅ browser fallback: challenge cleared, "
                          f"{len(html)}b", flush=True)
                    return html
                time.sleep(2)
            print("⛔ browser fallback: challenge did not clear in time",
                  flush=True)
            return None
        finally:
            try:
                browser.close()
            except Exception:
                pass

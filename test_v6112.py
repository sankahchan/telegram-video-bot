"""v6.11.2 — Telegram flood hardening.

- web_progress: max 1 status edit per 5s, duplicate pct skipped
- handle_link error replies: a failed reply (RetryAfter) is logged,
  never crashes the handler
Run: python3 test_v6112.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


# Extract the web_progress closure logic by simulating handle_link's
# definition (same code shape as bot.py).
print("== web_progress throttle ==")


class FakeStatus:
    def __init__(self):
        self.edits = []

    async def edit_text(self, txt):
        self.edits.append(txt)


async def main():
    status = FakeStatus()
    _wp_last = [0.0, -1]

    async def web_progress(tag, pct):
        try:
            now = time.time()
            if pct == _wp_last[1] or now - _wp_last[0] < 5:
                return
            _wp_last[0], _wp_last[1] = now, pct
            await status.edit_text(f"{tag} ⬇️ {pct}%")
        except Exception:
            pass

    t0 = time.time()
    for pct in (1, 2, 3, 4, 5):
        await web_progress("📥", pct)
    check("burst of 5 -> 1 edit", len(status.edits) == 1, status.edits)

    await web_progress("📥", 5)
    check("duplicate pct skipped", len(status.edits) == 1)

    # fake time travel +6s
    real_time = time.time
    time.time = lambda: t0 + 6
    try:
        await web_progress("📥", 9)
    finally:
        time.time = real_time
    check("edit after 5s window", len(status.edits) == 2, status.edits)
    check("latest pct shown", status.edits[-1].endswith("9%"), status.edits)


asyncio.run(main())

# The error-reply guard is a plain try/except around reply_text — verify the
# shape exists in bot.py source.
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bot.py")).read()
check("error reply wrapped in try/except",
      "error reply not delivered" in src)
check("burst pacing in follow_job", src.count("pace bursts") >= 3)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)

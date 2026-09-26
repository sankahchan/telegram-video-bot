"""v5.4.7 mock tests — no network. Run: python3 test_v547.py"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from filecache import CACHE_VERSION, make_key

PASS = []


def check(name, cond):
    assert cond, f"FAIL: {name}"
    PASS.append(name)


# --- version is mixed into the key -------------------------------------------
k = make_key("web", "https://x.com/a", "high", "video")
norm = "web|https://x.com/a|high|video"
old_style = hashlib.sha1(norm.encode("utf-8")).hexdigest()
check("key != pre-version key", k != old_style)
check("key == versioned sha1",
      k == hashlib.sha1(
          f"{CACHE_VERSION}|{norm}".encode("utf-8")).hexdigest())

# --- key still varies with inputs ----------------------------------------------
check("quality changes key",
      make_key("web", "https://x.com/a", "low", "video") != k)
check("url changes key",
      make_key("web", "https://x.com/b", "high", "video") != k)
check("same inputs -> same key",
      make_key("web", "https://x.com/a", "high", "video") == k)

# --- version constant is bumped for the normalize pipeline ----------------------
check("CACHE_VERSION set", bool(CACHE_VERSION))
check("CACHE_VERSION bumped past v546 (v572 ios remux gen)",
      CACHE_VERSION not in ("v546",) and CACHE_VERSION >= "v572")

print(f"✅ v5.4.7: {len(PASS)} tests passed")

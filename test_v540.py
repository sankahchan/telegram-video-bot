"""v5.4.0 mock tests — no network. Run: python3 test_v540.py"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import web_download as wd
import x_media
from x_media import (
    extract_tweet_id, extract_profile_user, parse_timeline_args,
    syndication_token, _best_variant, _parse_fx_media,
    _parse_syndication_media, _classify_error, XMediaError,
)
from filecache import FileIdCache, make_key

PASS = []


def check(name, cond):
    assert cond, f"FAIL: {name}"
    PASS.append(name)


# --- 1. tweet ID regex -------------------------------------------------------
check("tid x.com", extract_tweet_id("https://x.com/NASA/status/1234567890123456789") == "1234567890123456789")
check("tid twitter", extract_tweet_id("http://twitter.com/user/status/42") == "42")
check("tid /i/status/", extract_tweet_id("https://x.com/i/status/99") == "99")
check("tid mobile+query", extract_tweet_id("https://mobile.twitter.com/u/status/7?s=20&t=abc") == "7")
check("tid profile->None", extract_tweet_id("https://x.com/NASA") is None)
check("tid garbage->None", extract_tweet_id("hello world") is None)
check("profile @", extract_profile_user("@NASA") == "NASA")
check("profile url", extract_profile_user("https://x.com/SpaceX") == "SpaceX")
check("profile tweet->None", extract_profile_user("https://x.com/NASA/status/123") is None)
check("profile reserved->None", extract_profile_user("https://x.com/home") is None)

# --- 2. syndication token ------------------------------------------------------
t1 = syndication_token("1986135789756162205")
check("token nonempty", bool(t1))
check("token no 0/dot", "0" not in t1 and "." not in t1)
check("token charset", all(c in "123456789abcdefghijklmnopqrstuvwxyz" for c in t1))
check("token deterministic", syndication_token("1986135789756162205") == t1)
check("token differs per id", syndication_token("1466447129178783744") != t1)
check("token bad id", syndication_token("nope") == "x")
# cross-check against Node's exact JS Number.toString(36) when available
node = shutil.which("node")
if node:
    import subprocess
    for tid in ("1986135789756162205", "1466447129178783744"):
        js = subprocess.run(
            [node, "-e",
             "console.log((Number(process.argv[1])/1e15*Math.PI).toString(36)"
             ".replace(/(0+|\\.)/g,''))", tid],
            capture_output=True, text=True, timeout=15).stdout.strip()
        mine = syndication_token(tid)
        check(f"token prefix-matches JS toString(36) for {tid}",
              mine.startswith(js) and len(js) >= 8)
else:
    print("SKIP node cross-check (no node)")

# --- 3. media parsing (mock payloads, no network) ------------------------------
fx_tweet = {"text": "hello", "media": {"all": [
    {"type": "video", "variants": [
        {"url": "https://v/v1.mp4", "bitrate": 100, "content_type": "video/mp4"},
        {"url": "https://v/v2.mp4", "bitrate": 900, "content_type": "video/mp4"},
        {"url": "https://v/v.m3u8", "bitrate": 9999}],
     },
    {"type": "photo", "url": "https://p/p1.jpg"},
]}}
items = _parse_fx_media(fx_tweet)
check("fx video best bitrate", items[0]["url"] == "https://v/v2.mp4")
check("fx video kind", items[0]["kind"] == "video" and items[0]["title"] == "hello")
check("fx photo", items[1] == {"url": "https://p/p1.jpg", "kind": "photo", "title": "hello"})
check("best skips m3u8", _best_variant([{"url": "a.m3u8", "bitrate": 9}]) is None)
check("best empty", _best_variant([]) is None)
check("pick video first",
      wd._pick_x_video([{"kind": "photo", "url": "p"},
                        {"kind": "video", "url": "v"}])["url"] == "v")
try:
    wd._pick_x_video([{"kind": "photo", "url": "p"}])
    check("pick no-video raises", False)
except XMediaError as ex:
    check("pick no-video raises", ex.kind == "no_media")
syn = {"text": "t", "mediaDetails": [
    {"type": "video", "video_info": {"variants": [
        {"url": "https://s/lo.mp4", "bitrate": 200},
        {"url": "https://s/hi.mp4", "bitrate": 1500}]}},
    {"type": "photo", "media_url_https": "https://s/p.jpg"},
]}
sitems = _parse_syndication_media(syn)
check("syn video", sitems[0]["url"] == "https://s/hi.mp4")
check("syn photo", sitems[1]["url"] == "https://s/p.jpg")
check("classify 404", _classify_error("HTTP 404") == "not_found")
check("classify private", _classify_error("401 private") == "private")
check("classify age", _classify_error("sensitive content") == "age_restricted")
check("classify 429", _classify_error("429 rate limit") == "rate_limited")
check("classify timeout", _classify_error("connect timeout") == "network")
check("classify other", _classify_error("weird") == "no_media")
e = XMediaError("private", "msg")
check("XMediaError kind", e.kind == "private" and str(e) == "msg")

# --- 4. /xtimeline arg parsing --------------------------------------------------
check("tl default", parse_timeline_args(["@NASA"]) == ("NASA", 5))
check("tl n", parse_timeline_args(["@NASA", "3"]) == ("NASA", 3))
check("tl clamp hi", parse_timeline_args(["@NASA", "99"]) == ("NASA", 10))
check("tl clamp lo", parse_timeline_args(["@NASA", "0"]) == ("NASA", 1))
check("tl bad n", parse_timeline_args(["@NASA", "xyz"]) == ("NASA", 5))
check("tl no at", parse_timeline_args(["NASA", "5"]) == ("NASA", 5))
check("tl empty", parse_timeline_args([]) == (None, 5))
check("tl bad user", parse_timeline_args(["https://x.com/NASA/status/1"]) == (None, 5))

# --- 5. file_id cache ------------------------------------------------------------
tmp = tempfile.mkdtemp()
try:
    c = FileIdCache(path=os.path.join(tmp, "c.json"))
    k1 = make_key("web", "https://x.com/a", "high", "video")
    check("key sha1", k1 == hashlib.sha1(b"web|https://x.com/a|high|video").hexdigest())
    check("key differs", make_key("web", "https://x.com/a", "low", "video") != k1)
    check("get miss", c.get(k1) is None)
    c.set(k1, "FILEID123", "video", "cap")
    got = c.get(k1)
    check("get hit", got["file_id"] == "FILEID123" and got["kind"] == "video")
    check("no tmp left", not os.path.exists(os.path.join(tmp, "c.json.tmp")))
    # reload persists
    c2 = FileIdCache(path=os.path.join(tmp, "c.json"))
    check("reload", c2.get(k1)["file_id"] == "FILEID123")
    # TTL prune: age one entry past ttl
    c2.data[k1]["ts"] -= (c2.ttl + 10)
    c2._save()
    c3 = FileIdCache(path=os.path.join(tmp, "c.json"))
    check("ttl prune", c3.get(k1) is None)
    # clear count
    c3.set("a", "f1", "video")
    c3.set("b", "f2", "photo")
    check("clear count", c3.clear() == 2 and c3.get("a") is None)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# --- 6. per-site cookie selection --------------------------------------------------
ctmp = tempfile.mkdtemp()
old_dir, old_cookie = wd.DATA_DIR, wd.COOKIE_FILE
try:
    wd.DATA_DIR = ctmp
    wd.COOKIE_FILE = os.path.join(ctmp, "cookies.txt")
    open(wd.COOKIE_FILE, "w").write("x")
    check("cookie fallback yt", wd.cookie_file_for("https://youtube.com/watch?v=1") == wd.COOKIE_FILE)
    open(os.path.join(ctmp, "cookies_youtube.txt"), "w").write("y")
    check("cookie yt site", wd.cookie_file_for("https://youtu.be/abc") ==
          os.path.join(ctmp, "cookies_youtube.txt"))
    check("cookie ig fallback", wd.cookie_file_for("https://instagram.com/p/1") == wd.COOKIE_FILE)
    open(os.path.join(ctmp, "cookies_twitter.txt"), "w").write("t")
    check("cookie x site", wd.cookie_file_for("https://x.com/u/status/1") ==
          os.path.join(ctmp, "cookies_twitter.txt"))
    check("cookie other", wd.cookie_file_for("https://tiktok.com/@a/1") == wd.COOKIE_FILE)
    os.remove(wd.COOKIE_FILE)
    check("cookie none", wd.cookie_file_for("https://tiktok.com/@a/1") is None)
finally:
    wd.DATA_DIR, wd.COOKIE_FILE = old_dir, old_cookie
    shutil.rmtree(ctmp, ignore_errors=True)

# --- 7. PO-token extractor args + proxy --------------------------------------------
old_pot = wd._pot_ok
old_proxy = wd.YTDLP_PROXY
try:
    wd._pot_ok = True  # pretend provider reachable
    wd._pot_checked_at = time.monotonic()
    ea = wd.pot_extractor_args("https://youtube.com/watch?v=1")
    check("pot key exact", ea == {"youtubepot-bgutilhttp": {"base_url": wd.POT_PROVIDER_URL}})
    check("pot non-yt", wd.pot_extractor_args("https://x.com/a") == {})
    opts = wd._base_opts("/tmp/o", "b", ["android"], "https://youtube.com/watch?v=1")
    check("pot in _base_opts",
          opts["extractor_args"]["youtubepot-bgutilhttp"]["base_url"] == wd.POT_PROVIDER_URL)
    check("pot keeps player_client",
          opts["extractor_args"]["youtube"] == {"player_client": ["android"]})
    wd._pot_ok = False  # provider down -> silent skip
    wd._pot_checked_at = time.monotonic()
    check("pot unset", wd.pot_extractor_args("https://youtube.com/watch?v=1") == {})
    opts2 = wd._base_opts("/tmp/o", "b", ["android"], "https://youtube.com/watch?v=1")
    check("pot absent in opts", "youtubepot-bgutilhttp" not in opts2.get("extractor_args", {}))
    wd.YTDLP_PROXY = "socks5://u:p@h:1"
    opts3 = wd._base_opts("/tmp/o", "b", None, "https://tiktok.com/x")
    check("proxy set", opts3.get("proxy") == "socks5://u:p@h:1")
    wd.YTDLP_PROXY = ""
    check("proxy unset", "proxy" not in wd._base_opts("/tmp/o", "b"))
    # TTL: fresh timestamp -> cached value kept, no re-probe
    wd._pot_ok = True
    wd._pot_checked_at = time.monotonic()
    check("pot ttl fresh", wd._pot_available() is True)
    # TTL: stale timestamp -> re-probe (no server here -> False)
    wd._pot_ok = True
    wd._pot_checked_at = time.monotonic() - 9999
    check("pot ttl stale re-probes", wd._pot_available() is False)
finally:
    wd._pot_ok, wd.YTDLP_PROXY = old_pot, old_proxy

print(f"\n✅ v5.4.0 mock tests: {len(PASS)} passed")

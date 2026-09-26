"""v6.0.0 tests: quota, history, subs languages, /info, bookmarks, follow notify.

Run: python3 test_v600.py
"""
import ast
import json
import os
import re
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store

PASS = []


def check(name, cond):
    PASS.append(name)
    assert cond, f"FAIL: {name}"


tmp = tempfile.mkdtemp()
store.DATA_DIR = tmp
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bot.py"), encoding="utf-8").read()


def fullmatch_patterns():
    tree = ast.parse(SRC)
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "fullmatch"
                and node.args and isinstance(node.args[0], ast.Constant)):
            out.append(node.args[0].value)
    return out


# --- quota (UserStore + StatsStore) -------------------------------------------
us = store.UserStore()
us.add(111, months=1)
check("quota default None", us.quota_mb(111) is None)
check("quota set", us.set_quota(111, 51200) is True)
check("quota get", us.quota_mb(111) == 51200)
check("quota unset", us.set_quota(111, None) is True
      and us.quota_mb(111) is None)
check("quota unknown user", us.set_quota(999, 100) is False)

st = store.StatsStore()
st.log(100, "web", 111, url="https://x.com/a", ckey="k1")
st.log(50, "torrent", 111)
st.log(10, "web", 222, url="https://t.me/b")
check("usage sums 30d", st.usage(111, days=30) == 150)
check("usage per user", st.usage(222, days=30) == 10)
rec = st.recent(111, 10)
check("recent newest-first", rec[0]["mb"] == 50 and rec[1]["mb"] == 100)
check("recent stores url+ckey", rec[1]["url"] == "https://x.com/a"
      and rec[1]["ckey"] == "k1")
check("recent filters user", all(r["user"] == 111 for r in rec))

# --- bookmarks ----------------------------------------------------------------
bm = store.BookmarkStore()
check("bm add", bm.add(111, "https://x.com/v/1", "t1") is True)
check("bm dup rejected", bm.add(111, "https://x.com/v/1") is False)
bm.add(111, "magnet:?xt=urn:btih:abc", "m1")
items = bm.list(111)
check("bm list", len(items) == 2 and items[0]["url"] == "https://x.com/v/1")
check("bm remove", bm.remove(111, 0) is True
      and len(bm.list(111)) == 1)
check("bm remove bad idx", bm.remove(111, 9) is False)
check("bm per-user", bm.list(222) == [])

# --- follow notify mode --------------------------------------------------------
from follow import FollowStore
fl = FollowStore()
fid = fl.add(111, "https://rss.example/f", "Show", 111, {})
check("follow default auto", fl.list(111)[fid].get("mode") == "auto")
check("set notify", fl.set_mode(111, fid, "notify") is True)
check("mode notify", fl.list(111)[fid]["mode"] == "notify")
check("set auto", fl.set_mode(111, fid, "auto") is True)
check("bad mode", fl.set_mode(111, fid, "xx") is False)
check("bad fid", fl.set_mode(111, "nope", "notify") is False)

# --- subs languages -------------------------------------------------------------
from subs import LANG_ALIASES, movie_languages

check("alias mm", LANG_ALIASES["mm"] == "Burmese")
check("alias en", LANG_ALIASES["en"] == "English")
check("alias myanmar", LANG_ALIASES["myanmar"] == "Burmese")

import subs as subs_mod
HTML = """
<tr data-id="1">
<td class="rating-cell"><span class="label">8</span></td>
<span class="sub-lang">English</span>
<a href="/subtitles/dune-en">subtitle</a>
<tr data-id="2">
<td class="rating-cell"><span class="label">9</span></td>
<span class="sub-lang">English</span>
<a href="/subtitles/dune-en2">subtitle</a>
<tr data-id="3">
<td class="rating-cell"><span class="label">7</span></td>
<span class="sub-lang">Burmese</span>
<a href="/subtitles/dune-mm">subtitle</a>
"""


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


_real_httpx = subs_mod.httpx
fake = types.SimpleNamespace(
    get=lambda url, timeout=None, headers=None: _FakeResp(HTML))
subs_mod.httpx = fake
try:
    langs = movie_languages("tt1234567")
finally:
    subs_mod.httpx = _real_httpx
check("langs english first", langs[0] == "English")
check("langs has burmese", "Burmese" in langs)

# --- web_info (mocked yt_dlp) ----------------------------------------------------
import web_download

INFO = {
    "title": "Test Video", "duration": 125, "uploader": "Chan",
    "extractor_key": "YouTube",
    "formats": [
        {"vcodec": "avc1", "ext": "mp4", "height": 720, "width": 1280,
         "filesize": 50 * 1048576, "filesize_approx": None},
        {"vcodec": "avc1", "ext": "mp4", "height": 1080, "width": 1920,
         "filesize": None, "filesize_approx": 120 * 1048576},
        {"vcodec": "none", "ext": "m4a", "height": None,
         "filesize": 5 * 1048576, "filesize_approx": None},
    ],
}


class _FakeYDL:
    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return INFO


fake_ydl = types.ModuleType("yt_dlp")
fake_ydl.YoutubeDL = _FakeYDL
sys.modules["yt_dlp"] = fake_ydl
try:
    d = web_download.web_info("https://youtube.com/watch?v=x")
finally:
    del sys.modules["yt_dlp"]
check("web_info title", d["title"] == "Test Video")
check("web_info duration", d["duration"] == 125)
check("web_info skips audio-only",
      all(f["height"] for f in d["formats"]))
check("web_info sorted desc",
      [f["height"] for f in d["formats"]] == [1080, 720])
check("web_info mb", d["formats"][0]["mb"] == 120.0)

# --- bot.py static wiring ---------------------------------------------------------
for cmd, fn in [("quota", "quota_cmd"), ("history", "history_cmd"),
                ("info", "info_cmd"), ("bookmark", "bookmark_cmd"),
                ("bookmarks", "bookmarks_cmd"),
                ("unbookmark", "unbookmark_cmd")]:
    check(f"registers /{cmd}",
          f'("{cmd}", {fn})' in SRC)

for cmd, emoji in [("history", "🕘"), ("info", "ℹ️"),
                   ("bookmark", "🔖"), ("bookmarks", "🔖"),
                   ("quota", "📊")]:
    check(f"BOT_COMMANDS /{cmd}",
          re.search(rf'\("{cmd}", "{emoji}[^"]*"\),', SRC) is not None)

pats = fullmatch_patterns()
for pat, sample in [(r"subl:(\d+)", "subl:2"),
                    (r"hist:(\d+)", "hist:3"),
                    (r"bm:(dl|del):(\d+)", "bm:del:0"),
                    (r"fl:mode:([A-Za-z0-9]+)", "fl:mode:tvabcd1234")]:
    check(f"route {pat} exists+matches",
          any(p == pat and re.fullmatch(p, sample) for p in pats))

for topic in ["history", "info", "bookmark", "bookmarks",
              "unbookmark", "quota"]:
    check(f"help topic {topic}", f'"{topic}": (' in SRC)
check("help overview new cmds",
      "/history" in SRC and "/quota <id> [GB|off]" in SRC)
check("menu history/bookmarks buttons",
      'callback_data="menu:history"' in SRC
      and 'callback_data="menu:bookmarks"' in SRC)
check("menu_cb handles",
      'action == "history"' in SRC and 'action == "bookmarks"' in SRC)

n_quota_sites = SRC.count("quota_allows(uid)")
check("quota enforced >=6 sites", n_quota_sites >= 6)

check("quota in _user_line", "quota_mb(uid)" in SRC)
check("quota in admin text", "/quota <id> <GB|off>" in SRC)
check("deliver src_url param", "src_url: str | None = None" in SRC)
check("stats.log url+ckey", "url=src_url, ckey=cache_key" in SRC)
check("notify branch in follow_job",
      'f.get("mode", "auto") == "notify"' in SRC)
check("follows toggle kb", "_follows_kb" in SRC
      and "fl:mode:" in SRC)
check("subs lang flow",
      "_show_langs" in SRC and "subl_pick" in SRC
      and "pending_sub_lang" in SRC)
check("web_info import", "web_info," in SRC)
check("magnet/tg/web info helpers",
      "_magnet_info" in SRC and "_tg_info" in SRC
      and "_web_info_text" in SRC)

print(f"✅ v6.0.0: {len(PASS)} passed")

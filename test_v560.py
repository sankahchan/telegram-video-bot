"""v5.6.0 tests: series auto-follow (RSS), torrent subtitles, Drive upload.

Network-free: feedparser.parse and store paths are monkeypatched.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = []


def check(name, cond):
    PASS.append(name)
    assert cond, f"FAIL: {name}"


# ---------------------------------------------------------------- subtitles
from torrent_download import pick_targets, _SUB_EXTS  # noqa: E402

check("srt is sub ext", ".srt" in _SUB_EXTS)

movie_pack = [
    {"index": "1", "path": "./mov/movie.mkv", "size": 1_500_000_000},
    {"index": "2", "path": "./mov/movie.srt", "size": 50_000},
    {"index": "3", "path": "./mov/movie.en.ass", "size": 80_000},
    {"index": "4", "path": "./mov/poster.jpg", "size": 200_000},
    {"index": "5", "path": "./mov/readme.nfo", "size": 1_000},
]
got = pick_targets(movie_pack)
paths = [f["path"] for f in got]
check("video picked", "./mov/movie.mkv" in paths)
check("srt picked", "./mov/movie.srt" in paths)
check("ass picked", "./mov/movie.en.ass" in paths)
check("poster skipped", "./mov/poster.jpg" not in paths)
check("nfo skipped", "./mov/readme.nfo" not in paths)

# subs don't consume the media cap
eps = [{"index": str(i), "path": f"./s/e{i:02d}.mp4", "size": 50_000_000}
       for i in range(1, 22)]
eps += [{"index": f"s{i}", "path": f"./s/e{i:02d}.srt", "size": 40_000}
        for i in range(1, 5)]
got = pick_targets(eps)
media_n = sum(1 for f in got if f["path"].endswith(".mp4"))
sub_n = sum(1 for f in got if f["path"].endswith(".srt"))
check("media capped at 20", media_n == 20)
check("all 4 subs included", sub_n == 4)

# sub cap
many_subs = ([{"index": "1", "path": "./m/v.mp4", "size": 100_000_000}] +
             [{"index": f"x{i}", "path": f"./m/{i}.srt", "size": 10_000}
              for i in range(10)])
got = pick_targets(many_subs)
check("subs capped at 5",
      sum(1 for f in got if f["path"].endswith(".srt")) == 5)

print(f"✅ v5.6.0 subtitles: {len(PASS)} tests passed")

# ---------------------------------------------------------------- follow store
import store  # noqa: E402
import follow  # noqa: E402

_tmp = tempfile.mkdtemp()
store.DATA_DIR = _tmp  # FollowStore writes here now

fs = follow.FollowStore()
fid = fs.add(111, "http://example.com/show.rss", "My Show", 222,
             {"g1": 3})
check("add returns id", isinstance(fid, str) and len(fid) == 10)
lst = fs.list(111)
check("list has feed", fid in lst and lst[fid]["name"] == "My Show")
check("chat stored", lst[fid]["chat_id"] == 222)
allf = fs.all()
check("all() int keys", 111 in allf and fid in allf[111])

n = fs.bump_attempt(111, fid, "g2")
check("bump 1", n == 1)
n = fs.bump_attempt(111, fid, "g2")
check("bump 2", n == 2)
fs.mark_seen(111, fid, "g2")
check("mark_seen maxes attempts",
      fs.list(111)[fid]["seen"]["g2"] == follow.MAX_ATTEMPTS)

items = [{"guid": "g1", "title": "ep1", "link": "magnet:?a"},
         {"guid": "g2", "title": "ep2", "link": "magnet:?b"},
         {"guid": "g3", "title": "ep3", "link": "magnet:?c"}]
check("new_items filters seen",
      [i["guid"] for i in follow.new_items(fs.list(111)[fid], items)] == ["g3"])

# transient: attempts < MAX -> still retried
fs2 = follow.FollowStore()
fid2 = fs2.add(111, "http://example.com/o.rss", "Other", 222, {"g9": 1})
fresh = follow.new_items(fs2.list(111)[fid2],
                         [{"guid": "g9", "title": "e", "link": "magnet:?x"}])
check("retry within budget", len(fresh) == 1)

check("remove", fs.remove(111, fid) is True)
check("remove missing", fs.remove(111, "nope") is False)
fs.add(111, "http://example.com/a.rss", "A", 1, {})
fs.add(111, "http://example.com/b.rss", "B", 1, {})
check("remove_all", fs.remove_all(111) == 3 and fs.list(111) == {})

check("magnet is torrent link",
      follow.is_torrent_link("magnet:?xt=urn:btih:abc"))
check(".torrent url is torrent link",
      follow.is_torrent_link("https://x.com/f.torrent"))
check("html not torrent link",
      not follow.is_torrent_link("https://x.com/page.html"))

print(f"✅ v5.6.0 follow: {len(PASS)} tests passed (total)")

# ---------------------------------------------------------------- feed parsing (mocked)
import feedparser  # noqa: E402


class _FakeFeed:
    bozo = False

    def __init__(self, entries):
        self.entries = entries


_real_parse = feedparser.parse
feedparser.parse = lambda *a, **k: _FakeFeed([  # noqa: E731
    {"id": "ep10", "title": "Show S01E10", "link": "magnet:?xt=1"},
    {"id": "", "guid": "", "title": "NoGuid", "link": ""},
    {"title": "Show S01E11", "link": "https://x.com/e11.torrent"},
])
try:
    got = follow.fetch_items("http://example.com/rss")
finally:
    feedparser.parse = _real_parse
check("parses 2 valid items", len(got) == 2)
check("guid from id", got[0]["guid"] == "ep10")
check("guid falls back to link", got[1]["guid"] == "https://x.com/e11.torrent")

class _FakeBozoFeed(_FakeFeed):
    bozo = True


feedparser.parse = lambda *a, **k: _FakeBozoFeed([])  # noqa: E731
try:
    try:
        follow.fetch_items("http://example.com/empty")
        check("bozo empty raises", False)
    except ValueError:
        check("bozo empty raises", True)
finally:
    feedparser.parse = _real_parse

print(f"✅ v5.6.0 feedparse: {len(PASS)} tests passed (total)")

# ---------------------------------------------------------------- gdrive (no creds)
import gdrive  # noqa: E402

gdrive.TOKEN_PATH = os.path.join(_tmp, "nope-token.json")
check("not configured without token", gdrive.is_configured() is False)
check("service None without token", gdrive._service() is None)
check("email None without token", gdrive.account_email() is None)
try:
    gdrive.upload_file("/tmp/x", "x")
    check("upload raises without token", False)
except RuntimeError:
    check("upload raises without token", True)

print(f"✅ v5.6.0: {len(PASS)} tests passed (total)")

"""v5.7.0 tests: in-bot series search (/tv) via TVMaze + EZTV follow.

Network-free: httpx.get is monkeypatched with canned responses.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = []


def check(name, cond):
    PASS.append(name)
    assert cond, f"FAIL: {name}"


import follow  # noqa: E402
from follow import (  # noqa: E402
    FollowStore, select_releases, _quality_rank, new_items,
    tvmaze_search, tvmaze_show, fetch_eztv_items, apibay_search, fmt_size,
)


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


SEARCH_JSON = [
    {"score": 0.9, "show": {
        "id": 50415, "name": "Lioness", "premiered": "2023-07-23",
        "status": "Running",
        "externals": {"tvrage": None, "thetvdb": 388589,
                      "imdb": "tt13111078"}}},
    {"score": 0.5, "show": {
        "id": 999, "name": "Lioness: No IMDB", "premiered": "",
        "status": "Ended", "externals": {"imdb": None}}},
    {"score": 0.1, "show": {"id": None, "name": "Ghost"}},  # no id -> dropped
]

SHOW_JSON = {"id": 50415, "name": "Lioness", "premiered": "2023-07-23",
             "externals": {"imdb": "tt13111078"}}

EZTV_JSON = {"torrents_count": 3, "torrents": [
    {"id": 1, "hash": "aaa", "filename": "Lioness.S03E08.720p.WEB.H264.mkv",
     "magnet_url": "magnet:?xt=urn:btih:aaa", "title": "Lioness S03E08 720p",
     "season": 3, "episode": 8, "date_released_unix": 1000,
     "size_bytes": 500},
    {"id": 2, "hash": "bbb", "filename": "Lioness.S03E08.1080p.WEB.H264.mkv",
     "magnet_url": "magnet:?xt=urn:btih:bbb", "title": "Lioness S03E08 1080p",
     "season": 3, "episode": 8, "date_released_unix": 1001,
     "size_bytes": 900},
    {"id": 3, "hash": "ccc", "filename": "Lioness.S03E09.1080p.WEB.H264.mkv",
     "magnet_url": "magnet:?xt=urn:btih:ccc", "title": "Lioness S03E09 1080p",
     "season": 3, "episode": 9, "date_released_unix": 1002,
     "size_bytes": 950},
    {"id": 4, "hash": "", "filename": "bad.mkv", "magnet_url": "",
     "title": "bad", "season": 3, "episode": 10},  # skipped: no magnet/hash
]}

real_get = follow.httpx.get


def fake_get(url, **kw):
    if "search/shows" in url:
        return FakeResp(SEARCH_JSON)
    if "/shows/" in url:
        return FakeResp(SHOW_JSON)
    if "eztv" in url:
        return FakeResp(EZTV_JSON)
    raise AssertionError("unexpected url " + url)


follow.httpx.get = fake_get
try:
    # tvmaze_search
    res = tvmaze_search("lioness")
    check("search returns 2 (no-id dropped)", len(res) == 2)
    check("search name", res[0]["name"] == "Lioness")
    check("search year", res[0]["year"] == "2023")
    check("search imdb", res[0]["imdb_id"] == "tt13111078")
    check("search tvmaze_id", res[0]["tvmaze_id"] == 50415)
    check("search missing imdb -> None", res[1]["imdb_id"] is None)
    check("search missing year -> ''", res[1]["year"] == "")

    # tvmaze_show
    s = tvmaze_show(50415)
    check("show name", s["name"] == "Lioness")
    check("show imdb", s["imdb_id"] == "tt13111078")

    # fetch_eztv_items
    items = fetch_eztv_items("tt13111078")
    check("eztv 3 items (bad skipped)", len(items) == 3)
    check("eztv guid prefix", items[0]["guid"] == "eztv:aaa")
    check("eztv magnet link", items[0]["link"].startswith("magnet:?"))
    check("eztv season/episode", (items[1]["season"], items[1]["episode"]) == (3, 8))
finally:
    follow.httpx.get = real_get

# select_releases: one release per episode, prefer 1080p
items = [
    {"guid": "eztv:aaa", "title": "Lioness S03E08 720p", "link": "magnet:?a",
     "season": 3, "episode": 8},
    {"guid": "eztv:bbb", "title": "Lioness S03E08 1080p", "link": "magnet:?b",
     "season": 3, "episode": 8},
    {"guid": "eztv:ccc", "title": "Lioness S03E09 480p", "link": "magnet:?c",
     "season": 3, "episode": 9},
    {"guid": "eztv:ddd", "title": "mystery release", "link": "magnet:?d",
     "season": None, "episode": None},
]
sel = select_releases(items)
guids = [i["guid"] for i in sel]
check("1080p wins for S03E08", "eztv:bbb" in guids and "eztv:aaa" not in guids)
check("S03E09 kept", "eztv:ccc" in guids)
check("ungroupable kept", "eztv:ddd" in guids)
check("3 releases total", len(sel) == 3)

check("quality rank 1080p", _quality_rank("x 1080p y") == 2)
check("quality rank 720p", _quality_rank("x 720p y") == 1)
check("quality rank 4k", _quality_rank("x 4K y") == 3)
check("quality rank other", _quality_rank("x 480p y") == 0)

# new_items dedup for eztv guids
f = {"seen": {"eztv:aaa": 3}}
fresh = new_items(f, items)
check("seen guid filtered",
      all(i["guid"] != "eztv:aaa" for i in fresh))

# FollowStore.add_eztv round-trip
import tempfile  # noqa: E402
tmp = tempfile.mkdtemp()
FollowStore.FILE = os.path.join(tmp, "follows.json")
store = FollowStore()
fid = store.add_eztv(7, "Lioness (2023)", "tt13111078", 50415, 123,
                     {"eztv:aaa": 3})
check("eztv fid prefix", fid.startswith("tv"))
fl = store.list(7)
check("eztv stored", fid in fl)
check("eztv kind", fl[fid]["kind"] == "eztv")
check("eztv imdb", fl[fid]["imdb_id"] == "tt13111078")
check("eztv no rss_url key", "rss_url" not in fl[fid])
check("eztv seen kept", fl[fid]["seen"] == {"eztv:aaa": 3})
check("remove eztv", store.remove(7, fid) is True)

# RSS add still works and is tagged rss
fid2 = store.add(7, "https://x/y.rss", "Show", 123, {})
check("rss kind", store.list(7)[fid2]["kind"] == "rss")

APIBAY_JSON = [
    {"id": "1", "name": "Dune.Part.Two.1080p.WEB-DL", "info_hash": "A" * 40,
     "seeders": "150", "leechers": "10", "size": "2147483648"},
    {"id": "2", "name": "Dune.Part.Two.720p.WEB-DL", "info_hash": "B" * 40,
     "seeders": "300", "leechers": "20", "size": "1073741824"},
    {"id": "3", "name": "No hash here", "info_hash": "0" * 40,
     "seeders": "999", "leechers": "0", "size": "1"},
    {"id": "4", "name": "Bad seeders", "info_hash": "C" * 40,
     "seeders": "n/a", "leechers": "", "size": "100"},
]


def fake_get2(url, **kw):
    if "apibay" in url:
        return FakeResp(APIBAY_JSON)
    raise AssertionError("unexpected url " + url)


follow.httpx.get = fake_get2
try:
    res = apibay_search("dune part two")
    check("apibay 3 results (zero-hash dropped)", len(res) == 3)
    check("apibay sorted by seeders desc",
          [r["seeders"] for r in res] == [300, 150, 0])
    check("apibay hash lowercase", res[0]["info_hash"] == "b" * 40)
    check("apibay name", "720p" in res[0]["name"])
    check("apibay bad seeders -> 0", res[2]["seeders"] == 0)
finally:
    follow.httpx.get = real_get

# non-list response -> []
follow.httpx.get = lambda url, **kw: FakeResp(0)
try:
    check("apibay non-list -> []", apibay_search("x") == [])
finally:
    follow.httpx.get = real_get

check("fmt_size B", fmt_size(500) == "500 B")
check("fmt_size KB", fmt_size(2048) == "2.0 KB")
check("fmt_size GB", fmt_size(2147483648) == "2.0 GB")
check("fmt_size bad", fmt_size(None) == "?")

print(f"✅ v5.7.0 tv-search: {len(PASS)} tests passed")

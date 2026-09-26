"""Series auto-follow: torrent RSS feeds / EZTV API -> new episodes auto-download.

A follow = {kind, name, chat_id, seen: {guid: attempts}} persisted in
follows.json. kind="rss" uses rss_url; kind="eztv" uses imdb_id (found via
the in-bot /tv TVMaze search). A background job polls each source; unseen
items are queued for download via the normal torrent pipeline.
"""
import hashlib
import time

import feedparser
import httpx

from store import _load, _save


class FollowStore:
    """{user_id: {feed_id: {name, rss_url, chat_id, seen: {guid: attempts}, added}}}"""

    FILE = "follows.json"

    def _data(self):
        return _load(self.FILE, {})

    def add(self, user_id: int, rss_url: str, name: str, chat_id: int,
            seen: dict) -> str:
        d = self._data()
        u = d.setdefault(str(user_id), {})
        fid = hashlib.sha1(rss_url.encode()).hexdigest()[:10]
        u[fid] = {
            "kind": "rss",
            "name": name or rss_url[:40],
            "rss_url": rss_url,
            "chat_id": chat_id,
            "seen": dict(seen),
            "added": int(time.time()),
        }
        _save(self.FILE, d)
        return fid

    def add_eztv(self, user_id: int, name: str, imdb_id: str, tvmaze_id: int,
                 chat_id: int, seen: dict) -> str:
        """Follow a series via the EZTV API (found with in-bot /tv search)."""
        d = self._data()
        u = d.setdefault(str(user_id), {})
        fid = "tv" + hashlib.sha1(imdb_id.encode()).hexdigest()[:8]
        u[fid] = {
            "kind": "eztv",
            "name": name,
            "imdb_id": imdb_id,
            "tvmaze_id": tvmaze_id,
            "chat_id": chat_id,
            "seen": dict(seen),
            "added": int(time.time()),
        }
        _save(self.FILE, d)
        return fid

    def remove(self, user_id: int, fid: str) -> bool:
        d = self._data()
        u = d.get(str(user_id), {})
        if fid in u:
            del u[fid]
            _save(self.FILE, d)
            return True
        return False

    def remove_all(self, user_id: int) -> int:
        d = self._data()
        n = len(d.get(str(user_id), {}))
        d[str(user_id)] = {}
        _save(self.FILE, d)
        return n

    def list(self, user_id: int):
        return self._data().get(str(user_id), {})

    def all(self):
        out = {}
        for u, feeds in self._data().items():
            out[int(u)] = feeds
        return out

    def mark_seen(self, user_id: int, fid: str, guid: str) -> None:
        d = self._data()
        try:
            d[str(user_id)][fid]["seen"][guid] = 3  # done, no more retries
            _save(self.FILE, d)
        except KeyError:
            pass

    def bump_attempt(self, user_id: int, fid: str, guid: str) -> int:
        """Record a failed attempt; returns new attempt count."""
        d = self._data()
        try:
            seen = d[str(user_id)][fid]["seen"]
            seen[guid] = int(seen.get(guid, 0)) + 1
            _save(self.FILE, d)
            return seen[guid]
        except KeyError:
            return 99


MAX_ATTEMPTS = 3  # transient failures (no seeders yet) retry this many polls


def fetch_items(rss_url: str, timeout: int = 30) -> list:
    """Parse an RSS/Atom feed -> [{guid, title, link}]. Raises on failure."""
    parsed = feedparser.parse(rss_url,
                              request_headers={"User-Agent": "tg-video-bot/1.0"})
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"feed ဖတ်မရပါ: {rss_url}")
    items = []
    for e in parsed.entries:
        guid = (e.get("id") or e.get("guid") or e.get("link") or "").strip()
        link = (e.get("link") or "").strip()
        title = (e.get("title") or guid).strip()
        if guid and link:
            items.append({"guid": guid, "title": title, "link": link})
    return items


def new_items(follow: dict, items: list) -> list:
    """Items not seen yet (or still within retry budget), oldest first."""
    seen = follow.get("seen", {})
    fresh = [it for it in items
             if int(seen.get(it["guid"], 0)) < MAX_ATTEMPTS]
    return fresh


def is_torrent_link(link: str) -> bool:
    l = link.lower()
    return l.startswith("magnet:?") or ".torrent" in l.split("?")[0]


EZTV_API = "https://eztv.re/api/get-torrents"
TVMAZE_API = "https://api.tvmaze.com"
APIBAY_API = "https://apibay.org/q.php"


def fmt_size(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def apibay_search(query: str, timeout: int = 25, limit: int = 60) -> list:
    """TPB index search -> [{info_hash, name, size, seeders, leechers}],
    sorted by seeders desc. Raises on failure."""
    r = httpx.get(APIBAY_API, params={"q": query, "cat": "0"},
                  timeout=timeout,
                  headers={"User-Agent": "tg-video-bot/1.0"})
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    out = []
    for t in data[:limit]:
        h = (t.get("info_hash") or "").strip().lower()
        if len(h) != 40 or set(h) == {"0"}:
            continue
        try:
            seeders = int(t.get("seeders") or 0)
        except (TypeError, ValueError):
            seeders = 0
        try:
            leechers = int(t.get("leechers") or 0)
        except (TypeError, ValueError):
            leechers = 0
        out.append({
            "info_hash": h,
            "name": (t.get("name") or h).strip(),
            "size": t.get("size") or 0,
            "seeders": seeders,
            "leechers": leechers,
        })
    out.sort(key=lambda x: -x["seeders"])
    return out


def tvmaze_search(query: str, timeout: int = 20) -> list:
    """Search TVMaze -> [{tvmaze_id, name, year, imdb_id}]. Raises on failure."""
    r = httpx.get(TVMAZE_API + "/search/shows", params={"q": query},
                  timeout=timeout,
                  headers={"User-Agent": "tg-video-bot/1.0"})
    r.raise_for_status()
    out = []
    for hit in r.json():
        s = hit.get("show") or {}
        ext = s.get("externals") or {}
        out.append({
            "tvmaze_id": s.get("id"),
            "name": s.get("name") or "?",
            "year": (s.get("premiered") or "")[:4],
            "imdb_id": ext.get("imdb"),
            "status": s.get("status") or "",
        })
    return [o for o in out if o["tvmaze_id"]]


def tvmaze_show(tvmaze_id: int, timeout: int = 20) -> dict:
    """Fetch one TVMaze show -> {tvmaze_id, name, year, imdb_id}."""
    r = httpx.get(f"{TVMAZE_API}/shows/{tvmaze_id}", timeout=timeout,
                  headers={"User-Agent": "tg-video-bot/1.0"})
    r.raise_for_status()
    s = r.json()
    ext = s.get("externals") or {}
    return {
        "tvmaze_id": s.get("id"),
        "name": s.get("name") or "?",
        "year": (s.get("premiered") or "")[:4],
        "imdb_id": ext.get("imdb"),
    }


def fetch_eztv_items(imdb_id: str, timeout: int = 30, limit: int = 100) -> list:
    """EZTV API -> [{guid, title, link, season, episode}]. Raises on failure."""
    r = httpx.get(EZTV_API, params={"imdb_id": imdb_id, "limit": limit},
                  timeout=timeout, follow_redirects=True,
                  headers={"User-Agent": "tg-video-bot/1.0"})
    r.raise_for_status()
    items = []
    for t in r.json().get("torrents", []):
        magnet = (t.get("magnet_url") or "").strip()
        h = (t.get("hash") or "").strip()
        if not magnet or not h:
            continue
        items.append({
            "guid": "eztv:" + h,
            "title": (t.get("title") or t.get("filename") or h).strip(),
            "link": magnet,
            "season": t.get("season"),
            "episode": t.get("episode"),
        })
    return items


def _quality_rank(title: str) -> int:
    t = title.lower()
    if "2160p" in t or "4k" in t:
        return 3
    if "1080p" in t:
        return 2
    if "720p" in t:
        return 1
    return 0


def select_releases(items: list) -> list:
    """One release per (season, episode) — prefer 1080p, then 720p."""
    best = {}
    order = []
    for it in items:
        key = (it.get("season"), it.get("episode"))
        if None in key:
            order.append(it)  # can't group — keep as-is
            continue
        rank = _quality_rank(it.get("title", ""))
        if key not in best or rank > best[key][0]:
            best[key] = (rank, it)
    for _, it in best.values():
        order.append(it)
    return order

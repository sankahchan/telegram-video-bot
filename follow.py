"""Series auto-follow: torrent RSS feeds -> new episodes auto-download.

A follow = {rss_url, name, chat_id, seen: {guid: attempts}} persisted in
follows.json. A background job polls each feed; unseen items are queued for
download via the normal torrent pipeline.
"""
import hashlib
import time

import feedparser

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
            "name": name or rss_url[:40],
            "rss_url": rss_url,
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

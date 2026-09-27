"""Dead-magnet watchlist.

A magnet with no live seeders can be watched: every poll the bot re-checks
tracker health, and the moment seeders appear the owner gets a notification
with a one-tap download button. Entries persist in deadwatch.json.
"""
import time

from store import _load, _save

MAX_WATCH = 20  # per user


class DeadWatchStore:
    FILE = "deadwatch.json"

    def _data(self):
        return _load(self.FILE, {})

    def _write(self, d):
        _save(self.FILE, d)

    def add(self, uid: int, chat_id: int, magnet: str, infohash: str,
            name: str) -> str | None:
        """Add a magnet to the watchlist. Returns the watch token, or None
        when the user already watches this infohash / hit the cap."""
        d = self._data()
        u = d.setdefault(str(uid), {})
        for tok, e in u.items():
            if e.get("infohash") == infohash:
                return tok  # already watching
        if len(u) >= MAX_WATCH:
            return None
        token = f"{int(time.time()):x}{len(u):02x}"
        u[token] = {
            "magnet": magnet, "infohash": infohash, "name": name,
            "chat_id": chat_id, "added": int(time.time()),
            "last_check": 0, "last_seeders": 0,
        }
        self._write(d)
        return token

    def remove(self, uid: int, token: str) -> bool:
        d = self._data()
        u = d.get(str(uid), {})
        if token in u:
            del u[token]
            self._write(d)
            return True
        return False

    def list_for(self, uid: int) -> dict:
        return self._data().get(str(uid), {})

    def all(self) -> dict:
        return self._data()

    def touch(self, uid: int, token: str, seeders: int) -> None:
        d = self._data()
        e = d.get(str(uid), {}).get(token)
        if e is not None:
            e["last_check"] = int(time.time())
            e["last_seeders"] = seeders
            self._write(d)

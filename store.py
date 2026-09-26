"""JSON-backed persistent stores: stats, users, watchlist, night queue, settings.

All files live next to this script (DATA_DIR). Writes are atomic
(write temp + rename) so a crash never corrupts the file.
"""
import json
import os
import time

DATA_DIR = os.path.dirname(os.path.abspath(__file__))


def _path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def _load(name: str, default):
    p = _path(name)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def _save(name: str, data) -> None:
    p = _path(name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ---------------------------------------------------------------- stats
class StatsStore:
    """Download log: [{ts, mb, kind, user}]"""

    FILE = "stats.json"

    def log(self, mb: float, kind: str, user_id: int,
            url: str = None, ckey: str = None) -> None:
        items = _load(self.FILE, [])
        items.append({"ts": time.time(), "mb": round(mb, 2), "kind": kind,
                      "user": user_id, "url": url, "ckey": ckey})
        # keep last 5000 entries
        _save(self.FILE, items[-5000:])

    def usage(self, user_id: int, days: int = 30) -> float:
        """MB downloaded by user in the last `days` days (rolling)."""
        cutoff = time.time() - days * 86400
        total = 0.0
        for it in _load(self.FILE, []):
            if it.get("user") == user_id and it.get("ts", 0) >= cutoff:
                total += it.get("mb", 0) or 0
        return round(total, 1)

    def recent(self, user_id: int, n: int = 10) -> list:
        """Newest-first download history entries for a user."""
        items = [it for it in _load(self.FILE, [])
                 if it.get("user") == user_id]
        return items[-n:][::-1]

    def summary(self):
        now = time.time()
        day_ago = now - 86400
        month_ago = now - 86400 * 30
        d_n = d_mb = m_n = m_mb = a_n = a_mb = 0
        for it in _load(self.FILE, []):
            ts, mb = it.get("ts", 0), it.get("mb", 0) or 0
            a_n += 1
            a_mb += mb
            if ts >= month_ago:
                m_n += 1
                m_mb += mb
            if ts >= day_ago:
                d_n += 1
                d_mb += mb
        return {
            "day": (d_n, round(d_mb, 1)),
            "month": (m_n, round(m_mb, 1)),
            "all": (a_n, round(a_mb, 1)),
        }


# ---------------------------------------------------------------- users
class UserStore:
    """Extra allowed user IDs with optional expiry (subscription).

    users.json: {"allowed": [ids],
                 "meta": {uid_str: {"added": ts, "expires": ts|null,
                                    "name": str, "warned3": bool,
                                    "warned_exp": bool, "quota_mb": float|null}}}
    expires=None -> unlimited. 1 month = 30 days.
    quota_mb=None -> unlimited monthly download quota (rolling 30 days).
    """

    FILE = "users.json"
    MONTH_SEC = 30 * 86400

    def _data(self):
        d = _load(self.FILE, {})
        d.setdefault("allowed", [])
        d.setdefault("meta", {})
        now = int(time.time())
        for uid in d["allowed"]:
            d["meta"].setdefault(str(uid), {
                "added": now, "expires": None, "name": "",
                "warned3": False, "warned_exp": False, "quota_mb": None,
            })
        return d

    def _save_data(self, d):
        _save(self.FILE, d)

    def allowed_ids(self):
        return set(self._data()["allowed"])

    def add(self, uid: int, months: float = None, name: str = "") -> bool:
        """Add user; months=None -> unlimited. Returns True if new."""
        d = self._data()
        is_new = uid not in d["allowed"]
        if is_new:
            d["allowed"].append(uid)
        m = d["meta"].setdefault(str(uid), {
            "added": int(time.time()), "expires": None, "name": "",
            "warned3": False, "warned_exp": False,
        })
        if name:
            m["name"] = name
        if months is not None:
            m["expires"] = int(time.time() + months * self.MONTH_SEC)
            m["warned3"] = m["warned_exp"] = False
        self._save_data(d)
        return is_new

    def remove(self, uid: int) -> bool:
        d = self._data()
        if uid not in d["allowed"]:
            return False
        d["allowed"].remove(uid)
        d["meta"].pop(str(uid), None)
        self._save_data(d)
        return True

    def meta(self, uid: int) -> dict:
        return self._data()["meta"].get(str(uid), {})

    def expiry(self, uid: int):
        """Expiry timestamp, or None for unlimited / not in list."""
        return self.meta(uid).get("expires")

    def is_expired(self, uid: int) -> bool:
        exp = self.expiry(uid)
        return bool(exp) and time.time() >= exp

    def days_left(self, uid: int):
        """Days remaining; None = unlimited; negative = expired."""
        exp = self.expiry(uid)
        if not exp:
            return None
        return (exp - time.time()) / 86400

    def extend(self, uid: int, months: float):
        """Add months from max(now, current expiry). Returns new exp ts,
        or None if uid not in list."""
        d = self._data()
        if uid not in d["allowed"]:
            return None
        m = d["meta"][str(uid)]
        base = max(int(time.time()), m.get("expires") or 0)
        m["expires"] = int(base + months * self.MONTH_SEC)
        m["warned3"] = m["warned_exp"] = False
        self._save_data(d)
        return m["expires"]

    def set_flag(self, uid: int, key: str, val: bool = True) -> None:
        d = self._data()
        if str(uid) in d["meta"]:
            d["meta"][str(uid)][key] = val
            self._save_data(d)

    def all_users(self) -> list:
        """[{"id", "added", "expires", "name"}] sorted by id."""
        d = self._data()
        out = []
        for uid in sorted(d["allowed"]):
            m = d["meta"].get(str(uid), {})
            out.append({"id": uid, "added": m.get("added"),
                        "expires": m.get("expires"),
                        "name": m.get("name", "")})
        return out

    def set_quota(self, uid: int, mb: float | None) -> bool:
        """Monthly download quota in MB (None = unlimited)."""
        d = self._data()
        if uid not in d["allowed"]:
            return False
        d["meta"][str(uid)]["quota_mb"] = mb
        self._save_data(d)
        return True

    def quota_mb(self, uid: int):
        """Quota in MB, or None for unlimited / not in list."""
        return self.meta(uid).get("quota_mb")


# -------------------------------------------------------------- bookmarks
class BookmarkStore:
    """Saved links per user: {uid_str: [{url, title, ts}]} (max 50/user)."""

    FILE = "bookmarks.json"
    MAX_PER_USER = 50

    def _data(self):
        return _load(self.FILE, {})

    def add(self, uid: int, url: str, title: str = "") -> bool:
        d = self._data()
        items = d.setdefault(str(uid), [])
        if any(b["url"] == url for b in items):
            return False
        items.append({"url": url, "title": title or url[:60],
                      "ts": int(time.time())})
        d[str(uid)] = items[-self.MAX_PER_USER:]
        _save(self.FILE, d)
        return True

    def list(self, uid: int) -> list:
        return self._data().get(str(uid), [])

    def remove(self, uid: int, idx: int) -> bool:
        d = self._data()
        items = d.get(str(uid), [])
        if 0 <= idx < len(items):
            items.pop(idx)
            d[str(uid)] = items
            _save(self.FILE, d)
            return True
        return False


# -------------------------------------------------------------- watchlist
class WatchStore:
    """Auto-download watches: {user_id: {chat_id: {title, last_id}}}"""

    FILE = "watchlist.json"

    def _data(self):
        return _load(self.FILE, {})

    def add(self, user_id: int, chat_id: int, title: str, last_id: int) -> None:
        d = self._data()
        d.setdefault(str(user_id), {})[str(chat_id)] = {
            "title": title,
            "last_id": last_id,
        }
        _save(self.FILE, d)

    def remove(self, user_id: int, chat_id: int) -> bool:
        d = self._data()
        u = d.get(str(user_id), {})
        if str(chat_id) in u:
            del u[str(chat_id)]
            _save(self.FILE, d)
            return True
        return False

    def list(self, user_id: int):
        return self._data().get(str(user_id), {})

    def all(self):
        """{user_id: {chat_id: {...}}} with int keys."""
        out = {}
        for u, chats in self._data().items():
            out[int(u)] = {int(c): v for c, v in chats.items()}
        return out

    def set_last(self, user_id: int, chat_id: int, last_id: int) -> None:
        d = self._data()
        try:
            d[str(user_id)][str(chat_id)]["last_id"] = last_id
            _save(self.FILE, d)
        except KeyError:
            pass


# ------------------------------------------------------------ night queue
class QueueStore:
    """Queued downloads for night mode: [{kind, ref, user_id, chat_id, ts, label}]"""

    FILE = "night_queue.json"

    def add(self, item: dict) -> None:
        q = _load(self.FILE, [])
        q.append(item)
        _save(self.FILE, q)

    def all(self):
        return _load(self.FILE, [])

    def clear_for_user(self, user_id: int) -> None:
        _save(self.FILE, [i for i in _load(self.FILE, []) if i.get("user_id") != user_id])

    def replace(self, items) -> None:
        _save(self.FILE, items)


# --------------------------------------------------------------- settings
DEFAULTS = {
    "mode": "video",      # video | file
    "quality": "high",    # high | low
    "mp3": False,         # extract audio
    "zip": False,         # zip batch into one file
    "night": False,       # queue big files for night
    "night_hour": 3,      # KST hour (0-23) to process queue
    "save": False,        # also save copy to Saved Messages
    "convert": "ask",     # MKV torrent: ask|always|never convert to MP4/AAC
}


class SettingsStore:
    """Per-user settings, persisted."""

    FILE = "settings.json"

    def _data(self):
        return _load(self.FILE, {})

    def get(self, user_id: int):
        d = self._data().get(str(user_id), {})
        merged = dict(DEFAULTS)
        merged.update(d)
        return merged

    def set(self, user_id: int, key: str, value) -> None:
        all_d = self._data()
        u = all_d.setdefault(str(user_id), {})
        u[key] = value
        _save(self.FILE, all_d)

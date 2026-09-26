"""URL -> Telegram file_id cache (instant repeat delivery).

A link that was already downloaded+sent is re-sent via its Telegram file_id
— no re-download, no VPS bandwidth. Entries expire after 30 days.
"""
import hashlib
import json
import os
import time

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(DATA_DIR, "fileid_cache.json")
TTL_SECONDS = 30 * 86400  # 30 days

# Bump when the download/process pipeline changes its output (e.g. v5.4.6
# H.264 normalization, v5.7.2 iOS remux) so stale file_ids are never
# re-served: a repeat link re-downloads once instead of replaying the old file.
CACHE_VERSION = "v572"


def make_key(*parts) -> str:
    """Cache key = sha1 of version + normalized parts."""
    norm = "|".join(str(p).strip().lower() for p in parts)
    return hashlib.sha1(
        f"{CACHE_VERSION}|{norm}".encode("utf-8")).hexdigest()


class FileIdCache:
    def __init__(self, path: str = CACHE_FILE, ttl: int = TTL_SECONDS):
        self.path = path
        self.ttl = ttl
        self.data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except (FileNotFoundError, ValueError):
            return {}
        now = time.time()
        pruned = {k: v for k, v in d.items()
                  if now - v.get("ts", 0) < self.ttl}
        if len(pruned) != len(d):
            self.data = pruned
            try:
                self._save()
            except Exception:
                pass
        return pruned

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)  # atomic

    def get(self, key: str):
        return self.data.get(key)

    def set(self, key: str, file_id: str, kind: str, caption: str = "") -> None:
        self.data[key] = {"file_id": file_id, "kind": kind,
                          "caption": caption or "", "ts": time.time()}
        try:
            self._save()
        except Exception as e:
            print(f"⚠️ file_id cache save failed: {e}")

    def clear(self) -> int:
        n = len(self.data)
        self.data = {}
        try:
            self._save()
        except Exception:
            pass
        return n

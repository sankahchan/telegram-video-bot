"""Torrent downloads via aria2c (magnet links + .torrent files).

Flow:
  1. magnet -> fetch metadata only (--bt-metadata-only) -> .torrent file
     .torrent file -> use directly
  2. list files (aria2c --show-files), pick the largest video file
     (else the largest file)
  3. selective download (--select-file) with seeding disabled

Safety:
  - seeding OFF (--seed-time=0) + tiny upload cap: the VPS only leeches
  - selected file must fit Telegram's ~2GB bot limit (else refused)
  - stall detection aborts hung downloads

Needs the `aria2` system package (install.sh / update.sh install it).
"""
import os
import re
import shutil
import subprocess
import time

MAX_TORRENT_FILE_MB = 1900  # Telegram MTProto send cap is ~2GB
_METADATA_TIMEOUT = 180     # s to fetch magnet metadata (DHT)
_DOWNLOAD_TIMEOUT = 3600    # s max per torrent
_STALL_TIMEOUT = 600        # s without progress -> abort
_POLL_S = 5

_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts",
               ".m4v", ".3gp", ".mpg", ".mpeg"}


class TorrentError(Exception):
    pass


def is_magnet(text: str) -> bool:
    return (text or "").strip().lower().startswith("magnet:?")


def extract_magnets(text: str) -> list:
    return re.findall(r"magnet:\?[^\s<>\"']+", text or "", re.IGNORECASE)


def have_aria2() -> bool:
    return shutil.which("aria2c") is not None


def _run(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def fetch_magnet_metadata(magnet: str, tmpdir: str,
                         timeout: int = _METADATA_TIMEOUT) -> str:
    """Fetch only the .torrent metadata for a magnet. Returns .torrent path."""
    if not have_aria2():
        raise TorrentError(
            "aria2c မရှိသေးပါ — VPS မှာ update.sh run ပေးပါ:\n"
            "bash /opt/tg-video-bot/update.sh")
    cmd = ["aria2c", "--bt-metadata-only=true", "--bt-save-metadata=true",
           "--dir", tmpdir, "--allow-overwrite=true",
           "--max-upload-limit=50K", magnet]
    try:
        proc = _run(cmd, timeout)
    except subprocess.TimeoutExpired:
        raise TorrentError(
            "🧲 magnet metadata ရယူတာ timeout ဖြစ်သွားပါတယ် — seeders "
            "မရှိတာ (သို့) DHT unreachable ဖြစ်နိုင်ပါတယ်.\n"
            "Magnet metadata fetch timed out — likely no seeders or DHT "
            "is unreachable from the VPS.")
    torrents = [f for f in os.listdir(tmpdir) if f.endswith(".torrent")]
    if proc.returncode != 0 or not torrents:
        err = (proc.stderr or proc.stdout or "")[-300:]
        raise TorrentError(
            "🧲 magnet metadata ရယူမရပါ — seeders မရှိတာ (သို့) DHT "
            f"unreachable ဖြစ်နိုင်ပါတယ်.{' (' + err.strip() + ')' if err.strip() else ''}\n"
            "Could not fetch magnet metadata — likely no seeders or DHT "
            "unreachable from the VPS.")
    torrents.sort(key=lambda f: os.path.getmtime(os.path.join(tmpdir, f)),
                  reverse=True)
    return os.path.join(tmpdir, torrents[0])


def torrent_files(torrent_path: str) -> list:
    """Parse `aria2c --show-files` -> [{index, path, size}]."""
    proc = _run(["aria2c", "--show-files=true", torrent_path], 60)
    out = proc.stdout or ""
    files = []
    # aria2c --show-files format:
    #   idx|/path/to/file
    #    | 1.2GiB (1,234,567)
    cur = None
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\|(.+)", line)
        if m:
            if cur:
                files.append(cur)
            cur = {"index": m.group(1), "path": m.group(2).strip(),
                   "size": 0}
            continue
        m = re.match(r"\s*\|\s*[\d.]+\s*(?:KiB|MiB|GiB|TiB|B)\s*\(([\d,]+)\)",
                     line)
        if m and cur:
            cur["size"] = int(m.group(1).replace(",", ""))
    if cur:
        files.append(cur)
    if not files:
        raise TorrentError("torrent ထဲက file list ကို ဖတ်မရပါ.")
    return files


def pick_target(files: list) -> dict:
    """Largest video file; else the largest file."""
    if not files:
        raise TorrentError("torrent ထဲမှာ file မရှိပါ.")
    videos = [f for f in files
              if os.path.splitext(f["path"])[1].lower() in _VIDEO_EXTS]
    pool = videos or files
    return max(pool, key=lambda f: f["size"])


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, filenames in os.walk(path):
        for fn in filenames:
            if fn.endswith(".torrent"):
                continue
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def download_torrent(source: str, tmpdir: str, file_index: str,
                     expected_total: int, progress_cb=None,
                     timeout: int = _DOWNLOAD_TIMEOUT) -> str:
    """Selectively download one file from a torrent/magnet. Returns file path.

    progress_cb: sync fn(done_bytes, total_bytes).
    """
    dl_dir = os.path.join(tmpdir, "tdata")
    os.makedirs(dl_dir, exist_ok=True)
    cmd = ["aria2c", "--select-file", str(file_index),
           "--seed-time=0", "--max-upload-limit=50K",
           "--allow-overwrite=true", "--dir", dl_dir,
           "--console-log-level=warn", "--summary-interval=0",
           source]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    start = last_active = time.time()
    last_done = 0
    try:
        while True:
            rc = proc.poll()
            done = _dir_size(dl_dir)
            if done != last_done:
                last_done = done
                last_active = time.time()
                if progress_cb:
                    try:
                        progress_cb(done, expected_total)
                    except Exception:
                        pass
            if rc is not None:
                if rc != 0:
                    raise TorrentError(
                        f"❌ torrent download မအောင်မြင်ပါ (aria2c exit {rc}) — "
                        "seeders မရှိတာ ဖြစ်နိုင်ပါတယ်.\n"
                        f"Torrent download failed (aria2c exit {rc}) — "
                        "likely no seeders.")
                break
            if time.time() - last_active > _STALL_TIMEOUT:
                raise TorrentError(
                    "❌ torrent ရပ်နေပါတယ် (၁၀ မိနစ် progress မရှိ) — "
                    "seeders မရှိတာ ဖြစ်နိုင်ပါတယ်, နောက်မှ ပြန်စမ်းပါ.\n"
                    "Torrent stalled (no progress for 10 min) — likely no "
                    "seeders, try again later.")
            if time.time() - start > timeout:
                raise TorrentError(
                    "❌ torrent download ကြာလွန်းလို့ ရပ်လိုက်ပါတယ် "
                    "(၁ နာရီ ကျော်).\nTorrent download took too long (>1h), aborted.")
            time.sleep(_POLL_S)
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
    # locate the downloaded file (largest non-.torrent file wins)
    best = (None, -1)
    for root, _dirs, filenames in os.walk(dl_dir):
        for fn in filenames:
            if fn.endswith(".torrent"):
                continue
            p = os.path.join(root, fn)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if sz > best[1]:
                best = (p, sz)
    if not best[0] or best[1] <= 0:
        raise TorrentError("❌ download ပြီးပေမယ့် file မတွေ့ပါ.")
    return best[0]


def check_torrent_size(size_bytes: int):
    """Refuse files that can't be sent through Telegram (~2GB cap)."""
    if size_bytes > MAX_TORRENT_FILE_MB * 1048576:
        gb = size_bytes / 1073741824
        raise TorrentError(
            f"❌ ဒီ file က ကြီးလွန်းပါတယ် ({gb:.1f}GB) — Telegram ကနေ "
            f"ပို့လို့ရတာ အများဆုံး ~2GB ပဲ ဖြစ်ပါတယ်.\n"
            f"This file is too big ({gb:.1f}GB) — Telegram can only send "
            "up to ~2GB.")

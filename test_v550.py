"""v5.5.0 mock tests — no network. Run: python3 test_v550.py"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import torrent_download as td
from torrent_download import (
    is_magnet, extract_magnets, have_aria2,
    torrent_files, pick_target, check_torrent_size,
    download_torrent, TorrentError,
)

PASS = []


def check(name, cond):
    assert cond, f"FAIL: {name}"
    PASS.append(name)


# --- magnet detection ------------------------------------------------------------
check("magnet detected",
      is_magnet("magnet:?xt=urn:btih:abc123&dn=test"))
check("magnet case-insensitive",
      is_magnet("MAGNET:?xt=urn:btih:abc123"))
check("non-magnet rejected", not is_magnet("https://example.com/x.torrent"))
check("empty safe", not is_magnet(""))
check("none safe", not is_magnet(None))

mags = extract_magnets("see magnet:?xt=urn:btih:abc123 and "
                       "magnet:?xt=urn:btih:def456 end")
check("extract 2 magnets", len(mags) == 2)
check("magnet[0]", mags[0] == "magnet:?xt=urn:btih:abc123")
check("no magnets", extract_magnets("https://example.com") == [])

# --- aria2 present (sandbox installed it) -------------------------------------------
check("aria2c available", have_aria2())

# --- --show-files parsing (fixture from real aria2c output) ---------------------------
FIXTURE = """\
>>> Printing the contents of file 'x.torrent'...
Files:
idx|path/length
===+===========================================================================
  1|./movie-pack/sample.mkv
   |1.4GiB (1,503,238,400)
  2|./movie-pack/poster.jpg
   |120.5KiB (123,392)
  3|./movie-pack/subs/en.srt
   |32B (32)
---+---------------------------------------------------------------------------
"""


class _FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


_real_run = subprocess.run
subprocess.run = lambda *a, **k: _FakeProc(FIXTURE)  # noqa: E731
try:
    files = torrent_files("/tmp/fake.torrent")
finally:
    subprocess.run = _real_run

check("3 files parsed", len(files) == 3)
check("index", files[0]["index"] == "1")
check("path", files[0]["path"] == "./movie-pack/sample.mkv")
check("size", files[0]["size"] == 1503238400)
check("small size", files[2]["size"] == 32)

# unparseable output -> TorrentError
subprocess.run = lambda *a, **k: _FakeProc("garbage")  # noqa: E731
try:
    try:
        torrent_files("/tmp/fake.torrent")
        check("garbage raises", False)
    except TorrentError:
        check("garbage raises", True)
finally:
    subprocess.run = _real_run

# --- target picking ---------------------------------------------------------------------
check("picks largest video",
      pick_target(files)["path"] == "./movie-pack/sample.mkv")
non_video = [f for f in files if not f["path"].endswith(".mkv")]
check("no video -> largest file",
      pick_target(non_video)["path"] == "./movie-pack/poster.jpg")
try:
    pick_target([])
    check("empty raises", False)
except TorrentError:
    check("empty raises", True)

# --- size cap -----------------------------------------------------------------------------
check_torrent_size(1900 * 1048576 - 1)  # just under -> ok
check("under cap ok", True)
try:
    check_torrent_size(5 * 1073741824)
    check("over cap raises", False)
except TorrentError as e:
    check("over cap raises", "2GB" in str(e))

# --- download_torrent with mocked Popen ---------------------------------------------------
_real_popen = subprocess.Popen


class _FakePopen:
    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        # find --dir value; simulate aria2c writing the selected file
        d = cmd[cmd.index("--dir") + 1]
        os.makedirs(os.path.join(d, "data"), exist_ok=True)
        with open(os.path.join(d, "data", "sample.mkv"), "wb") as f:
            f.write(b"v" * 2048)
        self._rc = None

    def poll(self):
        self._rc = 0
        return 0

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


subprocess.Popen = _FakePopen  # noqa: E731
try:
    tmp = tempfile.mkdtemp()
    calls = []
    path = download_torrent("/tmp/fake.torrent", tmp, "1", 2048,
                            progress_cb=lambda d, t: calls.append((d, t)))
    check("returns downloaded file", path.endswith("sample.mkv"))
    check("file exists", os.path.exists(path))
    check("progress called", len(calls) >= 1 and calls[-1][0] == 2048)
finally:
    subprocess.Popen = _real_popen


# aria2c non-zero exit -> TorrentError
class _FailPopen(_FakePopen):
    def poll(self):
        return 3


subprocess.Popen = _FailPopen  # noqa: E731
try:
    try:
        download_torrent("/tmp/fake.torrent", tempfile.mkdtemp(),
                         "1", 100, None)
        check("nonzero exit raises", False)
    except TorrentError as e:
        check("nonzero exit raises", "exit 3" in str(e))
finally:
    subprocess.Popen = _real_popen

# no file produced -> TorrentError
class _EmptyPopen:
    def __init__(self, cmd, **kwargs):
        d = cmd[cmd.index("--dir") + 1]
        os.makedirs(d, exist_ok=True)

    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


subprocess.Popen = _EmptyPopen  # noqa: E731
try:
    try:
        download_torrent("/tmp/fake.torrent", tempfile.mkdtemp(),
                         "1", 100, None)
        check("empty raises", False)
    except TorrentError:
        check("empty raises", True)
finally:
    subprocess.Popen = _real_popen

# --- aria2c argv sanity ---------------------------------------------------------------------
seen = {}


class _ArgPopen(_FakePopen):
    def __init__(self, cmd, **kwargs):
        seen["cmd"] = cmd
        super().__init__(cmd, **kwargs)


subprocess.Popen = _ArgPopen  # noqa: E731
try:
    download_torrent("magnet:?xt=urn:btih:abc", tempfile.mkdtemp(),
                     "2", 100, None)
finally:
    subprocess.Popen = _real_popen
cmd = seen["cmd"]
check("select-file passed", "--select-file" in cmd and "2" in cmd)
check("seeding disabled", "--seed-time=0" in cmd)
check("upload capped", "--max-upload-limit=50K" in cmd)

print(f"✅ v5.5.0: {len(PASS)} tests passed")

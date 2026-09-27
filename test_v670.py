"""v6.7.0 — torrent pack: deadwatch, min-seeders, /dl dashboard+cancel,
smart pick, /totorrent, blacklist.

DeadWatchStore tests use a temp DATA_DIR so the real deadwatch.json is
never touched.
"""
import ast
import inspect
import os
import re
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

# isolate JSON stores in a temp dir BEFORE importing bot-side modules
_tmp = tempfile.mkdtemp(prefix="v670_")
import store
store.DATA_DIR = _tmp

from deadwatch import DeadWatchStore, MAX_WATCH
from torrent_download import (TorrentError, DownloadCancelled,
                              download_torrent)

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)


# --- DeadWatchStore ----------------------------------------------------------
dw = DeadWatchStore()
check("dw: empty list", dw.list_for(111) == {})
t1 = dw.add(111, 222, "magnet:?xt=urn:btih:" + "a" * 40, "a" * 40, "a" * 12)
check("dw: add returns token", isinstance(t1, str) and t1)
t2 = dw.add(111, 222, "magnet:?xt=urn:btih:" + "a" * 40, "a" * 40, "a" * 12)
check("dw: duplicate infohash returns same token", t2 == t1)
check("dw: list_for has 1", len(dw.list_for(111)) == 1)
dw.touch(111, t1, 3)
check("dw: touch updates seeders",
      dw.list_for(111)[t1]["last_seeders"] == 3
      and dw.list_for(111)[t1]["last_check"] > 0)
check("dw: remove True", dw.remove(111, t1) is True)
check("dw: remove again False", dw.remove(111, t1) is False)
check("dw: all() shape", isinstance(dw.all(), dict))
# cap
for i in range(MAX_WATCH + 3):
    ih = f"{i:040x}"
    dw.add(222, 333, f"magnet:?xt=urn:btih:{ih}", ih, ih[:12])
check("dw: cap enforced", len(dw.list_for(222)) == MAX_WATCH)
# persistence across instances
dw2 = DeadWatchStore()
check("dw: persists", len(dw2.list_for(222)) == MAX_WATCH)

# --- cancel plumbing ----------------------------------------------------------
check("cancel: DownloadCancelled is TorrentError",
      issubclass(DownloadCancelled, TorrentError))
sig = inspect.signature(download_torrent)
check("cancel: download_torrent has cancel_event",
      "cancel_event" in sig.parameters)

# --- settings defaults ---------------------------------------------------------
d = store.DEFAULTS
check("settings: min_seeders default 0", d.get("min_seeders") == 0)
check("settings: blacklist default",
      d.get("blacklist") == ["cam", "ts", "hdcam", "hdts", "telesync"])

# --- bot.py wiring (ast + regex, no telegram import needed) --------------------
src = open("bot.py", encoding="utf-8").read()
tree = ast.parse(src)
names = {n.name for n in ast.walk(tree)
         if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))}
for fn in ["dl_cmd", "dlx_pick", "sbest_pick", "totorrent_cmd",
           "setminseeders_cmd", "blacklist_cmd", "watchdead_cmd",
           "deadwatch_cmd", "unwatchdead_cmd", "dwdel_pick",
           "deadwatch_job", "_follow_skip_reason", "_title_blacklisted",
           "_smart_score", "_dl_bar"]:
    check(f"hook: {fn} defined", fn in names)

check("hook: DeadWatchStore imported", "from deadwatch import" in src)
check("hook: deadwatches store created", "deadwatches = DeadWatchStore()" in src)
check("hook: threading imported", "import threading" in src)
check("hook: DownloadCancelled imported", "DownloadCancelled" in src)

# run_torrent registers the download for /dl + passes cancel_event
rt = next(n for n in ast.walk(tree)
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_torrent")
rt_src = ast.get_source_segment(src, rt)
check("hook: run_torrent registers _active_downloads",
      "_active_downloads[dl_token]" in rt_src)
check("hook: run_torrent passes cancel_event",
      "cancel_event=cancel_event" in rt_src)
check("hook: run_torrent pops registry",
      "_active_downloads.pop(dl_token, None)" in rt_src)
check("hook: _prog updates registry",
      'entry["done"] = done' in rt_src)

# follow_job consults the skip helper
fj = next(n for n in ast.walk(tree)
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "follow_job")
check("hook: follow_job uses _follow_skip_reason",
      "_follow_skip_reason(uid, it)" in ast.get_source_segment(src, fj))

# callback routes
for pat, good, bad in [
    (r"dlx:([0-9a-f]{12})", "dlx:abcdef123456", "dlx:xyz"),
    (r"sbest:([0-9a-f]{12})", "sbest:abcdef123456", "sbest:zz"),
    (r"dwdel:([0-9a-f]+)", "dwdel:abc123", "dwdel:ZZZ"),
]:
    rx = re.compile(pat)
    check(f"hook: {pat} matches", rx.fullmatch(good) is not None)
    check(f"hook: {pat} rejects", rx.fullmatch(bad) is None)
    check(f"hook: {pat} routed", f're.fullmatch(r"{pat}"' in src)

# smart pick button in _search_kb
check("hook: sbest button in _search_kb", 'f"sbest:{key}"' in src)

# main(): commands, callback pattern, job
check("hook: commands registered",
      all(f'("{c}"' in src for c in
          ["dl", "totorrent", "setminseeders", "blacklist",
           "watchdead", "deadwatch", "unwatchdead"]))
check("hook: callback pattern covers dlx/sbest/dwdel",
      "dlx:" in src and "sbest:" in src and "dwdel:" in src)
check("hook: deadwatch_job scheduled",
      "run_repeating(deadwatch_job" in src)

# BOT_COMMANDS
for c in ["dl", "totorrent", "setminseeders", "blacklist",
          "watchdead", "deadwatch", "unwatchdead"]:
    check(f"hook: /{c} in BOT_COMMANDS", f'("{c}",' in src)

# --- _title_blacklisted token matching (ast-extracted, needs st + re) ------------
bl_src = [n for n in tree.body
          if isinstance(n, ast.FunctionDef)
          and n.name == "_title_blacklisted"][0]
bl_ns = {"re": re, "st": lambda uid: {"blacklist": store.DEFAULTS["blacklist"]}}
exec(compile(ast.Module([bl_src], []), "bot.py", "exec"), bl_ns)
bl = bl_ns["_title_blacklisted"]
for title, want in [
    ("Movie.2026.HDCAM.x264", "hdcam"),
    ("Movie.2026.CAM.x264", "cam"),
    ("Movie.2026.TS.x264", "ts"),
    ("The.Sports.Show.S01E01.1080p.WEB-DL", None),  # 'ts' must NOT match
    ("Movie.2026.1080p.WEB-DL", None),
    ("", None),
]:
    check(f"blacklist: {title!r} -> {want!r}", bl(1, title) == want)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)

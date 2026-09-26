"""v5.7.2 tests: ios_remux (MKV/EAC3 -> MP4/AAC for iPhone playback).

Run: python3 test_v572.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import media_tools

PASS = []


def check(name, cond):
    PASS.append(name)
    assert cond, f"FAIL: {name}"


# --- probe_streams parses ffprobe json -------------------------------------
class FakeProc:
    def __init__(self, out):
        self.stdout = out


real_run = media_tools.subprocess.run
real_which = media_tools.shutil.which


def fake_which_ok(name):
    return "/usr/bin/ffprobe"


media_tools.shutil.which = fake_which_ok
media_tools.subprocess.run = lambda *a, **k: FakeProc(
    '{"streams": ['
    '{"codec_type": "video", "codec_name": "hevc"},'
    '{"codec_type": "audio", "codec_name": "eac3"},'
    '{"codec_type": "subtitle", "codec_name": "subrip"}'
    ']}')
try:
    ps = media_tools.probe_streams("x.mkv")
    check("probe video codec", ps["video"] == "hevc")
    check("probe audio codec", ps["audio"] == "eac3")
finally:
    media_tools.subprocess.run = real_run


def fake_which_missing(name):
    return None


media_tools.shutil.which = fake_which_missing
try:
    check("probe no ffprobe -> {}", media_tools.probe_streams("x") == {})
finally:
    media_tools.shutil.which = real_which


# --- ios_remux decision logic (mock probe + ffmpeg) --------------------------
calls = []


async def fake_probe(path):
    return dict(fake_probe.streams)


async def fake_ffmpeg_ok(args):
    calls.append(list(args))
    return None


async def fake_ffmpeg_fail_once(args):
    calls.append(list(args))
    if "-c:s" in args:
        raise RuntimeError("ffmpeg error")
    return None


async def run_remux(src, streams, ffmpeg):
    fake_probe.streams = streams
    calls.clear()
    media_tools.asyncio.to_thread = _to_thread_sync
    media_tools.probe_streams = fake_probe_async
    media_tools._run_ffmpeg = ffmpeg
    try:
        return await media_tools.ios_remux(src, "out.mp4")
    finally:
        media_tools._run_ffmpeg = real_ffmpeg
        media_tools.probe_streams = real_probe


async def fake_probe_async(path):
    return dict(fake_probe.streams)


async def _to_thread_sync(fn, *a, **k):
    res = fn(*a, **k)
    if asyncio.iscoroutine(res):
        res = await res
    return res


real_to_thread = media_tools.asyncio.to_thread
real_ffmpeg = media_tools._run_ffmpeg
real_probe = media_tools.probe_streams


def remux(src, streams, ffmpeg):
    return asyncio.run(run_remux(src, streams, ffmpeg))


# 1. mp4 + aac -> untouched (no ffmpeg call)
r = remux("in.mp4", {"video": "h264", "audio": "aac"}, fake_ffmpeg_ok)
check("mp4+aac untouched", r == "in.mp4" and calls == [])

r = remux("in.mov", {"video": "h264", "audio": "mp3"}, fake_ffmpeg_ok)
check("mov+mp3 untouched", r == "in.mov" and calls == [])

# 2. mkv + eac3 -> remux, video copy, audio aac
r = remux("in.mkv", {"video": "hevc", "audio": "eac3"}, fake_ffmpeg_ok)
check("mkv+eac3 remuxed", r == "out.mp4")
check("video stream-copy", "-c:v" in calls[0] and "copy" in calls[0])
ia = calls[0].index("-c:a")
check("eac3 -> aac", calls[0][ia + 1] == "aac")
check("faststart set", "+faststart" in calls[0])
check("subtitles kept attempt", "mov_text" in calls[0])

# 3. mkv + aac audio -> audio copied, not re-encoded
r = remux("in.mkv", {"video": "hevc", "audio": "aac"}, fake_ffmpeg_ok)
check("mkv+aac remuxed", r == "out.mp4")
ia = calls[0].index("-c:a")
check("aac audio copied", calls[0][ia + 1] == "copy")

# 4. mkv + dts -> aac
r = remux("in.mkv", {"video": "h264", "audio": "dts"}, fake_ffmpeg_ok)
ia = calls[0].index("-c:a")
check("dts -> aac", calls[0][ia + 1] == "aac")

# 5. no audio stream -> no audio mapping, still remuxes container
r = remux("in.mkv", {"video": "hevc", "audio": None}, fake_ffmpeg_ok)
check("no-audio remuxed", r == "out.mp4" and "-c:a" not in calls[0])

# 6. mov_text fails (PGS subs) -> retry without subtitles
r = remux("in.mkv", {"video": "hevc", "audio": "eac3"}, fake_ffmpeg_fail_once)
check("PGS fallback retried", len(calls) == 2)
check("PGS fallback drops subs", "-sn" in calls[1] and r == "out.mp4")

# 7. both attempts fail -> raises (caller falls back to original)
async def fake_ffmpeg_always_fail(args):
    raise RuntimeError("ffmpeg error")


try:
    remux("in.mkv", {"video": "hevc", "audio": "eac3"}, fake_ffmpeg_always_fail)
    check("double failure raises", False)
except RuntimeError:
    check("double failure raises", True)

media_tools.asyncio.to_thread = real_to_thread
media_tools.probe_streams = real_probe
media_tools._run_ffmpeg = real_ffmpeg

# --- post_process hooks ios_remux (static) ------------------------------------
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bot.py"), encoding="utf-8").read()
check("post_process calls ios_remux", "await ios_remux(cur, out)" in src)
check("ios_remux imported", "ios_remux" in src.split("from media_tools import")[1].split("\n")[0])
check("remux failure falls back",
      "ios remux failed, sending original" in src)

print(f"✅ v5.7.2 ios-remux: {len(PASS)} tests passed")

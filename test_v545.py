"""v5.4.5 mock tests — no network. Run: python3 test_v545.py"""
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import web_download as wd

PASS = []


def check(name, cond):
    assert cond, f"FAIL: {name}"
    PASS.append(name)


def ffprobe_json(streams, duration):
    return json.dumps({"streams": streams,
                       "format": {"duration": str(duration)}})


def run_json(payload):
    m = mock.Mock()
    m.stdout = payload
    return m


V_STREAM = {"codec_type": "video", "width": 1080, "height": 1920,
            "avg_frame_rate": "30/1", "duration": "22.500000",
            "nb_frames": "675"}
A_STREAM = {"codec_type": "audio", "duration": "22.499002",
            "nb_frames": "487"}

# --- 1. verify_web_video: healthy file --------------------------------------
with mock.patch.object(wd.shutil, "which", return_value="/usr/bin/ffprobe"), \
     mock.patch.object(wd.subprocess, "run",
                       return_value=run_json(
                           ffprobe_json([V_STREAM, A_STREAM], 22.5))):
    ok, reason = wd.verify_web_video("/tmp/x.mp4")
check("verify healthy -> True", ok is True)

# --- 2. verify: truncated video track (frozen-frame symptom) ----------------
trunc = dict(V_STREAM, duration="2.000000", nb_frames="60")
with mock.patch.object(wd.shutil, "which", return_value="/usr/bin/ffprobe"), \
     mock.patch.object(wd.subprocess, "run",
                       return_value=run_json(
                           ffprobe_json([trunc, A_STREAM], 22.5))):
    ok, reason = wd.verify_web_video("/tmp/x.mp4")
check("verify truncated -> False", ok is False)
check("verify truncated reason", "truncated" in reason)

# --- 3. verify: no video stream ----------------------------------------------
with mock.patch.object(wd.shutil, "which", return_value="/usr/bin/ffprobe"), \
     mock.patch.object(wd.subprocess, "run",
                       return_value=run_json(ffprobe_json([A_STREAM], 22.5))):
    ok, reason = wd.verify_web_video("/tmp/x.mp4")
check("verify no video stream -> False", ok is False)

# --- 4. verify: zero dimensions ----------------------------------------------
nodim = dict(V_STREAM, width=0, height=0)
with mock.patch.object(wd.shutil, "which", return_value="/usr/bin/ffprobe"), \
     mock.patch.object(wd.subprocess, "run",
                       return_value=run_json(
                           ffprobe_json([nodim, A_STREAM], 22.5))):
    ok, reason = wd.verify_web_video("/tmp/x.mp4")
check("verify no dims -> False", ok is False)

# --- 5. verify: no false positive on slightly shorter video track ------------
near = dict(V_STREAM, duration="22.400000")
with mock.patch.object(wd.shutil, "which", return_value="/usr/bin/ffprobe"), \
     mock.patch.object(wd.subprocess, "run",
                       return_value=run_json(
                           ffprobe_json([near, A_STREAM], 22.5))):
    ok, reason = wd.verify_web_video("/tmp/x.mp4")
check("verify 22.4/22.5 within tolerance -> True", ok is True)

# --- 6. verify: ffprobe missing -> skip, never block --------------------------
with mock.patch.object(wd.shutil, "which", return_value=None):
    ok, reason = wd.verify_web_video("/tmp/x.mp4")
check("verify no ffprobe -> skip True", ok is True)

# --- 7. verify: probe crash -> skip, never block ------------------------------
with mock.patch.object(wd.shutil, "which", return_value="/usr/bin/ffprobe"), \
     mock.patch.object(wd.subprocess, "run",
                       side_effect=OSError("boom")):
    ok, reason = wd.verify_web_video("/tmp/x.mp4")
check("verify probe crash -> skip True", ok is True)

# --- 8. ensure_audio_track: audio present -> untouched, ffmpeg not run --------
with mock.patch.object(wd.shutil, "which",
                       return_value="/usr/bin/ffprobe"), \
     mock.patch.object(wd.subprocess, "run") as mr:
    mr.return_value = mock.Mock(stdout="codec_type=audio\n")
    out = wd.ensure_audio_track("/tmp/v.mp4")
check("ensure audio present -> same path", out == "/tmp/v.mp4")
check("ensure audio present -> ffmpeg not called", mr.call_count == 1)

# --- 9. ensure_audio_track: missing -> mux silent AAC -------------------------
calls = []


def fake_run(cmd, **kw):
    calls.append(cmd)
    m = mock.Mock()
    if cmd[0] == "ffprobe":
        m.stdout = ""  # no audio stream
    return m


with mock.patch.object(wd.shutil, "which",
                       return_value="/usr/bin/ffmpeg"), \
     mock.patch.object(wd.subprocess, "run", side_effect=fake_run), \
     mock.patch.object(wd.os.path, "exists", return_value=True), \
     mock.patch.object(wd.os.path, "getsize", return_value=100), \
     mock.patch.object(wd.os, "replace") as mrep:
    out = wd.ensure_audio_track("/tmp/v.mp4")
check("ensure no audio -> same path", out == "/tmp/v.mp4")
check("ensure no audio -> os.replace called", mrep.called)
ff = [c for c in calls if c[0] == "ffmpeg"][0]
check("ensure mux uses anullsrc", "anullsrc=r=44100:cl=stereo" in ff)
check("ensure mux copies video", "copy" in ff)
check("ensure mux aac audio", "aac" in ff)
check("ensure mux -shortest", "-shortest" in ff)

# --- 10. ensure_audio_track: ffmpeg missing -> untouched ----------------------
with mock.patch.object(wd.shutil, "which", return_value=None), \
     mock.patch.object(wd.subprocess, "run") as mr:
    out = wd.ensure_audio_track("/tmp/v.mp4")
check("ensure no ffmpeg -> same path", out == "/tmp/v.mp4")
check("ensure no ffmpeg -> nothing run", mr.call_count == 0)

# --- 11. photo cache fix: no subscript on sent.photo --------------------------
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bot.py"), encoding="utf-8").read()
check("photo cache no subscript",
      '"photo": getattr(sent, "photo", None),' in src)
check("photo cache old bug gone", 'or [None])[-1]' not in src)

# --- 12. _CorruptDownload wired into download_web retry loop ------------------
dlsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "web_download.py"), encoding="utf-8").read()
check("corrupt exception defined", "class _CorruptDownload" in dlsrc)
check("verify called in loop", "verify_web_video, result[0]" in dlsrc)
check("ensure_audio in loop", "ensure_audio_track, result[0]" in dlsrc)
check("corrupt retry limit", "corrupt_n >= 3" in dlsrc)
check("audio_only skips mux", "if not audio_only:" in dlsrc)
check("x cascade verified", "X cascade download corrupt" in dlsrc)

print(f"✅ v5.4.5: {len(PASS)} tests passed")

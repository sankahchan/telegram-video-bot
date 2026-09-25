"""v5.4.6 mock tests — no network. Run: python3 test_v546.py"""
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

# --- verify_web_video (unchanged from v5.4.5) --------------------------------
trunc = dict(V_STREAM, duration="2.000000", nb_frames="60")
with mock.patch.object(wd.shutil, "which", return_value="/usr/bin/ffprobe"), \
     mock.patch.object(wd.subprocess, "run",
                       return_value=run_json(
                           ffprobe_json([V_STREAM, A_STREAM], 22.5))):
    check("verify healthy -> True",
          wd.verify_web_video("/tmp/x.mp4")[0] is True)

with mock.patch.object(wd.shutil, "which", return_value="/usr/bin/ffprobe"), \
     mock.patch.object(wd.subprocess, "run",
                       return_value=run_json(
                           ffprobe_json([trunc, A_STREAM], 22.5))):
    ok, reason = wd.verify_web_video("/tmp/x.mp4")
    check("verify truncated -> False", ok is False)
    check("verify truncated reason", "truncated" in reason)

with mock.patch.object(wd.shutil, "which", return_value="/usr/bin/ffprobe"), \
     mock.patch.object(wd.subprocess, "run",
                       return_value=run_json(ffprobe_json([A_STREAM], 22.5))):
    check("verify no video stream -> False",
          wd.verify_web_video("/tmp/x.mp4")[0] is False)

with mock.patch.object(wd.shutil, "which", return_value=None):
    check("verify no ffprobe -> skip True",
          wd.verify_web_video("/tmp/x.mp4")[0] is True)


def probe_payload(vcodec, acodec):
    streams = [{"codec_type": "video", "codec_name": vcodec}]
    if acodec:
        streams.append({"codec_type": "audio", "codec_name": acodec})
    return run_json(json.dumps({"streams": streams}))


def run_normalize(vcodec, acodec):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        m = mock.Mock()
        if cmd[0] == "ffprobe":
            m.stdout = probe_payload(vcodec, acodec).stdout
        return m

    with mock.patch.object(wd.shutil, "which",
                           return_value="/usr/bin/ffmpeg"), \
         mock.patch.object(wd.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(wd.os.path, "exists", return_value=True), \
         mock.patch.object(wd.os.path, "getsize", return_value=100), \
         mock.patch.object(wd.os, "replace"):
        out = wd.normalize_web_video("/tmp/v.mp4")
    return out, calls


# --- normalize: h264+aac -> untouched, ffmpeg never runs ----------------------
out, calls = run_normalize("h264", "aac")
check("normalize h264+aac -> same path", out == "/tmp/v.mp4")
check("normalize h264+aac -> ffmpeg not called",
      not any(c[0] == "ffmpeg" for c in calls))

# --- normalize: vp9+aac -> transcode to h264 ----------------------------------
out, calls = run_normalize("vp9", "aac")
check("normalize vp9 -> same path", out == "/tmp/v.mp4")
ff = [c for c in calls if c[0] == "ffmpeg"]
check("normalize vp9 -> ffmpeg called once", len(ff) == 1)
ff = ff[0]
check("normalize vp9 -> libx264", "libx264" in ff)
check("normalize vp9 -> yuv420p", "yuv420p" in ff)
check("normalize vp9 -> aac audio", "aac" in ff)
check("normalize vp9 -> faststart", "+faststart" in ff)
check("normalize vp9 -> no anullsrc (has audio)",
      not any("anullsrc" in a for a in ff))

# --- normalize: vp9 no audio -> transcode + silent aac -------------------------
out, calls = run_normalize("vp9", None)
ff = [c for c in calls if c[0] == "ffmpeg"][0]
check("normalize vp9 silent -> libx264", "libx264" in ff)
check("normalize vp9 silent -> anullsrc",
      any("anullsrc" in a for a in ff))
check("normalize vp9 silent -> -shortest", "-shortest" in ff)

# --- normalize: h264 no audio -> stream copy + silent aac (no re-encode) ------
out, calls = run_normalize("h264", None)
ff = [c for c in calls if c[0] == "ffmpeg"][0]
check("normalize h264 silent -> copy video", "copy" in ff)
check("normalize h264 silent -> no libx264", "libx264" not in ff)
check("normalize h264 silent -> anullsrc",
      any("anullsrc" in a for a in ff))

# --- normalize: av1 -> transcode -----------------------------------------------
out, calls = run_normalize("av1", "opus")
ff = [c for c in calls if c[0] == "ffmpeg"][0]
check("normalize av1 -> libx264", "libx264" in ff)
check("normalize av1 opus -> aac", "aac" in ff)

# --- normalize: ffmpeg missing -> untouched ------------------------------------
with mock.patch.object(wd.shutil, "which", return_value=None), \
     mock.patch.object(wd.subprocess, "run") as mr:
    out = wd.normalize_web_video("/tmp/v.mp4")
check("normalize no ffmpeg -> same path", out == "/tmp/v.mp4")
check("normalize no ffmpeg -> nothing run", mr.call_count == 0)

# --- normalize: ffmpeg failure -> original kept ---------------------------------
def boom(cmd, **kw):
    if cmd[0] == "ffprobe":
        return probe_payload("vp9", "aac")
    raise OSError("ffmpeg exploded")

with mock.patch.object(wd.shutil, "which",
                       return_value="/usr/bin/ffmpeg"), \
     mock.patch.object(wd.subprocess, "run", side_effect=boom):
    out = wd.normalize_web_video("/tmp/v.mp4")
check("normalize ffmpeg fail -> original path", out == "/tmp/v.mp4")

# --- wiring --------------------------------------------------------------------
dlsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "web_download.py"), encoding="utf-8").read()
check("normalize defined", "def normalize_web_video" in dlsrc)
check("normalize in x cascade", "normalize_web_video, path)" in dlsrc)
check("normalize in yt-dlp loop",
      "normalize_web_video, result[0]" in dlsrc)
check("audio_only skips normalize", "if not audio_only:" in dlsrc)
check("old helper gone", "ensure_audio_track" not in dlsrc)

print(f"✅ v5.4.6: {len(PASS)} tests passed")

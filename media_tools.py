"""ffmpeg helpers: audio extraction, trim, compress, probe."""
import asyncio
import json
import os
import re
import shutil
import subprocess


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _need_ffmpeg():
    if not has_ffmpeg():
        raise RuntimeError(
            "ffmpeg မရှိသေးပါ — VPS မှာ install လုပ်ပါ:\n"
            "sudo apt-get install -y ffmpeg"
        )


async def _run_ffmpeg(args):
    _need_ffmpeg()
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        # Keep the tail of ffmpeg's own error output — a bare
        # "ffmpeg error" made VPS failures (e.g. the iOS remux one)
        # impossible to diagnose from the journal.
        tail = (err or b"").decode("utf-8", "replace").strip().splitlines()
        # keep the END of the output (the actual error); ffmpeg's banner
        # alone can exceed the budget and would otherwise crowd it out
        tail = "\n".join(tail[-12:])[-800:]
        raise RuntimeError(f"ffmpeg error (rc={proc.returncode}): {tail}")


async def to_mp3(src: str, dst: str) -> str:
    """Extract audio as MP3."""
    await _run_ffmpeg(["-i", src, "-vn", "-c:a", "libmp3lame", "-q:a", "4", dst])
    return dst


async def trim_video(src: str, dst: str, start: float, end: float) -> str:
    """Cut segment [start, end) seconds."""
    if end <= start:
        raise ValueError("အဆုံး အချိန်က အစ ထက် ကြီးရမယ်")
    await _run_ffmpeg([
        "-ss", str(start), "-to", str(end),
        "-i", src, "-c", "copy", dst,
    ])
    # -c copy with mp4 can produce unseekable output; re-mux if tiny/failed
    if not os.path.exists(dst) or os.path.getsize(dst) == 0:
        await _run_ffmpeg([
            "-ss", str(start), "-to", str(end),
            "-i", src, "-c:v", "libx264", "-preset", "veryfast",
            "-c:a", "aac", dst,
        ])
    return dst


async def compress_video(src: str, dst: str) -> str:
    """Compress to 720p for smaller size (quality=low).

    min(720,ih): never upscale videos that are already smaller than 720p —
    the original resolution is preserved in that case.
    """
    await _run_ffmpeg([
        "-i", src,
        "-vf", "scale=-2:'min(720,ih)'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-c:a", "aac", "-b:a", "128k",
        dst,
    ])
    return dst


def parse_ts(s: str) -> float:
    """Parse '90' | '1:30' | '01:02:03' | '1m30s' -> seconds."""
    s = s.strip().lower()
    m = re.fullmatch(r"(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", s)
    if m and (m.group(1) or m.group(2)):
        mins = float(m.group(1) or 0)
        secs = float(m.group(2) or 0)
        return mins * 60 + secs
    parts = s.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"အချိန် ပုံစံမှားနေပါတယ်: {s} (ဥပမာ 90 / 1:30 / 01:02:03)")
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    raise ValueError(f"အချိန် ပုံစံမှားနေပါတယ်: {s}")


def parse_trim_args(args) -> tuple:
    """['0:10', '0:45'] -> (10.0, 45.0). Raises ValueError with usage."""
    if len(args) != 2:
        raise ValueError("အသုံးပြုပုံ: /trim <အစ> <အဆုံး>  (ဥပမာ /trim 0:10 0:45)")
    return parse_ts(args[0]), parse_ts(args[1])


def probe_video(path: str) -> dict:
    """ffprobe -> {'width': w, 'height': h, 'duration': secs}.

    Rotation metadata (phone videos) is accounted for: 90/270 deg swaps w/h.
    Returns {} when ffprobe is missing or probing fails — callers must
    fall back to 0s (previous behavior).
    """
    if not shutil.which("ffprobe"):
        return {}
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=width,height",
             "-show_entries", "stream_tags=rotate",
             "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30,
        )
        info = json.loads(proc.stdout or "{}")
        width = height = 0
        for st in info.get("streams") or []:
            if st.get("width"):
                width = int(st["width"] or 0)
                height = int(st.get("height") or 0)
                try:
                    rot = int((st.get("tags") or {}).get("rotate") or 0)
                except (TypeError, ValueError):
                    rot = 0
                if rot in (90, 270):
                    width, height = height, width
                break
        duration = 0
        try:
            duration = int(float((info.get("format") or {}).get("duration") or 0))
        except (TypeError, ValueError):
            pass
        return {"width": width, "height": height, "duration": duration}
    except Exception:
        return {}


def probe_streams(path: str) -> dict:
    """ffprobe -> {'video': codec|None, 'audio': codec|None} (first of each).

    Returns {} when ffprobe is missing or probing fails.
    """
    if not shutil.which("ffprobe"):
        return {}
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_type,codec_name",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30,
        )
        info = json.loads(proc.stdout or "{}")
        out = {"video": None, "audio": None}
        for st in info.get("streams") or []:
            ct = st.get("codec_type")
            if ct in out and out[ct] is None:
                out[ct] = (st.get("codec_name") or "").lower() or None
        return out
    except Exception:
        return {}


_IOS_CONTAINER_OK = {".mp4", ".m4v", ".mov"}
_IOS_AUDIO_OK = {"aac", "mp3"}


def ios_container_ok(ext: str) -> bool:
    """True when the container already plays on iPhone (no convert prompt needed)."""
    return ext.lower() in _IOS_CONTAINER_OK


async def ios_remux(src: str, dst: str) -> str:
    """Make a video iOS-Telegram friendly WITHOUT re-encoding video.

    - Other containers (mkv/avi/...) -> MP4, video stream-copied
      (HEVC/H.264 both play on iPhone; no quality loss, fast).
    - Non-AAC/MP3 audio (ac3/eac3/dts/opus/...) -> AAC 192k
      (iOS can't decode AC3/EAC3/DTS, which is why some downloads
      play video with no sound).
    - Text subtitles kept as mov_text when possible.

    Returns src unchanged when already compatible. Raises on failure
    (callers should fall back to the original file).
    """
    ext = os.path.splitext(src)[1].lower()
    streams = await asyncio.to_thread(probe_streams, src)
    acodec = (streams or {}).get("audio")
    vcodec = (streams or {}).get("video")
    if ext in _IOS_CONTAINER_OK and acodec in (None, *_IOS_AUDIO_OK):
        return src
    if not vcodec:
        raise RuntimeError("video stream မတွေ့လို့ convert မလုပ်နိုင်ပါ")
    base = ["-i", src, "-map", "0:v?", "-c:v", "copy"]
    if acodec:
        base += ["-map", "0:a?"]
        if acodec in _IOS_AUDIO_OK:
            base += ["-c:a", "copy"]
        else:
            base += ["-c:a", "aac", "-b:a", "192k"]
    base += ["-movflags", "+faststart"]
    try:
        # try 1: keep text subtitles
        await _run_ffmpeg(base + ["-map", "0:s?", "-c:s", "mov_text", dst])
    except RuntimeError:
        # try 2: bitmap subtitles (PGS) can't go into MP4 -> drop them
        await _run_ffmpeg(base + ["-sn", dst])
    # --- validate the output: never hand back a broken or silent file ---
    if not os.path.exists(dst) or os.path.getsize(dst) < 1024:
        raise RuntimeError("convert output file ပျက်နေပါတယ်")
    if acodec:
        out_streams = await asyncio.to_thread(probe_streams, dst)
        if not (out_streams or {}).get("audio"):
            # Source had audio but the remux lost it (e.g. probe failed and
            # audio was never mapped) — a silent video is worse than the
            # original, so fail loudly and let the caller fall back.
            try:
                os.remove(dst)
            except OSError:
                pass
            raise RuntimeError("convert လုပ်ပြီးမှ အသံပါမလာပါ")
    return dst

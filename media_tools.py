"""ffmpeg helpers: audio extraction, trim, compress."""
import asyncio
import os
import re
import shutil


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
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError("ffmpeg error")


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
    """Compress to 720p for smaller size (quality=low)."""
    await _run_ffmpeg([
        "-i", src,
        "-vf", "scale=-2:720",
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

"""v6.0.3 tests — iOS remux failure visibility + output validation.

Chan reported a torrent .mkv (HEVC 10-bit + DDP 5.1) arriving unconverted:
the v5.7.2 ios_remux failed on the VPS and fell back to the original
silently, and the swallowed ffmpeg stderr (bare "ffmpeg error") made the
real cause invisible in the journal. v6.0.3:
  1. _run_ffmpeg captures ffmpeg's stderr tail into the raised error.
  2. ios_remux validates its output (exists, non-empty, audio preserved) —
     never hands back a broken or silent file.
  3. post_process appends a bilingual warning to note_out on remux failure,
     and every download path surfaces it in the caption.

Run: python3 test_v603.py
"""
import asyncio
import os
import re
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

import media_tools
from media_tools import _run_ffmpeg, ios_remux

PASS = []


def check(name, cond):
    assert cond, f"FAIL: {name}"
    PASS.append(name)


HAVE_FFMPEG = media_tools.has_ffmpeg()
print(f"ffmpeg present: {HAVE_FFMPEG}")


# --- 1. ffmpeg stderr is captured into the raised error --------------------
if HAVE_FFMPEG:
    try:
        asyncio.run(_run_ffmpeg(["-i", "/nonexistent_xyz_123"]))
        raised = None
    except RuntimeError as e:
        raised = str(e)
    check("ffmpeg failure raises RuntimeError", raised is not None)
    check("error mentions return code", "rc=" in raised)
    # ffmpeg prints "...: No such file or directory" for a missing input
    check("error includes ffmpeg stderr tail", "No such file" in raised)
    check("error not the old bare message", raised != "ffmpeg error")
else:
    print("SKIP: real-ffmpeg stderr test (no ffmpeg)")


# --- 2. ios_remux still works on a realistic file ---------------------------
def _make_hevc_ac3_mkv(path):
    """10-bit HEVC + AC3 5.1 + SRT subs, 3s — like a BluRay rip."""
    import subprocess
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-preset", "ultrafast",
         "-c:a", "ac3", "-b:a", "192k",
         "-t", "3", path],
        check=True)


if HAVE_FFMPEG:
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "rip.mkv")
        _make_hevc_ac3_mkv(src)
        dst = os.path.join(td, "rip_ios.mp4")
        out = asyncio.run(ios_remux(src, dst))
        check("ios_remux converts mkv/ac3", out == dst)
        streams = asyncio.run(
            asyncio.to_thread(media_tools.probe_streams, dst))
        check("remuxed video kept (hevc)",
              (streams or {}).get("video") == "hevc")
        check("remuxed audio is aac", (streams or {}).get("audio") == "aac")
        check("remuxed file smaller than src (audio re-encoded)",
              os.path.getsize(dst) < os.path.getsize(src))
else:
    print("SKIP: real-ffmpeg remux test (no ffmpeg)")


# --- 3. ios_remux refuses to hand back a silent/broken file ------------------
real_probe = media_tools.probe_streams


def _fake_probe_no_audio(path):
    # source probes fine; the OUTPUT probes as audio-less (simulates a
    # remux that dropped the audio track, e.g. probe failed at map time)
    if path.endswith("_ios.mp4"):
        return {"video": "hevc", "audio": None}
    return {"video": "hevc", "audio": "ac3"}


if HAVE_FFMPEG:
    media_tools.probe_streams = _fake_probe_no_audio
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "rip2.mkv")
            _make_hevc_ac3_mkv(src)
            dst = os.path.join(td, "rip2_ios.mp4")
            try:
                asyncio.run(ios_remux(src, dst))
                raised = None
            except RuntimeError as e:
                raised = str(e)
            check("silent remux output raises", raised is not None)
            check("silent remux error mentions audio",
                  "အသံ" in (raised or ""))
            check("bad output file removed", not os.path.exists(dst))
    finally:
        media_tools.probe_streams = real_probe

    # no video stream at all -> clear error, no ffmpeg invocation attempt
    media_tools.probe_streams = lambda p: {"video": None, "audio": "ac3"}
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "novideo.mkv")
            open(src, "wb").write(b"\x00" * 2048)
            try:
                asyncio.run(ios_remux(src, os.path.join(td, "o.mp4")))
                raised = None
            except RuntimeError as e:
                raised = str(e)
            check("missing video stream raises", raised is not None)
    finally:
        media_tools.probe_streams = real_probe
else:
    print("SKIP: validation tests (no ffmpeg)")


# --- 4. post_process note_out + all call sites ------------------------------
# Import bot.py for real with stubbed third-party modules (same pattern as
# test_v602, plus httpx/feedparser which aren't installed in this sandbox).
import types  # noqa: E402
import importlib.abc  # noqa: E402
import importlib.machinery  # noqa: E402


class _Any:
    def __init__(self, name="stub"):
        object.__setattr__(self, "_name", name)

    def __getattr__(self, attr):
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        return _Any(f"{object.__getattribute__(self, '_name')}.{attr}")

    def __call__(self, *a, **k):
        return _Any()


class _StubImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    PREFIXES = ("telegram", "pyrogram", "dotenv", "httpx", "feedparser")

    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == p or fullname.startswith(p + ".")
               for p in self.PREFIXES):
            return importlib.machinery.ModuleSpec(fullname, self,
                                                  is_package=True)
        return None

    def create_module(self, spec):
        mod = types.ModuleType(spec.name)
        mod.__path__ = []
        if spec.name == "dotenv":
            mod.load_dotenv = lambda *a, **k: None
        else:
            def _ga(attr, _mod=mod):
                if attr.startswith("__") and attr.endswith("__"):
                    raise AttributeError(attr)
                obj = _Any(f"{_mod.__name__}.{attr}")
                setattr(_mod, attr, obj)
                return obj
            mod.__getattr__ = _ga
        return mod

    def exec_module(self, module):
        pass


sys.meta_path.insert(0, _StubImporter())

os.environ.setdefault("API_ID", "12345")
os.environ.setdefault("API_HASH", "dummyhash")
os.environ.setdefault("BOT_TOKEN", "dummy:token")
os.environ.setdefault("SESSION_STRING", "dummysession")
os.environ.setdefault("ALLOWED_USER_IDS", "1180438393")

import bot  # noqa: E402  -- executes all module-level code
check("bot module imports with stubs", bot is not None)

# every post_process call site must thread note_out through
src = open(os.path.join(REPO, "bot.py"), encoding="utf-8").read()
starts = [m.start() for m in re.finditer(r"await post_process\(", src)]
check("post_process call sites found", len(starts) == 7)
for i, s in enumerate(starts):
    window = src[s:s + 400]
    check(f"call site {i+1} passes note_out", "note_out" in window)
check("_with_notes helper exists", callable(bot._with_notes))
check("_with_notes appends",
      bot._with_notes("cap", ["n1"]) == "cap\nn1")
check("_with_notes handles None caption", bot._with_notes(None, ["n1"]) == "n1")
check("_with_notes empty notes", bot._with_notes("cap", []) == "cap")


async def _fake_remux_ok(src, dst):
    open(dst, "wb").write(b"\x00" * 4096)
    return dst


async def _fake_remux_fail(src, dst):
    raise RuntimeError("ffmpeg error (rc=1): boom")


async def _run_pp(monkey_remux):
    real_st, real_remux = bot.st, bot.ios_remux
    bot.st = lambda uid: {"mp3": False, "quality": "high"}
    bot.ios_remux = monkey_remux
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "movie.mkv")
            open(src, "wb").write(b"\x00" * 4096)
            notes: list = []
            final, as_audio = await bot.post_process(
                src, "video", 1, td, 0, use_trim=False, note_out=notes)
            return final, as_audio, notes, src
    finally:
        bot.st, bot.ios_remux = real_st, real_remux


final, as_audio, notes, src = asyncio.run(_run_pp(_fake_remux_ok))
check("remux ok -> no note", notes == [])
check("remux ok -> returns converted path", final != src)

final, as_audio, notes, src = asyncio.run(_run_pp(_fake_remux_fail))
check("remux fail -> falls back to original", final == src)
check("remux fail -> bilingual note appended", len(notes) == 1
      and "convert" in notes[0] and "conversion" in notes[0])

print(f"\n✅ v6.0.3: {len(PASS)} passed")

"""v5.4.3 mock tests — no network. Run: python3 test_v543.py"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import web_download as wd

PASS = []


def check(name, cond):
    assert cond, f"FAIL: {name}"
    PASS.append(name)


# --- 1. storyboard_only: PO-token-missing signature --------------------------
SB = ("ERROR: [youtube] d33A264UMqo: Requested format is not available || "
      "YouTube returned formats=4, availability=public, live=not_live, "
      "sample=[sb3:mhtml:url,sb2:mhtml:url,sb1:mhtml:url,sb0:mhtml:url], "
      "pot=down, cookies=cookies.txt, ytdlp=2025.9.1")
check("sb storyboard-only -> True", wd.storyboard_only(SB))

ZERO = ("ERROR: [youtube] abc: Requested format is not available || "
        "YouTube returned formats=0, availability=public, pot=down")
check("sb formats=0 -> True", wd.storyboard_only(ZERO))

NORMAL = ("ERROR: [youtube] abc: Requested format is not available || "
          "YouTube returned formats=12, availability=public, "
          "sample=[248:webm:url,137:mp4:url,sb0:mhtml:url]")
check("sb playable formats -> False", not wd.storyboard_only(NORMAL))

check("sb no diagnosis -> False",
      not wd.storyboard_only("ERROR: [youtube] abc: some other error"))
check("sb empty -> False", not wd.storyboard_only(""))

# --- 2. pot_server_hint respects reachability --------------------------------
wd._pot_ok, wd._pot_checked_at = True, time.monotonic()
try:
    check("hint empty when server up", wd.pot_server_hint() == "")
finally:
    wd._pot_ok, wd._pot_checked_at = None, 0.0

wd._pot_ok, wd._pot_checked_at = False, time.monotonic()
try:
    h = wd.pot_server_hint()
    check("hint bilingual when down", "PO-token server" in h and "Docker" in h)
    check("hint has docker run", "docker run" in h and "pot-provider" in h
          and "brainicism/bgutil-ytdlp-pot-provider" in h)
    check("hint mentions /ytcheck", "/ytcheck" in h)
finally:
    wd._pot_ok, wd._pot_checked_at = None, 0.0

# --- 3. yt_pipeline_status shape (no network; TCP probe fails fast locally) --
st = wd.yt_pipeline_status()
for key in ("ytdlp", "pot_plugin", "pot_server", "pot_url", "cookies"):
    check(f"status has {key}", key in st)
check("status pot_server is bool", isinstance(st["pot_server"], bool))
check("status pot_url default", st["pot_url"] == "http://127.0.0.1:4416"
      or st["pot_url"].startswith("http"))

# --- 4. bot.py wiring: format-error branch uses the new helpers -------------
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bot.py"), encoding="utf-8").read()
seg = src.split('if "Requested format is not available" in s:')[1]
seg = seg.split('if "isn\'t available to everyone"')[0]
check("branch calls storyboard_only", "storyboard_only(s)" in seg)
check("branch appends pot_server_hint", "pot_server_hint()" in seg)
check("not-a-bot branch uses pot_server_hint",
      src.count("pot_server_hint()") >= 2)
check("ytcheck registered", '("ytcheck", ytcheck_cmd)' in src)
check("ytcheck in help", "/ytcheck" in src)

print(f"✅ v5.4.3: {len(PASS)} tests passed")

# --- 5. photo extension fix (PHOTO_EXT_INVALID) ------------------------------
# original_filename() exec'd in isolation: bot.py needs pyrogram/PTB at
# import time, so pull just the pure helpers out of the source.
import ast as _ast
import mimetypes as _mimetypes

_tree = _ast.parse(src)
_helpers = {}
for _node in _ast.walk(_tree):
    if isinstance(_node, _ast.FunctionDef) and _node.name in (
            "original_filename", "media_of", "_safe_filename"):
        _helpers[_node.name] = _ast.get_source_segment(src, _node)
_ns = {"mimetypes": _mimetypes}
for _n in ("_safe_filename", "media_of", "original_filename"):
    exec(_helpers[_n], _ns)
_original_filename = _ns["original_filename"]


class _NoNameMedia:
    """Telegram Photo/VideoNote-like: no file_name, no mime_type."""
    file_name = None
    mime_type = None


class _NamedMedia:
    def __init__(self, file_name=None, mime_type=None):
        self.file_name = file_name
        self.mime_type = mime_type


def _msg(**kw):
    m = type("M", (), {})()
    for k in ("photo", "video", "video_note", "document",
              "animation", "audio", "voice"):
        setattr(m, k, kw.get(k))
    return m


check("photo gets .jpg",
      _original_filename(_msg(photo=_NoNameMedia()), "photo", "3_1")
      == "3_1_photo.jpg")
check("video_note gets .mp4",
      _original_filename(_msg(video_note=_NoNameMedia()), "video_note", "3_2")
      == "3_2_video_note.mp4")
check("doc keeps original name",
      _original_filename(_msg(document=_NamedMedia("report.pdf")), "doc", "3")
      == "3_report.pdf")
check("voice ogg stays .ogg",
      _original_filename(_msg(voice=_NamedMedia(mime_type="audio/ogg")),
                         "audio", "3") == "3_audio.ogg")
check("video mp4 via mime",
      _original_filename(_msg(video=_NamedMedia(mime_type="video/mp4")),
                         "video", "3") == "3_video.mp4")

# deliver() must guarantee a valid image extension before send_photo
_dseg = src.split('elif kind == "photo" and mode == "video":')[1]
_dseg = _dseg.split("elif")[0]
check("deliver renames extensionless photo",
      'path + ".jpg"' in _dseg and "os.rename" in _dseg)

# --- 6. deterministic shortfall convergence ---------------------------------
_conv_src = _helpers.get("_size_converged")
if _conv_src is None:  # helper defined at module level, not nested
    for _node in _ast.walk(_tree):
        if isinstance(_node, _ast.FunctionDef) and _node.name == "_size_converged":
            _conv_src = _ast.get_source_segment(src, _node)
_cns = {}
exec(_conv_src, _cns)
_size_converged = _cns["_size_converged"]

E = 53698533  # the real-world case: expected vs converged 53477376
G = 53477376
check("converged identical x3 -> True", _size_converged([G, G, G], E))
check("converged x4 -> True", _size_converged([G, G, G, G], E))
check("varying sizes -> False",
      not _size_converged([G, G - 1000, G], E))
check("only 2 attempts -> False", not _size_converged([G, G], E))
check("exact match -> False", not _size_converged([E, E, E], E))
check("far short (<95%) -> False",
      not _size_converged([E // 2, E // 2, E // 2], E))
check("just under 95% -> False",
      not _size_converged([int(E * 0.94)] * 3, E))
check("at 95% -> True",
      _size_converged([int(E * 0.96)] * 3, E))
check("expected=0 -> False", not _size_converged([G, G, G], 0))
check("empty -> False", not _size_converged([], E))

print(f"✅ v5.4.3 (photo fix): {len(PASS)} tests passed")

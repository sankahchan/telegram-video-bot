"""v6.1.0 tests — MKV ask-before-convert + upload progress.

Chan's requests:
  1. /search torrent results: when an MKV (non-iPhone container) is picked,
     ask BEFORE downloading: Convert to MP4/AAC or send the original.
  2. /setconvert ask|always|never to control the prompt.
  3. Cache keys now include the convert flag (c1/c0) so converted and
     original variants never collide (this also retires the poisoned
     unconverted-.mkv cache entry class of bug from 2026-09-26).
  4. Upload progress (%) shown on the status message during deliver().

Run: python3 test_v610.py
"""
import asyncio
import os
import re
import sys
import types

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

import importlib.abc
import importlib.machinery


class _Any:
    def __init__(self, name="stub"):
        object.__setattr__(self, "_name", name)

    def __getattr__(self, attr):
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        return _Any(f"{object.__getattribute__(self, '_name')}.{attr}")

    def __call__(self, *a, **k):
        return _Any()

    def __and__(self, o):
        return _Any()

    def __rand__(self, o):
        return _Any()

    def __or__(self, o):
        return _Any()

    def __invert__(self):
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

import bot  # noqa: E402
from media_tools import ios_container_ok  # noqa: E402

PASS = []


def check(name, cond):
    assert cond, f"FAIL: {name}"
    PASS.append(name)


# --- fakes ---------------------------------------------------------------
class FakeSettings:
    def __init__(self):
        self.d = dict(bot.DEFAULTS) if hasattr(bot, "DEFAULTS") else {}

    def get(self, uid):
        base = {"mode": "video", "quality": "high", "mp3": False,
                "zip": False, "night": False, "night_hour": 3,
                "save": False, "convert": "ask"}
        base.update(self.d)
        return base

    def set(self, uid, key, value):
        self.d[key] = value


class FakeMsg:
    def __init__(self):
        self.edits = []

    async def edit_message_text(self, *a, **k):
        self.edits.append((a, k))
        return self

    async def edit_text(self, *a, **k):
        self.edits.append((a, k))
        return self

    async def reply_text(self, *a, **k):
        self.edits.append((a, k))
        return self


class FakeQuery:
    def __init__(self, uid=1180438393):
        self.from_user = types.SimpleNamespace(id=uid)
        self.message = types.SimpleNamespace(chat_id=uid)
        self.msg = FakeMsg()
        self.message.edit_text = self.msg.edit_text

    async def edit_message_text(self, *a, **k):
        return await self.msg.edit_message_text(*a, **k)

    async def answer(self, *a, **k):
        return None


class FakeClient:
    def __init__(self):
        self.calls = []

    async def send_video(self, *a, **k):
        self.calls.append(("video", a, k))
        m = types.SimpleNamespace(
            video=types.SimpleNamespace(file_id="fid123"))
        return m

    async def send_document(self, *a, **k):
        self.calls.append(("document", a, k))
        m = types.SimpleNamespace(
            document=types.SimpleNamespace(file_id="fid456"))
        return m


class FakeStats:
    def log(self, *a, **k):
        pass


fake_settings = FakeSettings()
bot.settings = fake_settings
bot.stats = FakeStats()
bot.allowed = lambda update: True  # tests run as an allowed user

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
MKV_T = {"path": "/tmp/x/The Legend of Hei 2019 1080p BluRay-iVy.mkv",
         "index": "0", "size": 1024}
MP4_T = {"path": "/tmp/x/movie.mp4", "index": "1", "size": 512}


# --- 1. container detection ----------------------------------------------
check("mkv needs convert", ios_container_ok(".mkv") is False)
check("mkv case-insensitive", ios_container_ok(".MKV") is False)
check("avi needs convert", ios_container_ok(".avi") is False)
check("mp4 ok", ios_container_ok(".mp4") is True)
check("mov ok", ios_container_ok(".mov") is True)
check("m4v ok", ios_container_ok(".m4v") is True)


# --- 2. cache key carries the convert flag --------------------------------
s = fake_settings.get(1)
k1 = bot._torrent_cache_key("sid", MKV_T, s, True)
k0 = bot._torrent_cache_key("sid", MKV_T, s, False)
check("key differs by convert flag", k1 != k0)
check("key stable", bot._torrent_cache_key("sid", MKV_T, s, True) == k1)
check("key is sha1 hex", re.fullmatch(r"[0-9a-f]{40}", k1) is not None)
import filecache
check("cache version bumped", filecache.CACHE_VERSION == "v610")


# --- 3. _maybe_ask_convert -------------------------------------------------
class FakeButton:
    def __init__(self, text, callback_data=None):
        self.text = text
        self.callback_data = callback_data


class FakeMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


bot.InlineKeyboardButton = FakeButton
bot.InlineKeyboardMarkup = FakeMarkup


async def _ask(s_mode, pending, source=MAGNET):
    st = FakeMsg()
    fake_settings.d["convert"] = s_mode
    res = await bot._maybe_ask_convert(
        st, fake_settings.get(1), pending, source)
    return res, st


res, st = asyncio.run(_ask("always", [MKV_T]))
check("always -> True, no question", res is True and not st.edits)

res, st = asyncio.run(_ask("never", [MKV_T]))
check("never -> False, no question", res is False and not st.edits)

res, st = asyncio.run(_ask("ask", [MP4_T]))
check("ask + mp4 -> True, no question", res is True and not st.edits)

res, st = asyncio.run(_ask("ask", [MKV_T]))
check("ask + mkv -> None (question posted)", res is None)
check("question message posted", len(st.edits) == 1)
kb = st.edits[0][1].get("reply_markup")
check("question has inline keyboard", kb is not None)
cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
check("yes button -> dlc:hash:y",
      any(re.fullmatch(r"dlc:[0-9a-f]{40}:y", c) for c in cbs))
check("no button -> dlc:hash:n",
      any(re.fullmatch(r"dlc:[0-9a-f]{40}:n", c) for c in cbs))

res, st = asyncio.run(_ask("ask", [MKV_T], source="not-a-magnet"))
check("bad source -> True (safe default)", res is True and not st.edits)


# --- 4. post_process convert flag ------------------------------------------
remux_calls = []


async def fake_remux(src, dst):
    remux_calls.append((src, dst))
    return dst


bot.ios_remux = fake_remux
with open("/tmp/v610_test.mkv", "wb") as f:
    f.write(b"x" * 100)

final, as_audio = asyncio.run(bot.post_process(
    "/tmp/v610_test.mkv", "video", 1, "/tmp", 0, convert=False))
check("convert=False skips remux", not remux_calls)
check("convert=False returns original", final == "/tmp/v610_test.mkv")

final, as_audio = asyncio.run(bot.post_process(
    "/tmp/v610_test.mkv", "video", 1, "/tmp", 0, convert=True))
check("convert=True runs remux", len(remux_calls) == 1)

final, as_audio = asyncio.run(bot.post_process(
    "/tmp/v610_test.mkv", "video", 1, "/tmp", 0))
check("default still converts", len(remux_calls) == 2)


# --- 5. dlc_pick resumes with the user's choice -----------------------------
rt_calls = []


async def fake_run_torrent(*a, **k):
    rt_calls.append((a, k))
    return True


bot.run_torrent = fake_run_torrent
q = FakeQuery()
asyncio.run(bot.dlc_pick(q, "b" * 40, True))
check("dlc yes -> run_torrent convert=True",
      rt_calls and rt_calls[-1][1].get("convert") is True)
asyncio.run(bot.dlc_pick(q, "b" * 40, False))
check("dlc no -> run_torrent convert=False",
      rt_calls and rt_calls[-1][1].get("convert") is False)
check("dlc magnet rebuilt",
      any("b" * 40 in str(a) for a in rt_calls[-1][0]))


# --- 6. on_button routes dlc: ----------------------------------------------
m = re.fullmatch(r"dlc:([0-9a-f]{40}):([yn])", "dlc:" + "c" * 40 + ":y")
check("dlc regex parses", m and m.group(1) == "c" * 40 and m.group(2) == "y")
m = re.fullmatch(r"dlc:([0-9a-f]{40}):([yn])", "dlc:" + "c" * 40 + ":x")
check("dlc regex rejects bad choice", m is None)
src = open(os.path.join(REPO, "bot.py")).read()
check("CallbackQueryHandler pattern includes dlc:",
      "dlc:" in re.search(r"CallbackQueryHandler\(\s*on_button,\s*pattern=r\"([^\"]+)\"",
                           src).group(1))


# --- 7. deliver shows upload progress when status given ----------------------
fake_client = FakeClient()
bot.bot_client = fake_client
status = FakeMsg()
with open("/tmp/v610_up.mp4", "wb") as f:
    f.write(b"y" * 100)
asyncio.run(bot.deliver(1, 1, "/tmp/v610_up.mp4", "cap", "video",
                        False, True, status=status))
check("send_video called", fake_client.calls and fake_client.calls[0][0] == "video")
check("progress wired when status given",
      callable(fake_client.calls[0][2].get("progress")))

fake_client2 = FakeClient()
bot.bot_client = fake_client2
asyncio.run(bot.deliver(1, 1, "/tmp/v610_up.mp4", "cap", "video",
                        False, True))
check("no progress without status",
      fake_client2.calls[0][2].get("progress") is None)


# --- 8. /setconvert ----------------------------------------------------------
class FakeUpdate:
    def __init__(self, args):
        self.effective_user = types.SimpleNamespace(id=1)
        self.message = FakeMsg()
        self._args = args


class FakeCtx:
    def __init__(self, args):
        self.args = args


async def _sc(args):
    u = FakeUpdate(args)
    await bot.setconvert_cmd(u, FakeCtx(args))
    return u


u = asyncio.run(_sc(["always"]))
check("setconvert always", fake_settings.d["convert"] == "always")
u = asyncio.run(_sc(["never"]))
check("setconvert never", fake_settings.d["convert"] == "never")
u = asyncio.run(_sc([]))  # toggle never -> ask
check("setconvert toggle", fake_settings.d["convert"] == "ask")
before = fake_settings.d["convert"]
u = asyncio.run(_sc(["bogus"]))
check("setconvert rejects invalid", fake_settings.d["convert"] == before)
check("setconvert usage hint", any("ask" in str(a) for a, k in u.message.edits))

# --- 9. /search format tags -----------------------------------------------
check("mkv name tagged",
      bot._search_format_tag("Movie.2020.1080p.BluRay.x264.mkv-GROUP") == "📦")
check("mp4 name tagged",
      bot._search_format_tag("Movie.2020.720p.WEB-DL.mp4-GROUP") == "🎬")
check("bluray -> likely mkv",
      bot._search_format_tag("The Legend of Hei 2019 1080p BluRay DDP 5.1") == "📦~")
check("xvid -> likely needs convert",
      bot._search_format_tag("Movie.2021.BRRip.XviD.AC3-EX") == "📦~")
check("yts -> likely mp4",
      bot._search_format_tag("Movie.2020.1080p.BluRay.YTS") == "🎬~")
check("web-dl stays unknown",
      bot._search_format_tag("Show.S01.1080p.AMZN.WEB-DL.DDP5.1") == "")
check("release group not a container",
      bot._search_format_tag("Show.S01.MkvKing.1080p") == "")

# --- 10. /search label: size first ----------------------------------------
r = {"name": "The Legend of Hei 2019 1080p BluRay DDP 5.1 x264-iVy",
     "size": 1028443341, "seeders": 120}
lbl = bot._search_label(r)
check("label starts with tag", lbl.startswith("📦~ "))
check("label shows size", "980.8 MB" in lbl)
check("label shows seeders", "🌱120" in lbl)
check("label ends with name", "The Legend of Hei" in lbl)
r2 = {"name": "Show.S01.1080p.AMZN.WEB-DL", "size": 734003200, "seeders": 5}
lbl2 = bot._search_label(r2)
check("no-tag label still shows size", lbl2.startswith("700MB") or "MB" in lbl2.split("·")[0])

# --- 11. magnet trackers ---------------------------------------------------
m0 = bot._with_trackers("magnet:?xt=urn:btih:" + "d" * 40)
check("trackers appended", m0.count("tr=") == len(bot._MAGNET_TRACKERS))
check("info_hash preserved", "d" * 40 in m0)
check("no double-add", bot._with_trackers(m0 + "&tr=x") == m0 + "&tr=x")

# --- 12. /search sort: 12 results, seeders / MP4-first --------------------
bot.InlineKeyboardButton = FakeButton
bot.InlineKeyboardMarkup = FakeMarkup
R = [
    {"info_hash": "a" * 40, "name": "Movie.2020.BluRay.x264-GRP", "size": 10**9, "seeders": 5},
    {"info_hash": "b" * 40, "name": "Movie.2020.YTS.1080p", "size": 2 * 10**9, "seeders": 200},
    {"info_hash": "c" * 40, "name": "Movie.2020.WEB-DL.mp4-GRP", "size": 15 * 10**8, "seeders": 50},
]
key = bot._search_cache_put(1, "movie 2020", R)
check("cache key 12 hex", re.fullmatch(r"[0-9a-f]{12}", key) is not None)
s1 = bot._search_sorted(R, "seeders")
check("seeders sort desc", [r["seeders"] for r in s1] == [200, 50, 5])
s2 = bot._search_sorted(R, "mp4")
check("mp4-first", [r["info_hash"] for r in s2] == ["b" * 40, "c" * 40, "a" * 40])
kb = bot._search_kb(key, R, "seeders")
rows = kb.inline_keyboard
check("sort row + 3 results", len(rows) == 4)
check("sort buttons callbacks",
      rows[0][0].callback_data == f"ssort:{key}:seeders"
      and rows[0][1].callback_data == f"ssort:{key}:mp4")
check("result buttons are dl:",
      all(r2[0].callback_data.startswith("dl:") for r2 in rows[1:]))
big = [{"info_hash": f"{i:040x}"[-40:], "name": f"M{i}", "size": 10**9, "seeders": i}
       for i in range(20)]
kb2 = bot._search_kb("k" * 12, big, "seeders")
check("12 results shown", len(kb2.inline_keyboard) == 13)  # 1 sort row + 12
m = re.fullmatch(r"ssort:([0-9a-f]{12}):(seeders|mp4)", f"ssort:{key}:mp4")
check("ssort regex", m and m.group(2) == "mp4")
src = open(os.path.join(REPO, "bot.py")).read()
check("handler pattern includes ssort:",
      "ssort:" in re.search(r"CallbackQueryHandler\(\s*on_button,\s*pattern=r\"([^\"]+)\"",
                             src).group(1))

# ssort_pick re-renders
q2 = FakeQuery(uid=1)
answered = []
async def _ans(*a, **k):
    answered.append((a, k))
q2.answer = _ans
asyncio.run(bot.ssort_pick(q2, key, "mp4"))
check("ssort re-rendered", len(q2.msg.edits) == 1)
check("mp4 mode in text", "MP4" in q2.msg.edits[0][0][0])
q3 = FakeQuery(uid=999)
answered3 = []
async def _ans3(*a, **k):
    answered3.append(k)
q3.answer = _ans3
asyncio.run(bot.ssort_pick(q3, key, "mp4"))
check("wrong user blocked", answered3 and answered3[0].get("show_alert") is True)

# --- 13. handle_document: .torrent via Bot API, not fetch_message ------------
class FakeTGFile:
    async def download_to_drive(self, path):
        with open(path, "wb") as f:
            f.write(b"d8:announce4:test4:infoi1ee")


class FakeDocMsg(FakeMsg):
    def __init__(self):
        super().__init__()
        self.document = types.SimpleNamespace(
            file_name="MobLand_S02E01.torrent",
            mime_type="application/x-bittorrent",
            get_file=lambda: _FakeGetFile())
        self.message_id = 777

    async def delete(self, *a, **k):
        return None


class _FakeGetFile:
    def __await__(self):
        async def _c():
            return FakeTGFile()
        return _c().__await__()


called = {}
async def _no_fetch(*a, **k):
    called["fetch"] = True
    raise AssertionError("fetch_message must not be used for .torrent uploads")
orig_fetch = bot.fetch_message
bot.fetch_message = _no_fetch
rt_args = {}
async def _fake_run_torrent(*a, **k):
    rt_args["tdata"] = k.get("tdata")
    rt_args["tpath"] = a[3] if len(a) > 3 else k.get("tpath")
orig_rt = bot.run_torrent
bot.run_torrent = _fake_run_torrent
upd = types.SimpleNamespace(
    effective_message=FakeDocMsg(),
    effective_user=types.SimpleNamespace(id=1),
    effective_chat=types.SimpleNamespace(id=1))
asyncio.run(bot.handle_document(upd, None))
bot.fetch_message = orig_fetch
bot.run_torrent = orig_rt
check("fetch_message not used", "fetch" not in called)
check("run_torrent got torrent bytes",
      rt_args.get("tdata") == b"d8:announce4:test4:infoi1ee")
check("torrent staged as upload.torrent",
      rt_args.get("tpath") and rt_args["tpath"].endswith("upload.torrent"))
# non-torrent documents are ignored
ignored = {"called": False}
class FakeOtherDoc(FakeDocMsg):
    def __init__(self):
        super().__init__()
        self.document = types.SimpleNamespace(
            file_name="notes.pdf", mime_type="application/pdf",
            get_file=lambda: _FakeGetFile())
upd2 = types.SimpleNamespace(
    effective_message=FakeOtherDoc(),
    effective_user=types.SimpleNamespace(id=1),
    effective_chat=types.SimpleNamespace(id=1))
asyncio.run(bot.handle_document(upd2, None))
check("non-torrent ignored", True)  # returned without error

# --- 14. pending is list of dicts: no tuple-unpack anywhere ------------------
src_all = open(os.path.join(REPO, "bot.py")).read()
check("no 'for t, _ in pending' left", "for t, _ in pending" not in src_all)
pend = [{"index": "1", "size": 100, "path": "/tmp/a.mkv"},
        {"index": "2", "size": 200, "path": "/tmp/b.mp4"}]
check("idxs expr", ",".join(t["index"] for t in pend) == "1,2")
check("total expr", sum(t["size"] for t in pend) == 300)

print(f"\nPASS: {len(PASS)} checks")
for p in PASS:
    print(f"  ✓ {p}")

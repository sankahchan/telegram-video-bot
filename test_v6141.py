"""v6.14.1 tests — terminal watch status is never delivered silently.

2026-09-28: the browser cleared the Cloudflare wall, all batch-1 probes
finished, but the user never got the quality picker and the journal showed
nothing after 'probe done: vidnest'. The final _safe_status_edit failed
(flaky MTProto / flood ban) and its False return was ignored.
_fix_: _deliver_watch_status tries the edit, falls back to a fresh
send_message, and logs every outcome.
"""
import asyncio
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# bot.py can't be imported here (needs pyrogram/dotenv/telegram — VPS-only
# deps), so extract the REAL _deliver_watch_status from its source via AST
# and exec it in a controlled namespace. This tests the shipped code, not
# a copy.
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "bot.py")).read()
_tree = ast.parse(_src)
_fn = next(n for n in ast.walk(_tree)
           if isinstance(n, ast.AsyncFunctionDef)
           and n.name == "_deliver_watch_status")
_ns = {"asyncio": asyncio, "bot_client": None}
exec(compile(ast.Module(body=[_fn], type_ignores=[]), "bot.py", "exec"), _ns)


def _set_bot_client(c):
    _ns["bot_client"] = c


_deliver_watch_status = _ns["_deliver_watch_status"]

_results = []
_calls = []


def check(name, cond):
    _results.append((name, bool(cond)))
    print(("✅ " if cond else "❌ ") + name, flush=True)


class _FakeMsg:
    def __init__(self, mode="ok"):
        self.mode = mode

    async def edit_text(self, text, **kw):
        _calls.append(("edit", text, kw))
        if self.mode == "fail":
            raise RuntimeError("RetryAfter: flood ban")
        if self.mode == "hang":
            await asyncio.sleep(60)
        if self.mode == "cancelled":
            raise asyncio.CancelledError()
        return True


class _Sent:
    pass


class _FakeBot:
    def __init__(self, mode="ok"):
        self.mode = mode

    async def send_message(self, chat_id, text, **kw):
        _calls.append(("send", chat_id, text, kw))
        if self.mode == "fail":
            raise RuntimeError("send boom")
        return _Sent()


async def main():
    _set_bot_client(_FakeBot("ok"))

    # 1: edit works -> returned msg, no send
    _calls.clear()
    m = _FakeMsg("ok")
    r = await _deliver_watch_status(123, m, "hello", parse_mode="Markdown")
    check("edit ok: returns status msg", r is m)
    check("edit ok: no fresh send", [c[0] for c in _calls] == ["edit"])
    check("edit ok: kwargs pass through",
          _calls[0][2].get("parse_mode") == "Markdown")

    # 2: edit fails (flood ban) -> fresh send with same text+kwargs
    _calls.clear()
    r = await _deliver_watch_status(123, _FakeMsg("fail"), "picker",
                                    parse_mode="Markdown",
                                    reply_markup="KB")
    check("edit fail: falls back to send", isinstance(r, _Sent))
    sends = [c for c in _calls if c[0] == "send"]
    check("edit fail: send got text+kwargs",
          len(sends) == 1 and sends[0][2] == "picker"
          and sends[0][3].get("reply_markup") == "KB")

    # 3: status_msg None -> straight to send
    _calls.clear()
    r = await _deliver_watch_status(123, None, "hello")
    check("msg None: sends fresh", isinstance(r, _Sent)
          and [c[0] for c in _calls] == ["send"])

    # 4: edit hangs -> timeout -> falls back to send (bounded)
    _calls.clear()
    t0 = asyncio.get_event_loop().time()
    r = await _deliver_watch_status(123, _FakeMsg("hang"), "hello",
                                    timeout=1)
    dt = asyncio.get_event_loop().time() - t0
    check("hung edit: bounded fallback to send",
          isinstance(r, _Sent) and dt < 10)

    # 5: both fail -> None, never raises
    _set_bot_client(_FakeBot("fail"))
    _calls.clear()
    try:
        r = await _deliver_watch_status(123, _FakeMsg("fail"), "hello")
        check("both fail: None, no raise", r is None)
    except Exception:
        check("both fail: None, no raise", False)

    # 6: CancelledError propagates (cancellation must not be swallowed)
    _set_bot_client(_FakeBot("ok"))
    try:
        await _deliver_watch_status(123, _FakeMsg("cancelled"), "hello")
        check("cancelled: propagates", False)
    except asyncio.CancelledError:
        check("cancelled: propagates", True)
    except Exception:
        check("cancelled: propagates", False)


asyncio.run(main())

print()
failed = [n for n, ok in _results if not ok]
print(f"{len(_results) - len(failed)}/{len(_results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)

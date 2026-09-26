"""v6.0.2 — import smoke test.

v6.0.1 crashed on the VPS at startup with
    NameError: name 'BookmarkStore' is not defined
because bot.py was never actually imported in tests (only py_compile ran
in update.sh). This test imports bot.py for real with stubbed
third-party modules, executing ALL module-level code — any NameError,
ImportError or other startup crash fails loudly here instead of on the VPS.
"""
import os
import sys
import types

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)


import importlib.abc
import importlib.machinery


class _Any:
    """Duck-type stub: any attr access, call, or operator returns another _Any."""
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
    PREFIXES = ("telegram", "pyrogram", "dotenv")

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

# dummy env so module-level credential check passes
os.environ.setdefault("API_ID", "12345")
os.environ.setdefault("API_HASH", "dummyhash")
os.environ.setdefault("BOT_TOKEN", "dummy:token")
os.environ.setdefault("SESSION_STRING", "dummysession")
os.environ.setdefault("ALLOWED_USER_IDS", "1180438393")

import bot  # noqa: E402  -- executes all module-level code

checks = []


def check(name, cond):
    assert cond, f"FAIL: {name}"
    checks.append(name)


check("bot module imported", bot is not None)
check("bookmarks store", isinstance(bot.bookmarks, bot.BookmarkStore))
check("user_store", bot.user_store is not None)
check("follows store", bot.follows is not None)
check("quota_allows", callable(bot.quota_allows))
check("on_button", callable(bot.on_button))
check("handle_link", callable(bot.handle_link))
check("BOT_COMMANDS non-empty", len(bot.BOT_COMMANDS) > 10)
check("md_esc", bot._md_esc("a_b") == "a\\_b")
check("tg_src_url", bot._tg_src_url(-1001, 2) == "https://t.me/c/1/2")

print(f"\n✅ v6.0.2 import smoke: {len(checks)} passed")

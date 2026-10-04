"""v6.16.1 tests — Telegram menu is bilingual (Myanmar + English).

Chan asked for English in the Telegram menu (2026-10-04). Covered:
- BOT_COMMANDS (the "/" popup menu): all 35 descriptions bilingual
- /menu inline keyboard: title (5 occurrences) + buttons bilingual
- /menu submenu prompts (search/tv/subs/follows/bookmarks/stats/drive)
- WELCOME (/start) and HELP_OVERVIEW (/help)
HELP_TOPICS were already bilingual — untouched.
"""
import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_results = []


def check(name, cond):
    _results.append((name, bool(cond)))
    print(("✅ " if cond else "❌ ") + name, flush=True)


_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "bot.py")).read()
_tree = ast.parse(_src)

# BOT_COMMANDS via AST (bot.py can't be imported — VPS-only deps)
_cmds = None
for _n in ast.walk(_tree):
    if isinstance(_n, ast.Assign):
        for _t in _n.targets:
            if getattr(_t, "id", "") == "BOT_COMMANDS":
                _cmds = ast.literal_eval(_n.value)
check("BOT_COMMANDS found", _cmds is not None)
_missing = [c for c, d in _cmds
            if not re.search(r"[A-Za-z]{3,}", d)]
check(f"BOT_COMMANDS all bilingual ({len(_cmds)} cmds)",
      _cmds is not None and not _missing)
if _missing:
    print("   missing English:", _missing)

# /menu title — bilingual, all 5 occurrences
_n = _src.count('"🎛️ **Menu** — လိုတာနှိပ် / Tap what you need:"')
check("menu title bilingual x5", _n == 5)

# buttons: no Myanmar-only labels left in the menu keyboard
check("no Myanmar-only menu button text",
      '"🔎 Torrent ရှာ",' not in _src
      and '"🎛️ **Menu** — လိုတာနှိပ်:"' not in _src)
check("torrent button is English", '"🔎 Torrent Search"' in _src)

# submenu prompts bilingual
for _marker in ["Send text to search", "Send a series name",
                "Send a movie name", "Tap a button to switch",
                "No bookmarks yet", "Drive is connected",
                "Drive not connected", "30 days"]:
    check(f"submenu EN: {_marker[:24]}", _marker in _src)

# WELCOME + HELP_OVERVIEW bilingual
check("WELCOME bilingual", "Welcome to Downloader Bot" in _src)
check("HELP_OVERVIEW bilingual", "**Command များ / Commands**" in _src)

n_fail = sum(1 for _, ok in _results if not ok)
print(f"\n{len(_results) - n_fail}/{len(_results)} passed")
sys.exit(1 if n_fail else 0)

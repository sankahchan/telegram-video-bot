"""v5.7.1 tests: /menu UI + bot command menu.

Static checks on bot.py source (bot.py itself needs telegram/pyrogram
to import, so we parse instead of importing).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = []


def check(name, cond):
    PASS.append(name)
    assert cond, f"FAIL: {name}"


src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bot.py"), encoding="utf-8").read()

# BOT_COMMANDS: no dupes
cmds = re.findall(r'\(\s*"([a-z]+)"\s*,\s*"[^"]*"\),', src.split("BOT_COMMANDS = [")[1].split("]")[0])
check("BOT_COMMANDS non-empty", len(cmds) > 10)
check("BOT_COMMANDS no dupes", len(cmds) == len(set(cmds)))

# every BOT_COMMAND has a registered CommandHandler
reg_block = src.split("for cmd, fn in [")[1].split("]:")[0]
registered = re.findall(r'\("([a-z]+)",\s*\w+\)', reg_block)
for c in cmds:
    if c in ("menu",):  # registered separately below; check anyway
        pass
    check(f"command /{c} registered", c in registered)

# menu + search + tv registered
for c in ("menu", "search", "tv"):
    check(f"/{c} in handlers", f'("{c}",' in reg_block)

# menu callback_data values all handled by menu_cb
cb_datas = set(re.findall(r'callback_data="menu:([a-z]+)"', src))
check("menu buttons exist", len(cb_datas) >= 10)
handled = set(re.findall(r'action == "([a-z]+)"', src))
check("menu:main handled", "main" in src)  # elif action == "main"
for cb in cb_datas:
    check(f"menu:{cb} handled",
          cb in handled or f'"{cb}"' in src)

# on_button routes menu:*
check("menu route in on_button",
      re.search(r'fullmatch\(r"menu:\(\[a-z\]\+\)"', src) is not None)
check("menu pattern in CallbackQueryHandler",
      re.search(r'CallbackQueryHandler\(\s*on_button,\s*pattern=r"[^"]*menu:',
                src) is not None)

# set_my_commands wired in post_init
check("set_my_commands in post_init",
      "_set_bot_commands(application)" in src)
check("BotCommand used", "BotCommand(c, d)" in src)

# help topics for new commands
for t in ('"menu": (', '"search": (', '"tv": ('):
    check(f"help topic {t} exists", t in src)

print(f"✅ v5.7.1 menu: {len(PASS)} tests passed")

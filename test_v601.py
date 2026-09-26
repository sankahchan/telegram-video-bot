"""v6.0.1 audit fixes — network-free mock/static tests.

Covers the bugs found in the full re-audit of v6.0.0:
1. _md_esc: Telegram legacy-Markdown escaping for user/URL-derived text
   (URLs with `_` used to crash /history, /bookmarks, /follows, /users,
   drive messages with "can't parse entities").
2. _tg_src_url: rebuild t.me links for /history resend.
3. follow_job: select_releases() now runs before the notify branch
   (notify-only EZTV feeds used to spam one msg per release).
4. /find flow: quota check added.
5. src_url now passed to deliver() in tg batch, /find, night drain, watch
   (history resend fallback used to be dead for Telegram downloads).
6. tname escaped in all Markdown drive messages.
"""
import ast
import re
import sys

SRC = open("bot.py", encoding="utf-8").read()
TREE = ast.parse(SRC)
PASS = []


def check(name, cond):
    assert cond, f"FAIL: {name}"
    PASS.append(name)


# ---- 1. _md_esc behaviour -------------------------------------------
ns = {"re": re}
for node in ast.walk(TREE):
    if isinstance(node, ast.FunctionDef) and node.name in ("_md_esc", "_tg_src_url"):
        exec(compile(ast.parse(ast.get_source_segment(SRC, node)),
                     "<fn>", "exec"), ns)
_md_esc = ns["_md_esc"]
_tg_src_url = ns["_tg_src_url"]

check("esc underscore", _md_esc("a_b") == "a\\_b")
check("esc star+brackets", _md_esc("x*y[z]") == "x\\*y\\[z\\]")
check("esc backtick", _md_esc("a`b") == "a\\`b")
check("esc empty", _md_esc("") == "")
check("esc none", _md_esc(None) == "")
check("esc plain unchanged", _md_esc("hello 123") == "hello 123")
check("esc url", _md_esc("https://x.com/a_b") == "https://x.com/a\\_b")

check("tg url private", _tg_src_url(-1003559980888, 123) == "https://t.me/c/3559980888/123")
check("tg url username", _tg_src_url("someuser", 45) == "https://t.me/someuser/45")

# ---- 2. escaping applied at all Markdown call sites -------------------
check("history label escaped", "_md_esc((it.get(\"url\") or \"\")[:50])" in SRC)
check("bookmark title escaped", "_md_esc((b.get('title') or '')[:55])" in SRC)
check("follow name escaped", "_md_esc(v['name'])" in SRC)
check("user name escaped", "_md_esc(u['name'])" in SRC)
check("drive tname_md callback", "tname_md = _md_esc(tname)" in SRC)
# every remaining raw `{tname}` must be in a message WITHOUT parse_mode="Markdown"
lines = SRC.splitlines()
for i, l in enumerate(lines):
    if "`{tname}`" in l:
        window = "\n".join(lines[i:i + 4])
        check(f"tname plain-text only (line {i + 1})",
              'parse_mode="Markdown"' not in window)

# ---- 3. follow_job ordering -------------------------------------------
fj = None
for node in ast.walk(TREE):
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "follow_job":
        fj = ast.get_source_segment(SRC, node)
        break
check("follow_job found", fj is not None)
i_sel = fj.index("select_releases(fresh)")
i_notify = fj.index('== "notify"')
check("select_releases before notify", i_sel < i_notify)
check("no literal ** in follow msg", '**{f[' not in fj and "**{f[" not in fj)

# ---- 4. /find quota + src_url ------------------------------------------
check("find quota check", "quota_allows(uid)" in SRC)
# the find flow deliver passes src_url
check("find src_url", "src_url=_tg_src_url(cid, mid)" in SRC)

# ---- 5. src_url in tg batch / night / watch ----------------------------
check("tg batch src_url", "cache_key=t_ckey, src_url=t_src" in SRC)
check("night tg src_url", 'src_url=_tg_src_url(ref["chat"], ref["msg"])' in SRC)
check("watch src_url", "src_url=_tg_src_url(cid, m.id)" in SRC)

# ---- 6. callback routes still intact -------------------------------------
pats = []
for node in ast.walk(TREE):
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_button":
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "fullmatch":
                a = n.args[0]
                if isinstance(a, ast.Constant):
                    pats.append(a.value)
samples = {
    r"menu:([a-z]+)": ["menu:history", "menu:bookmarks"],
    r"subl:(\d+)": ["subl:3"],
    r"bm:(dl|del):(\d+)": ["bm:dl:0", "bm:del:12"],
    r"fl:mode:([A-Za-z0-9]+)": ["fl:mode:abc123", "fl:mode:tv9f8e7d6c"],
    r"hist:(\d+)": ["hist:0"],
}
for p in pats:
    for pat, vals in samples.items():
        if re.fullmatch(pat, p):
            pass
for pat, vals in samples.items():
    check(f"route {pat}", pat in pats)
    for v in vals:
        check(f"route match {v}", re.fullmatch(pat, v) is not None)

print(f"\n✅ v6.0.1 audit fixes: {len(PASS)} passed")

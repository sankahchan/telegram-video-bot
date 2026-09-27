"""v6.6.0 — torrent health pre-check (dead-magnet early warning).

Covers torrent_health.py (UDP scrape protocol, infohash extraction,
verdict logic) and the bot.py hook wiring (run_torrent skip_health,
thc: callback route).
"""
import ast
import asyncio
import base64
import os
import re
import struct
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

import torrent_health as th

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)


# --- extract_infohash -------------------------------------------------------
IH_HEX = "0123456789abcdef0123456789abcdef01234567"
check("ih: 40-hex", th.extract_infohash(
    f"magnet:?xt=urn:btih:{IH_HEX}&dn=x") == IH_HEX)
check("ih: uppercase lowered", th.extract_infohash(
    f"magnet:?xt=urn:btih:{IH_HEX.upper()}") == IH_HEX)
B32 = base64.b32encode(bytes.fromhex(IH_HEX)).decode()
check("ih: base32 -> hex", th.extract_infohash(
    f"magnet:?xt=urn:btih:{B32}") == IH_HEX)
check("ih: no btih -> None",
      th.extract_infohash("magnet:?xt=urn:sha1:abc&dn=x") is None)
check("ih: garbage -> None",
      th.extract_infohash("magnet:?xt=urn:btih:zzzz") is None)
check("ih: empty -> None", th.extract_infohash("") is None)
check("ih: 39-char hex -> None",
      th.extract_infohash("magnet:?xt=urn:btih:" + "a" * 39) is None)

# --- UDP protocol packets ---------------------------------------------------
tid = 12345
creq = th._build_connect_request(tid)
check("proto: connect req len 16", len(creq) == 16)
proto, act, rtid = struct.unpack(">qii", creq)
check("proto: connect req fields",
      proto == 0x41727101980 and act == 0 and rtid == tid)

cresp = struct.pack(">iiq", 0, tid, 0xCAFEBABEDEAD)
check("proto: connect resp parsed",
      th._parse_connect_response(cresp, tid) == 0xCAFEBABEDEAD)
check("proto: connect resp bad tid -> None",
      th._parse_connect_response(cresp, tid + 1) is None)
check("proto: connect resp truncated -> None",
      th._parse_connect_response(cresp[:10], tid) is None)

ih_bytes = bytes.fromhex(IH_HEX)
sreq = th._build_scrape_request(0xCAFEBABEDEAD, tid, ih_bytes)
check("proto: scrape req len 36", len(sreq) == 36)
check("proto: scrape req carries ih", sreq[16:] == ih_bytes)

sresp = struct.pack(">ii", 2, tid) + struct.pack(">iii", 7, 100, 3)
check("proto: scrape resp parsed",
      th._parse_scrape_response(sresp, tid) == (7, 100, 3))
check("proto: scrape resp bad action -> None",
      th._parse_scrape_response(struct.pack(">ii", 9, tid) + b"\x00" * 12,
                                tid) is None)
check("proto: scrape resp bad tid -> None",
      th._parse_scrape_response(sresp, tid + 1) is None)
check("proto: scrape resp truncated -> None",
      th._parse_scrape_response(sresp[:10], tid) is None)

# --- check_health verdicts (mocked network) ---------------------------------
real_scrape = th._scrape_one


async def _run_health(fake):
    th._scrape_one = fake
    try:
        return await th.check_health(IH_HEX)
    finally:
        th._scrape_one = real_scrape


def fake_none(host, port, ih, timeout=4.0):
    return None


def fake_dead(host, port, ih, timeout=4.0):
    return (0, 0)


def fake_alive(host, port, ih, timeout=4.0):
    return (5, 2) if "opentrackr" in host else (3, 9)


h = asyncio.run(_run_health(fake_none))
check("health: no trackers -> unknown",
      h["verdict"] == "unknown" and h["trackers_ok"] == 0
      and h["trackers_tried"] == len(th._SCRAPE_TRACKERS))

h = asyncio.run(_run_health(fake_dead))
check("health: 0 seeders -> dead",
      h["verdict"] == "dead" and h["seeders"] == 0)

h = asyncio.run(_run_health(fake_alive))
check("health: seeders -> alive, max taken",
      h["verdict"] == "alive" and h["seeders"] == 5 and h["leechers"] == 9)

h = asyncio.run(th.check_health("nothex"))
check("health: bad infohash -> unknown", h["verdict"] == "unknown")

# --- warning text ------------------------------------------------------------
w = th.dead_torrent_warning({"seeders": 0, "leechers": 4,
                             "trackers_ok": 5, "trackers_tried": 6,
                             "verdict": "dead"})
check("warning: bilingual + stats",
      "Seeder မတွေ့ပါ" in w and "No live seeders" in w
      and "5/6" in w and "leechers: 4" in w)

# --- bot.py hook wiring (ast + regex) ----------------------------------------
src = open("bot.py", encoding="utf-8").read()
tree = ast.parse(src)

rt = None
for node in ast.walk(tree):
    if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
            and node.name == "run_torrent":
        rt = node
        break
check("hook: run_torrent exists", rt is not None)
params = [a.arg for a in rt.args.args] if rt else []
check("hook: skip_health param", "skip_health" in params)
check("hook: _health_gate called",
      "_health_gate(" in ast.get_source_segment(src, rt))
check("hook: extract_infohash used",
      "extract_infohash(source)" in ast.get_source_segment(src, rt))

names = {n.name for n in ast.walk(tree)
         if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))}
check("hook: thc_pick defined", "thc_pick" in names)
check("hook: _health_gate defined", "_health_gate" in names)
check("hook: _prune_health_pending defined",
      "_prune_health_pending" in names)

m = re.search(r're\.fullmatch\(r"thc:\(\[0-9a-f\]\{12\}\):\(\[yn\]\)"',
              src)
check("hook: thc: route registered", m is not None)
pat = re.compile(r"thc:([0-9a-f]{12}):([yn])")
mm = pat.fullmatch("thc:abcdef123456:y")
check("hook: thc route matches sample",
      mm is not None and mm.group(1) == "abcdef123456"
      and mm.group(2) == "y")
check("hook: thc route rejects bad token",
      pat.fullmatch("thc:xyz:1") is None)
check("hook: secrets imported", "import secrets" in src)
check("hook: torrent_health imported",
      "from torrent_health import" in src)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)

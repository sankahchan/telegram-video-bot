"""Torrent health pre-check: live seeder/leecher probe before downloading.

Problem: a magnet with no live seeders makes aria2c hang in the metadata
fetch for up to 180s (e.g. Brothers.2026) before failing. This module
scrapes public UDP trackers for the infohash first (seconds, not minutes).
Zero live seeders -> the bot warns immediately and asks whether to force
the download, instead of a long silent wait.

Pure stdlib — no new dependencies. Implements the UDP tracker protocol
(BEP 15) scrape subset only.
"""
import asyncio
import base64
import hashlib
import random
import re
import socket
import struct

_PROTO_ID = 0x41727101980
_ACT_CONNECT = 0
_ACT_SCRAPE = 2

# (host, port) — UDP scrape uses the same endpoint as announce, no path.
_SCRAPE_TRACKERS = [
    ("tracker.openbittorrent.com", 80),
    ("tracker.opentrackr.org", 1337),
    ("exodus.desync.com", 6969),
    ("explodie.org", 6969),
    ("tracker.torrent.eu.org", 451),
    ("open.stealth.si", 80),
]

_EACH_TIMEOUT = 4.0  # seconds per tracker


def extract_infohash(magnet: str) -> str | None:
    """Pull the btih value out of a magnet link. Returns 40-char lowercase
    hex, or None when the magnet has no usable infohash."""
    m = re.search(r"btih:([0-9A-Za-z]{32,40})", magnet or "")
    if not m:
        return None
    raw = m.group(1)
    if len(raw) == 40 and re.fullmatch(r"[0-9a-fA-F]{40}", raw):
        return raw.lower()
    if len(raw) == 32:
        try:
            digest = base64.b32decode(raw.upper())
        except Exception:
            return None
        if len(digest) == 20:
            return digest.hex()
    return None


# --- UDP tracker protocol (BEP 15), scrape subset --------------------------

def _build_connect_request(tid: int) -> bytes:
    return struct.pack(">qii", _PROTO_ID, _ACT_CONNECT, tid)


def _parse_connect_response(data: bytes, tid: int) -> int | None:
    """Returns the connection id, or None on mismatch/truncation."""
    if len(data) < 16:
        return None
    action, rtid, conn_id = struct.unpack(">iiq", data[:16])
    if action != _ACT_CONNECT or rtid != tid:
        return None
    return conn_id


def _build_scrape_request(conn_id: int, tid: int, ih: bytes) -> bytes:
    return struct.pack(">qii", conn_id, _ACT_SCRAPE, tid) + ih


def _parse_scrape_response(data: bytes, tid: int) -> tuple | None:
    """Returns (seeders, completed, leechers) for the first infohash,
    or None on mismatch/truncation."""
    if len(data) < 20:
        return None
    action, rtid = struct.unpack(">ii", data[:8])
    if action != _ACT_SCRAPE or rtid != tid:
        return None
    seeders, completed, leechers = struct.unpack(">iii", data[8:20])
    return (seeders, completed, leechers)


def _scrape_one(host: str, port: int, ih: bytes,
                timeout: float = _EACH_TIMEOUT) -> tuple | None:
    """Blocking single-tracker scrape. Returns (seeders, leechers) or None
    when the tracker is unreachable / misbehaves."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            tid = random.getrandbits(31)
            sock.sendto(_build_connect_request(tid), (host, port))
            data, _ = sock.recvfrom(2048)
            conn_id = _parse_connect_response(data, tid)
            if conn_id is None:
                return None
            tid2 = random.getrandbits(31)
            sock.sendto(_build_scrape_request(conn_id, tid2, ih),
                        (host, port))
            data, _ = sock.recvfrom(2048)
            parsed = _parse_scrape_response(data, tid2)
            if parsed is None:
                return None
            seeders, _completed, leechers = parsed
            return (max(0, seeders), max(0, leechers))
        finally:
            sock.close()
    except Exception:
        return None


async def check_health(infohash_hex: str,
                      timeout: float = _EACH_TIMEOUT) -> dict:
    """Scrape public trackers for live seeders/leechers.

    Returns {"seeders", "leechers", "trackers_ok", "trackers_tried",
             "verdict"} where verdict is one of "alive" | "dead" | "unknown".
    "unknown" = no tracker answered (fall back to the normal DHT path).
    Seeders are the max across responding trackers (avoids double counting
    the same swarm seen by several trackers).
    """
    try:
        ih = bytes.fromhex(infohash_hex)
    except ValueError:
        return {"seeders": 0, "leechers": 0, "trackers_ok": 0,
                "trackers_tried": 0, "verdict": "unknown"}
    if len(ih) != 20:
        return {"seeders": 0, "leechers": 0, "trackers_ok": 0,
                "trackers_tried": 0, "verdict": "unknown"}
    results = await asyncio.gather(*[
        asyncio.to_thread(_scrape_one, host, port, ih, timeout)
        for host, port in _SCRAPE_TRACKERS
    ])
    ok = [r for r in results if r is not None]
    seeders = max((r[0] for r in ok), default=0)
    leechers = max((r[1] for r in ok), default=0)
    verdict = "unknown" if not ok else ("dead" if seeders == 0 else "alive")
    return {"seeders": seeders, "leechers": leechers,
            "trackers_ok": len(ok), "trackers_tried": len(_SCRAPE_TRACKERS),
            "verdict": verdict}


def dead_torrent_warning(h: dict) -> str:
    return (
        "⚠️ Seeder မတွေ့ပါ — ဒီ torrent dead ဖြစ်နိုင်ပါတယ်.\n"
        f"Trackers: {h['trackers_ok']}/{h['trackers_tried']} · "
        f"seeders: {h['seeders']} · leechers: {h['leechers']}\n\n"
        "Metadata ရဖို့ မိနစ်အတော်ကြာစောင့်ရနိုင်ပါတယ် (သို့) လုံးဝရမှာမဟုတ်ပါ — "
        "ဒီတိုင်း ဆက်လုပ်မလား?\n\n"
        "⚠️ No live seeders found — this torrent may be dead.\n"
        f"Trackers: {h['trackers_ok']}/{h['trackers_tried']} · "
        f"seeders: {h['seeders']} · leechers: {h['leechers']}\n\n"
        "Fetching metadata may take minutes or never succeed — "
        "proceed anyway?"
    )

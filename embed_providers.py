#!/usr/bin/env python3
"""Embed-provider extractors: movie/series -> direct .m3u8/.mp4 URLs.

Dependencies: Python stdlib + httpx ONLY. No browser, no crypto packages.
AES (CBC/GCM) and the VidEasy PRNG are implemented in pure Python below.

Documented 2026-09-27 from public sources (luffy, e2iplayer, flyx-main,
streamapp-rdn). Flows are transcribed, not live-verified — confirm once
from the deployment network before wiring into the bot (datacenter-IP
403s are the most common surprise).

Usage:
    import httpx, providers
    c = httpx.Client(headers={"User-Agent": providers.UA}, timeout=20,
                     follow_redirects=True)
    res = providers.extract_vidnest(c, 550)                      # movie
    res = providers.extract_vidnest(c, 1396, "tv", 1, 1)          # series
    # res -> {"sources": [{"url","quality","type","referer"}],
    #          "subtitles": [{"url","lang","label"}]}

Self-test (crypto vectors only, no network):
    python3 providers.py --selftest
"""

import base64
import hashlib
import hmac
import json
import re
import sys
from urllib.parse import quote, urlencode

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_M32 = 0xFFFFFFFF


# =====================================================================
# Pure-Python AES (encrypt + decrypt, 128/192/256) + CBC + GCM
# =====================================================================

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16")
_INV_SBOX = bytes.fromhex(
    "52096ad53036a538bf40a39e81f3d7fb"
    "7ce339829b2fff87348e4344c4dee9cb"
    "547b9432a6c2233dee4c950b42fac34e"
    "082ea16628d924b2765ba2496d8bd125"
    "72f8f66486689816d4a45ccc5d65b692"
    "6c704850fdedb9da5e154657a78d9d84"
    "90d8ab008cbcd30af7e45805b8b34506"
    "d02c1e8fca3f0f02c1afbd0301138a6b"
    "3a9111414f67dcea97f2cfcef0b4e673"
    "96ac7422e7ad3585e2f937e81c75df6e"
    "47f11a711d29c5896fb7620eaa18be1b"
    "fc563e4bc6d279209adbc0fe78cd5af4"
    "1fdda8338807c731b11210592780ec5f"
    "60517fa919b54a0d2de57a9f93c99cef"
    "a0e03b4dae2af5b0c8ebbb3c83539961"
    "172b047eba77d626e169146355210c7d")
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _sub_word(w):
    return ((_SBOX[(w >> 24) & 0xFF] << 24) | (_SBOX[(w >> 16) & 0xFF] << 16) |
            (_SBOX[(w >> 8) & 0xFF] << 8) | _SBOX[w & 0xFF])


def _rot_word(w):
    return ((w << 8) & _M32) | (w >> 24)


def _expand_key(key: bytes):
    nk, nr = len(key) // 4, len(key) // 4 + 6
    w = [int.from_bytes(key[i * 4:(i + 1) * 4], "big") for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = w[i - 1]
        if i % nk == 0:
            t = _sub_word(_rot_word(t)) ^ (_RCON[i // nk - 1] << 24)
        elif nk > 6 and i % nk == 4:
            t = _sub_word(t)
        w.append(w[i - nk] ^ t)
    rks = [b"".join(w[4 * r + c].to_bytes(4, "big") for c in range(4))
           for r in range(nr + 1)]
    return rks, nr


def _xtime(a):
    return (((a << 1) ^ 0x1B) & 0xFF) if a & 0x80 else (a << 1) & 0xFF


def _gmul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _shift_rows(s: bytes) -> bytes:
    return bytes(s[row + 4 * ((col + row) & 3)]
                 for col in range(4) for row in range(4))


def _inv_shift_rows(s: bytes) -> bytes:
    return bytes(s[row + 4 * ((col - row) & 3)]
                 for col in range(4) for row in range(4))


def _mix_columns(s: bytes) -> bytes:
    o = bytearray(16)
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        o[4 * c:4 * c + 4] = bytes((
            _xtime(a0) ^ (_xtime(a1) ^ a1) ^ a2 ^ a3,
            a0 ^ _xtime(a1) ^ (_xtime(a2) ^ a2) ^ a3,
            a0 ^ a1 ^ _xtime(a2) ^ (_xtime(a3) ^ a3),
            (_xtime(a0) ^ a0) ^ a1 ^ a2 ^ _xtime(a3)))
    return bytes(o)


def _inv_mix_columns(s: bytes) -> bytes:
    o = bytearray(16)
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        o[4 * c:4 * c + 4] = bytes((
            _gmul(a0, 0x0E) ^ _gmul(a1, 0x0B) ^ _gmul(a2, 0x0D) ^ _gmul(a3, 0x09),
            _gmul(a0, 0x09) ^ _gmul(a1, 0x0E) ^ _gmul(a2, 0x0B) ^ _gmul(a3, 0x0D),
            _gmul(a0, 0x0D) ^ _gmul(a1, 0x09) ^ _gmul(a2, 0x0E) ^ _gmul(a3, 0x0B),
            _gmul(a0, 0x0B) ^ _gmul(a1, 0x0D) ^ _gmul(a2, 0x09) ^ _gmul(a3, 0x0E)))
    return bytes(o)


class _AES:
    def __init__(self, key: bytes):
        if len(key) not in (16, 24, 32):
            raise ValueError("bad key length")
        self.rks, self.nr = _expand_key(key)

    def encrypt_block(self, pt: bytes) -> bytes:
        s = bytes(a ^ b for a, b in zip(pt, self.rks[0]))
        for r in range(1, self.nr):
            s = bytes(a ^ b for a, b in zip(
                _mix_columns(_shift_rows(bytes(_SBOX[x] for x in s))),
                self.rks[r]))
        return bytes(a ^ b for a, b in zip(
            _shift_rows(bytes(_SBOX[x] for x in s)), self.rks[self.nr]))

    def decrypt_block(self, ct: bytes) -> bytes:
        s = bytes(a ^ b for a, b in zip(ct, self.rks[self.nr]))
        for r in range(self.nr - 1, 0, -1):
            s = bytes(a ^ b for a, b in zip(
                _inv_shift_rows(bytes(_INV_SBOX[x] for x in s)),
                self.rks[r]))
            s = _inv_mix_columns(s)
        return bytes(a ^ b for a, b in zip(
            _inv_shift_rows(bytes(_INV_SBOX[x] for x in s)), self.rks[0]))


def aes_cbc_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("cbc data not block-aligned")
    a, out, prev = _AES(key), bytearray(), iv
    for i in range(0, len(data), 16):
        blk = bytes(x ^ y for x, y in zip(data[i:i + 16], prev))
        prev = a.encrypt_block(blk)
        out += prev
    return bytes(out)


def aes_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("cbc data not block-aligned")
    a, out, prev = _AES(key), bytearray(), iv
    for i in range(0, len(data), 16):
        blk = data[i:i + 16]
        out += bytes(x ^ y for x, y in zip(a.decrypt_block(blk), prev))
        prev = blk
    return bytes(out)


def pkcs7_pad(d: bytes, bs: int = 16) -> bytes:
    n = bs - len(d) % bs
    return d + bytes([n]) * n


def pkcs7_unpad(d: bytes) -> bytes:
    n = d[-1]
    if not 1 <= n <= 16 or d[-n:] != bytes([n]) * n:
        raise ValueError("bad pkcs7 padding")
    return d[:-n]


def _ghash_mul(x: int, y: int) -> int:
    r = 0
    for _ in range(128):
        if (y >> 127) & 1:
            r ^= x
        lsb = x & 1
        x >>= 1
        if lsb:
            x ^= (0xE1 << 120)
        y = (y << 1) & ((1 << 128) - 1)
    return r


def aes_gcm_decrypt(nonce12: bytes, ct_and_tag: bytes, key: bytes,
                    aad: bytes = b"") -> bytes:
    """AES-GCM decrypt (12-byte nonce). Raises ValueError on tag mismatch."""
    if len(nonce12) != 12 or len(ct_and_tag) < 16:
        raise ValueError("bad gcm input")
    a = _AES(key)
    h = int.from_bytes(a.encrypt_block(b"\x00" * 16), "big")
    ct, tag = ct_and_tag[:-16], ct_and_tag[-16:]
    j0 = nonce12 + b"\x00\x00\x00\x01"
    out, ctr = bytearray(), int.from_bytes(j0, "big")
    for i in range(0, len(ct), 16):
        ctr += 1
        ks = a.encrypt_block(ctr.to_bytes(16, "big"))
        blk = ct[i:i + 16]
        out += bytes(x ^ y for x, y in zip(blk, ks))
    y = 0
    for chunk in (aad, ct):
        pad = chunk + b"\x00" * (-len(chunk) % 16)
        for i in range(0, len(pad), 16):
            y = _ghash_mul(y ^ int.from_bytes(pad[i:i + 16], "big"), h)
    y = _ghash_mul(y ^ ((len(aad) * 8) << 64 | (len(ct) * 8)), h)
    expect = (int.from_bytes(a.encrypt_block(j0), "big") ^ y).to_bytes(16, "big")
    if not hmac.compare_digest(expect, tag):
        raise ValueError("GCM tag mismatch")
    return bytes(out)


# =====================================================================
# VidEasy PRNG (verbatim port of the player-side decryptor)
# =====================================================================

_VE_F = [1116352408, 1899447441, 3049323471, 642507279, 1996953984, 1482362762,
         2456739939, 2873987083, 2077488179, 2893834134, 3263035621, 1997028328,
         3585070361, 4287887244, 3699594741, 2581712021]
_VE_MAGIC = b"mvm1"


def _imul(a, b):
    return ((a & _M32) * (b & _M32)) & _M32


def _ve_mix(e):
    e &= _M32
    e ^= e >> 16
    e = _imul(e, 2246822507)
    e ^= e >> 13
    e = _imul(e, 3266489909)
    e ^= e >> 16
    return e & _M32


def _ve_rotl(e, t):
    e, t = e & _M32, t & 31
    return e if t == 0 else ((e << t) | (e >> (32 - t))) & _M32


def _ve_fnv1a(s):
    t = 2166136261
    for ch in s:
        t = _imul(t ^ ord(ch), 16777619)
    return _ve_mix(t)


def _ve_acc_seed(s):
    t = 1732584193
    for i, ch in enumerate(s):
        t = _ve_rotl((t ^ _imul(ord(ch), _VE_F[i & 15])) & _M32, 5)
    return _ve_mix(t)


def _ve_build_state(seed, media_id):
    # NOTE: faithful port. The reference's triangular-parity helpers make
    # (n*(n+1))&1 always 0, so the RC4 branch is unreachable and every loop
    # iteration takes the t-branch. Mirrored exactly; the mvm1 magic check
    # validates the port on every real payload.
    s, assigned = [None] * 61, set()
    a = _ve_mix(_ve_fnv1a(seed) ^ _ve_mix((int(media_id) ^ 2654435769) & _M32))
    for e in range(8):
        t = a % 61
        a = _ve_rotl((a + 2654435769) & _M32, 7 + (7 & e))
        s[t] = (a ^ _ve_mix(a)) & _M32
        assigned.add(t)
        a = _ve_mix((a + t) & _M32)
    return [s, _ve_mix((2779096485 ^ a) & _M32), assigned]


def _ve_next_word(st, counter):
    s, acc, assigned = st
    n = acc % 61
    i = -1 if n in assigned else 0
    lv = (s[n] or 0) & _M32
    a = (lv ^ _imul(2654435769, counter + 1)) & _M32
    d = ((acc ^ a) | ((acc & a & i) & _M32)) & _M32
    d = (_ve_rotl((d + acc) & _M32, 31 & n) ^
         _ve_rotl(acc, 31 & _imul(n, 7))) & _M32
    acc = _ve_mix((d + 2654435769) & _M32)
    s[n] = acc
    assigned.add(n)
    st[1] = acc
    return acc


def _ve_keystream(seed, media_id, n):
    st = _ve_build_state(seed, media_id)
    out, counter = bytearray(), 0
    while len(out) < n:
        t = _ve_next_word(st, counter)
        counter += 1
        out += bytes((t & 0xFF, (t >> 8) & 0xFF,
                      (t >> 16) & 0xFF, (t >> 24) & 0xFF))
    return bytes(out[:n])


def videasy_decrypt_payload(payload_b64: str, seed: str, media_id) -> dict:
    """Decrypt a VidEasy /sources-with-title payload -> {"sources", "subtitles"}."""
    raw_b64 = payload_b64.replace("-", "+").replace("_", "/")
    raw = base64.b64decode(raw_b64 + "=" * (-len(raw_b64) % 4))
    dec = bytes(x ^ y for x, y in zip(raw, _ve_keystream(seed, media_id, len(raw))))
    if dec[:4] != _VE_MAGIC:
        raise ValueError("videasy: bad magic — seed or port mismatch")
    return json.loads(dec[4:].decode("utf-8"))


# =====================================================================
# VidNest custom-base64 + Hunter decoder + Dean-Edwards unpacker
# =====================================================================

_VN_ALPHABET = ("RB0fpH8ZEyVLkv7c2i6MAJ5u3IKFDxlS1NTsnGaqmXYdUrtzjwObCgQP94hoeW+/=")
_VN_TRANS = str.maketrans(_VN_ALPHABET,
                          "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")


def vidnest_b64decode(s: str) -> str:
    """VidNest's shuffled-alphabet base64 (same bit layout, alphabet only)."""
    std = s.translate(_VN_TRANS)
    return base64.b64decode(std + "=" * (-len(std) % 4)).decode("utf-8")


_HUNTER_RE = re.compile(
    r"eval\(function\(h,u,n,t,e,r\).*?\}\('(.+?)',\s*(\d+),\s*'(.+?)',\s*(\d+),\s*(\d+),\s*(\d+)\)",
    re.S)


def hunter_decode(html: str):
    """Hunter-obfuscation decoder (multiembed.mov / SuperEmbed style).

    eval(function(h,u,n,t,e,r){...}('ENC', SEP, 'CHARSET', OFF, BASE1, BASE2))
    -> for each token split on chr(SEP): value = sum(charset.index(ch) * BASE1**k) - OFF
    Returns the decoded JS/HTML string, or None if no match.
    """
    m = _HUNTER_RE.search(html)
    if not m:
        return None
    encoded, sep, charset = m.group(1), int(m.group(2)), m.group(3)
    off, base1 = int(m.group(4)), int(m.group(5))
    out = []
    for tok in encoded.split(chr(sep)):
        if not tok:
            continue
        v = 0
        for ch in tok:
            i = charset.find(ch)
            if i >= 0:
                v = v * base1 + i
        v -= off
        if 0 < v < 0x110000:
            out.append(chr(v))
    return "".join(out)


_PACKER_RE = re.compile(
    r"eval\(function\(p,a,c,k,e,(?:d|r)\)\{.*?\}\('(.*)',(\d+),(\d+),'(.*)'\.split\('\|'\)",
    re.S)


def packer_unpack(html: str):
    """Dean Edwards p,a,c,k,e,d unpacker (2Embed Swish chain)."""
    m = _PACKER_RE.search(html)
    if not m:
        return None
    p, a, c, k = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|")
    digs = "0123456789abcdefghijklmnopqrstuvwxyz"

    def base_n(n, base):
        if n == 0:
            return "0"
        s = ""
        while n:
            s = digs[n % base] + s
            n //= base
        return s

    for i in range(c - 1, -1, -1):
        if i < len(k) and k[i]:
            p = re.sub(r"\b" + base_n(i, a) + r"\b", k[i], p)
    return p


def _balanced_json(text: str, start: int):
    """Extract the {...} or [...] JSON value starting at text[start]."""
    open_c, close_c = text[start], "}" if text[start] == "{" else "]"
    depth, i, instr, esc = 0, start, False, False
    while i < len(text):
        ch = text[i]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        elif ch == '"':
            instr = True
        elif ch == open_c:
            depth += 1
        elif ch == close_c:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return None


def _extract_js_obj(html: str, varname: str):
    """Grab `var {varname} = {...}` / `= [...]` from page JS."""
    m = re.search(r"var\s+" + re.escape(varname) + r"\s*=\s*", html)
    if not m:
        return None
    i = m.end()
    while i < len(html) and html[i] not in "{[":
        i += 1
    if i >= len(html):
        return None
    frag = _balanced_json(html, i)
    if not frag:
        return None
    try:
        return json.loads(frag)
    except Exception:
        return None


# =====================================================================
# Provider extractors (each: client, tmdb_id, media_type, season, episode)
# =====================================================================

def _need_httpx():
    if httpx is None:
        raise RuntimeError("httpx is required")


def _mkresult():
    return {"sources": [], "subtitles": []}


def _add_src(out, url, quality=None, referer=None, origin=None):
    if not url:
        return
    if url.startswith("//"):
        url = "https:" + url
    low = url.lower()
    typ = "hls" if ".m3u8" in low else ("mp4" if ".mp4" in low else "unknown")
    out["sources"].append({"url": url, "quality": quality or "auto",
                           "type": typ, "referer": referer, "origin": origin})


# ---------------- VidNest ----------------

_VIDNEST_SERVERS = ["moviebox", "allmovies", "purstream", "hollymoviehd",
                    "vidlink", "onehd", "klikxxi"]


def _vidnest_links(server, root):
    links = []
    if server == "moviebox":
        for s in root.get("url", []) or []:
            links.append((s.get("link"), s.get("resolution")))
    elif server in ("allmovies", "delta"):
        for s in root.get("streams", []) or []:
            links.append((s.get("url"), s.get("language")))
    elif server == "hollymoviehd":
        for s in root.get("sources", []) or []:
            links.append((s.get("file"), s.get("label")))
    elif server in ("purstream", "klikxxi"):
        for s in root.get("sources", []) or []:
            links.append((s.get("url"), s.get("quality") or s.get("name")))
    elif server == "vidlink":
        pl = (((root.get("data") or {}).get("stream") or {}).get("playlist"))
        links.append((pl, "auto"))
    elif server == "onehd":
        links.append((root.get("url"), "auto"))
    else:  # catflix / lamda / flixhq — generic scan
        for m in re.finditer(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', json.dumps(root)):
            links.append((m.group(0), "auto"))
    return links


def extract_vidnest(client, tmdb_id, media_type="movie", season=None, episode=None):
    """VidNest aggregator. Headers: Referer/Origin https://vidnest.fun/."""
    _need_httpx()
    out = _mkresult()
    ref = "https://vidnest.fun/"
    h = {"Referer": ref, "Origin": ref}
    is_tv = media_type == "tv" and season and episode
    for server in _VIDNEST_SERVERS:
        if is_tv:
            url = f"https://new.vidnest.fun/{server}/tv/{tmdb_id}/{season}/{episode}"
        else:
            url = f"https://new.vidnest.fun/{server}/movie/{tmdb_id}"
        if server == "onehd":
            url += "?server=upcloud"
        try:
            r = client.get(url, headers=h, timeout=12)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            resp = r.json()
            payload = resp.get("data")
            if not payload:
                continue
            if resp.get("encrypted"):
                payload = vidnest_b64decode(payload)
            root = json.loads(payload)
        except Exception:
            continue
        for u, label in _vidnest_links(server, root):
            _add_src(out, u, label, ref, ref)
    return out


# ---------------- VixSrc ----------------

def extract_vixsrc(client, tmdb_id, media_type="movie", season=None, episode=None,
                   domain="vixsrc.to"):
    """VixSrc JSON API + embed scrape. Returns None on 404 (not catalogued)."""
    _need_httpx()
    base = f"https://{domain}"
    kind = "tv" if media_type == "tv" else "movie"
    suffix = f"/{season}/{episode}" if kind == "tv" and season and episode else ""
    referer = f"{base}/{kind}/{tmdb_id}{suffix}"
    r = client.get(f"{base}/api/{kind}/{tmdb_id}{suffix}",
                   headers={"Referer": referer}, timeout=12)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    src = (r.json() or {}).get("src")
    if not src:
        raise RuntimeError("vixsrc: api response has no embed src")
    embed_url = src if src.startswith("http") else base + src
    page = client.get(embed_url, headers={"Referer": referer}, timeout=12).text

    m = re.search(r"window\.masterPlaylist[^:]+params:[^{]+({[^<]+?})", page)
    if not m:
        raise RuntimeError("vixsrc: playlist params not found")
    raw = m.group(1)
    params = json.loads(re.sub(r",[^\"]+}", "}", raw.replace("'", '"')))
    if params.get("asn"):
        raise RuntimeError("vixsrc: playlist is ASN-signed — unusable server-side")

    m2 = re.search(r"window\.masterPlaylist\s*=\s*\{[\s\S]*?url:\s*'([^']+)'", page)
    if not m2:
        raise RuntimeError("vixsrc: playlist url not found")
    m3 = re.search(r"window\.canPlayFHD\s+?=\s+?(\w+)", page)
    fhd = bool(m3) and m3.group(1) == "true"

    sep = "&" if "?" in m2.group(1) else "?"
    master = (m2.group(1) + sep +
              urlencode({"expires": params["expires"], "token": params["token"]}))
    if fhd:
        master += "&h=1"
    out = _mkresult()
    _add_src(out, master, "fhd" if fhd else "auto", referer, base)
    return out


# ---------------- VidEasy ----------------

_VIDEASY_BACKENDS = [("/cdn/sources-with-title", "Yoru"),
                     ("/neon2/sources-with-title", "Neon"),
                     ("/m4uhd/sources-with-title", "Breach"),
                     ("/meine/sources-with-title", "Killjoy"),
                     ("/lamovie/sources-with-title", "Omen")]
_VE_PLAYER = "https://player.videasy.to/"


def extract_videasy(client, tmdb_id, media_type="movie", season=None, episode=None):
    """VidEasy SpeedRaceLight API with local PRNG decrypt (no relay)."""
    _need_httpx()
    h = {"Referer": _VE_PLAYER, "Origin": _VE_PLAYER,
         "Accept": "application/json, text/plain, */*"}
    kind = "tv" if media_type == "tv" else "movie"
    meta = client.get(
        f"https://db.speedracelight.com/3/{kind}/{tmdb_id}?append_to_response=external_ids",
        headers=h, timeout=12).json()
    title = (meta.get("title") or meta.get("name") or
             meta.get("original_title") or str(tmdb_id))
    year = (meta.get("release_date") or meta.get("first_air_date") or "")[:4]
    imdb = meta.get("imdb_id") or (meta.get("external_ids") or {}).get("imdb_id") or ""
    seed = client.get(f"https://api.speedracelight.com/seed?mediaId={tmdb_id}",
                      headers=h, timeout=12).json()["seed"]

    out = _mkresult()
    title_q = quote(quote(title, safe=""), safe="")  # double-encoded on the wire
    for path, label in _VIDEASY_BACKENDS:
        q = {"mediaType": kind, "tmdbId": str(tmdb_id), "imdbId": imdb,
             "episodeId": str(episode or 1), "seasonId": str(season or 1),
             "enc": "2", "seed": seed}
        if year:
            q["year"] = year
        qs = "title=" + title_q + "&" + urlencode(q)
        try:
            r = client.get(f"https://api.speedracelight.com{path}?{qs}",
                           headers=h, timeout=15)
        except Exception:
            continue
        payload = r.text.strip()
        if payload.startswith('"') and payload.endswith('"'):
            try:
                payload = json.loads(payload)
            except Exception:
                continue
        if payload.startswith("{"):  # error envelope (e.g. stale seed)
            continue
        try:
            data = videasy_decrypt_payload(payload, seed, int(tmdb_id))
        except Exception:
            continue
        for s in data.get("sources", []) or []:
            _add_src(out, s.get("url") or s.get("file"),
                     s.get("quality") or label, _VE_PLAYER, _VE_PLAYER)
        for t in data.get("subtitles", []) or []:
            u = t.get("url") or t.get("file")
            if u:
                out["subtitles"].append(
                    {"url": u, "lang": t.get("lang") or t.get("language") or "",
                     "label": t.get("label") or t.get("lang") or ""})
    return out


# ---------------- VidRock ----------------

_VIDROCK_KEY = bytes.fromhex(
    "7f3e9c2a8b5d1f4e6a9c3b7d2e5f8a1c4b6d9e2f5a8c1b4d7e9f2a5c8b1d4e7f")


def extract_vidrock(client, tmdb_id, media_type="movie", season=None, episode=None):
    """VidRock .net API: per-server AES-256-GCM blobs. Needs numeric tmdb id."""
    _need_httpx()
    out = _mkresult()
    kind = "tv" if media_type == "tv" else "movie"
    suffix = f"/{season}/{episode}" if kind == "tv" and season and episode else ""
    r = client.get(f"https://vidrock.net/api/{kind}/{tmdb_id}{suffix}",
                   headers={"Referer": "https://vidrock.net/"}, timeout=12)
    r.raise_for_status()
    for name, info in (r.json() or {}).items():
        enc = (info or {}).get("url")
        if not enc:
            continue
        blob = base64.b64decode(enc.replace("-", "+").replace("_", "/") +
                                "=" * (-len(enc) % 4))
        try:
            url = aes_gcm_decrypt(blob[:12], blob[12:], _VIDROCK_KEY
                                  ).decode("utf-8", "ignore").strip()
        except Exception:
            continue
        _add_src(out, url, name, "https://vidrock.net/", "https://vidrock.net/")
    # subtitles (documented for tv; movie path is best-effort)
    if kind == "tv" and season and episode:
        sub_url = f"https://sub.vdrk.site/v2/tv/{tmdb_id}/{season}/{episode}"
    else:
        sub_url = f"https://sub.vdrk.site/v2/movie/{tmdb_id}"
    try:
        subs = client.get(sub_url, timeout=12).json()
        for t in subs if isinstance(subs, list) else []:
            if isinstance(t, dict) and t.get("file"):
                out["subtitles"].append(
                    {"url": t["file"], "lang": "", "label": t.get("label", "")})
    except Exception:
        pass
    return out


# ---------------- 2Embed ----------------

def _resolve_2embed_chain(client, xps_url):
    """XPS branch: page -> backups/data/playlist.json -> m3u8/mp4 + subs."""
    out = _mkresult()
    origin_m = re.match(r"(https://[^/]+)", xps_url)
    origin = origin_m.group(1) if origin_m else "https://play.xpass.top"
    html = client.get(xps_url, headers={"Referer": "https://streamsrcs.2embed.cc/"},
                      timeout=15).text
    backups = _extract_js_obj(html, "backups") or []
    data = _extract_js_obj(html, "data") or {}
    suburl_m = re.search(r'var\s+suburl\s*=\s*"([^"]+)"', html)
    dataurl_m = re.search(r'var\s+dataUrl\s*=\s*"([^"]+)"', html)

    pl_paths = []
    pl = data.get("playlist")
    if pl:
        pl_paths.append(pl if pl.startswith("http") else origin + pl)
    for b in backups[:8]:
        u = (b or {}).get("url")
        if u:
            pl_paths.append(u if u.startswith("http") else origin + u)
    h = {"Referer": xps_url, "Origin": origin}
    for pl_url in pl_paths:
        try:
            pl_json = client.get(pl_url, headers=h, timeout=12).json()
        except Exception:
            continue
        for entry in pl_json.get("playlist", []) or []:
            for s in entry.get("sources", []) or []:
                f = s.get("file", "")
                if "/video/error" in f or "/error" in f:
                    continue
                _add_src(out, f, s.get("label"), "https://play.xpass.top/",
                         "https://play.xpass.top/")
        if out["sources"]:
            break
    if suburl_m:
        try:
            sj = client.get(suburl_m.group(1), timeout=12).json()
            tracks = sj if isinstance(sj, list) else sj.get("subtitles", [])
            for t in tracks:
                u = t.get("file") or t.get("url")
                if u:
                    out["subtitles"].append(
                        {"url": u, "lang": t.get("lang") or "",
                         "label": t.get("label") or t.get("language") or ""})
        except Exception:
            pass
    return out


def extract_2embed(client, tmdb_id, media_type="movie", season=None, episode=None,
                   imdb_id=None):
    """2Embed embed page -> server chain. XPS branch implemented; Swish unpacker
    available via packer_unpack(); vesy delegates to extract_videasy()."""
    _need_httpx()
    if media_type == "tv":
        embed_path = f"/embedtv/{tmdb_id}&s={season or 1}&e={episode or 1}"
    else:
        if not imdb_id:
            try:
                imdb_id = client.get(
                    f"https://api.2embed.cc/movie?tmdb_id={tmdb_id}",
                    timeout=12).json().get("imdb_id")
            except Exception:
                imdb_id = None
        embed_path = f"/embed/{imdb_id or tmdb_id}"
    html = client.get(f"https://www.2embed.cc{embed_path}",
                      headers={"Referer": "https://www.2embed.cc/"},
                      timeout=15).text

    chains = []
    for m in re.finditer(
            r'''onclick="go\('(https://streamsrcs\.2embed\.cc/([^?'\s]+)\?[^']*)'\)''',
            html):
        chains.append((m.group(2), m.group(1)))
    m_if = re.search(r'<iframe[^>]+data-src="([^"]+)"', html)
    if m_if:
        chains.append(("iframe", m_if.group(1)))
    chains.sort(key=lambda c: {"xps": 0, "swish": 1, "vesy": 2,
                               "vcr": 3}.get(c[0], 9))

    for ctype, url in chains:
        if ctype == "xps":
            mm = re.search(r"/e/(movie|tv)/(.+?)(?:\?|$)", url)
            if not mm:
                continue
            if mm.group(1) == "movie":
                xps_url = f"https://play.xpass.top/e/movie/{mm.group(2)}?autostart=true"
            else:
                parts = mm.group(2).split("/")
                xps_url = (f"https://play.xpass.top/e/tv/{parts[0]}/"
                           f"{parts[1] if len(parts) > 1 else season or 1}/"
                           f"{parts[2] if len(parts) > 2 else episode or 1}"
                           "?autostart=true")
            out = _resolve_2embed_chain(client, xps_url)
        elif ctype == "swish":
            mm = re.search(r"2vcdn\.skin/e/([^\?'\"]+)", url)
            out = _mkresult()
            if mm:
                try:
                    pg = client.get(
                        f"https://2vcdn.skin/e/{mm.group(1)}",
                        headers={"Referer": "https://streamsrcs.2embed.cc/"},
                        timeout=15).text
                    js = packer_unpack(pg) or ""
                    for f in re.findall(r'https?://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*', js):
                        _add_src(out, f, "auto", "https://2vcdn.skin/",
                                 "https://2vcdn.skin/")
                except Exception:
                    pass
        elif ctype == "vesy":
            out = extract_videasy(client, tmdb_id, media_type, season, episode)
        else:
            out = _mkresult()  # vcr/vidcore.net chain: relay-only, skipped
        if out["sources"]:
            return out
    return _mkresult()


# ---------------- VidCore.org aggregator ----------------

def extract_vidcore_org(client, tmdb_id, media_type="movie", season=None, episode=None):
    """www.vidcore.org/api/sources — multi-round with skip= labels."""
    _need_httpx()
    out = _mkresult()
    base = "https://www.vidcore.org"
    params = {"id": str(tmdb_id), "type": "tv" if media_type == "tv" else "movie"}
    if media_type == "tv" and season and episode:
        params.update({"season": str(season), "episode": str(episode)})
    h = {"Referer": f"{base}/embed/movie/{tmdb_id}", "Origin": base,
         "Accept": "application/json"}
    skip = []
    for _ in range(4):
        q = dict(params)
        if skip:
            q["skip"] = ",".join(skip)
        try:
            r = client.get(base + "/api/sources", params=q, headers=h, timeout=15)
        except Exception:
            break
        if r.status_code != 200:
            break
        try:
            data = r.json()
        except Exception:
            break
        for src in data.get("sources", []) or []:
            label = src.get("label", "")
            skip.append(label)
            for s in ((src.get("data") or {}).get("sources") or []):
                hdrs = s.get("headers") or {}
                _add_src(out, s.get("url"), s.get("quality") or label,
                         hdrs.get("Referer"), hdrs.get("Origin"))
            for t in ((src.get("data") or {}).get("subtitles") or []):
                u = t.get("url") or t.get("file")
                if u:
                    out["subtitles"].append(
                        {"url": u, "lang": t.get("lang") or t.get("language") or "",
                         "label": t.get("label") or ""})
    return out


# ---------------- VidSrc family (cloudnestra /rcp/) ----------------

def extract_vidsrc_cloudnestra(client, embed_url):
    """vidsrc.xyz/.to/.fyi/.mov embed -> cloudnestra /rcp/ -> /prorcp/ -> file.

    Raises RuntimeError('turnstile') if Cloudflare Turnstile is detected.
    embed_url example: https://vidsrc.xyz/embed/movie/{tmdb_id}
    """
    _need_httpx()
    out = _mkresult()
    page = client.get(embed_url, timeout=15).text
    cur = embed_url
    for _ in range(5):  # follow nested iframes
        m = re.search(r'<iframe[^>]+src="([^"]+)"', page)
        if not m:
            break
        cur = m.group(1)
        if cur.startswith("//"):
            cur = "https:" + cur
        page = client.get(cur, headers={"Referer": embed_url}, timeout=15).text

    m = re.search(r'''(?:src|href)=["'](?:https?:)?//(cloudnestra\.com/rcp/[^"'?#]+)''',
                  page)
    if not m:
        raise RuntimeError("vidsrc: cloudnestra /rcp/ link not found")
    rcp = "https://" + m.group(1)
    rcp_page = client.get(rcp, headers={"Referer": cur}, timeout=15).text
    if "cf-turnstile" in rcp_page or "/rcp_verify" in rcp_page:
        raise RuntimeError("turnstile")
    m2 = re.search(r'''src:\s*['"]/prorcp/([^'"]+)['"]''', rcp_page)
    if not m2:
        raise RuntimeError("vidsrc: /prorcp/ token not found")
    pro = client.get(f"https://cloudnestra.com/prorcp/{m2.group(1)}",
                     headers={"Referer": "https://cloudnestra.com/"},
                     timeout=15).text
    m3 = re.search(r'''file:\s*"(https://[^"]+)"''', pro)
    if not m3:
        raise RuntimeError("vidsrc: file url not found")
    url = m3.group(1).replace("{v1}", "cloudnestra.com") \
                      .replace("{v2}", "cloudnestra.com") \
                      .replace("{v3}", "cloudnestra.com") \
                      .replace("{v4}", "cloudnestra.com")
    url = url.split('" or "')[0]
    _add_src(out, url, "auto", rcp, "https://cloudnestra.com")
    # subtitles
    try:
        h2 = {"Referer": rcp, "X-Requested-With": "XMLHttpRequest"}
        subs = client.get(f"https://cloudnestra.com/ajax/embed/episode/"
                          f"{m.group(1).split('/')[-1]}/subtitles",
                          headers=h2, timeout=12).json()
        for t in subs if isinstance(subs, list) else []:
            if isinstance(t, dict) and t.get("file"):
                out["subtitles"].append(
                    {"url": t["file"], "lang": "",
                     "label": t.get("label", "")})
    except Exception:
        pass
    return out


# ---------------- VidLink (2025-era local AES flow) ----------------

_VIDLINK_KEY = bytes.fromhex(
    "2de6e6ea13a9df9503b11a6117fd7e51941e04a0c223dfeacfe8a1dbb6c52783")


def extract_vidlink(client, tmdb_id, media_type="movie", season=None, episode=None):
    """VidLink AES-256-CBC flow (documented 2025; current protocol may differ —
    2026 sources report XSalsa20-Poly1305 or the enc-dec.app relay)."""
    _need_httpx()
    iv = bytes(range(16))
    ct = aes_cbc_encrypt(pkcs7_pad(str(tmdb_id).encode()), _VIDLINK_KEY, iv)
    token = quote(base64.b64encode((iv.hex() + ":" + ct.hex()).encode()).decode(),
                  safe="")
    if media_type == "tv" and season and episode:
        url = f"https://vidlink.pro/api/b/tv/{token}/{season}/{episode}"
    else:
        url = f"https://vidlink.pro/api/b/movie/{token}"
    h = {"Referer": "https://vidlink.pro/", "Origin": "https://vidlink.pro"}
    body = client.get(url, headers=h, timeout=15).text
    parts = body.split(":")
    if len(parts) != 2:
        raise RuntimeError("vidlink: unexpected api response shape")
    data = json.loads(pkcs7_unpad(
        aes_cbc_decrypt(bytes.fromhex(parts[1]), _VIDLINK_KEY,
                        bytes.fromhex(parts[0]))).decode("utf-8", "ignore"))
    out = _mkresult()
    if data.get("playlist"):
        _add_src(out, data["playlist"], "auto", "https://vidlink.pro/",
                 "https://vidlink.pro/")
    for s in data.get("sources", []) or []:
        _add_src(out, s.get("file"), s.get("label") or s.get("quality"),
                 "https://vidlink.pro/", "https://vidlink.pro/")
    try:
        subs = client.get(f"https://vidlink.pro/api/subtitles/{tmdb_id}",
                          headers=h, timeout=12).json()
        for t in subs if isinstance(subs, list) else []:
            if isinstance(t, dict) and t.get("url"):
                out["subtitles"].append(
                    {"url": t["url"], "lang": "",
                     "label": t.get("label", "")})
    except Exception:
        pass
    return out


# =====================================================================
# Self-test: crypto vectors only (no network, no copyrighted content)
# =====================================================================

def _selftest():
    ok = 0

    def check(name, cond):
        nonlocal ok
        assert cond, "FAIL: " + name
        ok += 1
        print("ok:", name)

    # FIPS-197 vectors
    check("aes128 enc",
          _AES(bytes.fromhex("000102030405060708090a0b0c0d0e0f"))
          .encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff")).hex()
          == "69c4e0d86a7b0430d8cdb78070b4c55a")
    check("aes256 enc",
          _AES(bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"))
          .encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff")).hex()
          == "8ea2b7ca516745bfeafc49904b496089")
    a128 = _AES(bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c"))
    ct = a128.encrypt_block(bytes.fromhex("3243f6a8885a308d313198a2e0370734"))
    check("aes128 dec", a128.decrypt_block(ct).hex() == "3243f6a8885a308d313198a2e0370734")

    # CBC roundtrip
    k, iv = bytes(range(32)), bytes(range(16))
    raw_msg = b"hello vidrock test payload"
    msg = pkcs7_pad(raw_msg)
    check("cbc roundtrip",
          pkcs7_unpad(aes_cbc_decrypt(aes_cbc_encrypt(msg, k, iv), k, iv)) == raw_msg)

    # GCM: local encrypt (test-only) + decrypt roundtrip, H anchor check.
    # H = AES-128(0-key)(0-block) is the widely published 66e94bd4...
    def _gcm_encrypt(key, nonce12, pt, aad=b""):
        a = _AES(key)
        assert a.encrypt_block(b"\x00" * 16).hex() == \
            "66e94bd4ef8a2c3b884cfa59ca342b2e", "H anchor mismatch"
        h = int.from_bytes(a.encrypt_block(b"\x00" * 16), "big")
        j0 = nonce12 + b"\x00\x00\x00\x01"
        out, ctr = bytearray(), int.from_bytes(j0, "big")
        for i in range(0, len(pt), 16):
            ctr += 1
            ks = a.encrypt_block(ctr.to_bytes(16, "big"))
            blk = pt[i:i + 16]
            out += bytes(x ^ y for x, y in zip(blk, ks))
        y = 0
        for chunk in (aad, bytes(out)):
            pad = chunk + b"\x00" * (-len(chunk) % 16)
            for i in range(0, len(pad), 16):
                y = _ghash_mul(y ^ int.from_bytes(pad[i:i + 16], "big"), h)
        y = _ghash_mul(y ^ ((len(aad) * 8) << 64 | (len(bytes(out)) * 8)), h)
        tag = (int.from_bytes(a.encrypt_block(j0), "big") ^ y).to_bytes(16, "big")
        return bytes(out), tag

    _gkey, _gnonce = bytes(16), bytes(range(12))
    _gpt = b"vidrock gcm roundtrip, odd length!"
    _gct, _gtag = _gcm_encrypt(_gkey, _gnonce, _gpt, b"aad-bytes")
    check("gcm H anchor + roundtrip",
          aes_gcm_decrypt(_gnonce, _gct + _gtag, _gkey, b"aad-bytes") == _gpt)
    try:
        aes_gcm_decrypt(_gnonce, _gct + bytes([_gtag[0] ^ 1]) + _gtag[1:],
                        _gkey, b"aad-bytes")
        check("gcm tag reject", False)
    except ValueError:
        check("gcm tag reject", True)

    # VidEasy port roundtrip
    seed, mid = "ve-test-seed-123", 550
    plain = b"mvm1" + json.dumps({"sources": [{"url": "https://x/y.m3u8"}]},
                                 separators=(",", ":")).encode()
    ks = _ve_keystream(seed, mid, len(plain))
    payload = base64.b64encode(bytes(a ^ b for a, b in zip(plain, ks))).decode()
    check("videasy roundtrip",
          videasy_decrypt_payload(payload, seed, mid)["sources"][0]["url"]
          == "https://x/y.m3u8")
    check("videasy magic check",
          videasy_decrypt_payload(payload, seed, mid)["sources"][0]["url"]
          == "https://x/y.m3u8")

    # VidNest custom b64 roundtrip
    std = base64.b64encode(b'{"url":["https://a/b.m3u8"]}').decode().rstrip("=")
    vn = std.translate(str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",
        _VN_ALPHABET[:64]))
    check("vidnest b64", vidnest_b64decode(vn) == '{"url":["https://a/b.m3u8"]}')

    # Hunter roundtrip (synthetic)
    charset = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    sep, off, base1 = 31, 7, len(charset)
    secret = 'file:"https://cdn.example/v.m3u8"'
    toks = []
    for ch in secret:
        v = ord(ch) + off
        t = ""
        while v:
            t = charset[v % base1] + t
            v //= base1
        toks.append(t)
    html = ("eval(function(h,u,n,t,e,r){}('" + chr(sep).join(toks) + "'," +
            str(sep) + ",'" + charset + "'," + str(off) + "," + str(base1) + ",10)")
    check("hunter roundtrip", hunter_decode(html) == secret)

    # Packer unpack (synthetic)
    packed = ("eval(function(p,a,c,k,e,d){e=function(c){return c};"
              "while(c--)if(k[c])p=p.replace(new RegExp('\\\\b'+e(c)+'\\\\b','g'),k[c]);"
              "return p}('0 1',2,2,'file|https://x/y.m3u8'.split('|'),0,{})")
    check("packer unpack", packer_unpack(packed) == "file https://x/y.m3u8")

    print(f"\n{ok} checks passed")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)

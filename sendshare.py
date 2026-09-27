"""Download from Mozilla-Send-protocol instances (send.vis.ee).

Protocol summary (compatible with the `ffsend` CLI):
  share URL: https://send.vis.ee/download/<file-id>#<secret-key>
  1. GET /api/exists/<file-id> -> 404 = gone; WWW-Authenticate: send-v1 <nonce>
  2. auth_key = HKDF-SHA256(secret, salt=b"", info=b"authentication", L=64)
     Authorization: send-v1 <b64url(HMAC-SHA256(auth_key, nonce))>
  3. GET /api/metadata/<file-id> -> {"metadata": "<b64url>"}; decrypt with
     meta_key = HKDF(secret, b"", b"metadata", 16), AES-128-GCM, zero nonce.
     Plaintext JSON: {"name", "size", ...}.
  4. GET /api/download/<file-id> (streamed) -> RFC 8188 aes128gcm records,
     decrypted record-by-record; each download may consume the share.

Only stdlib + httpx + cryptography. Raises SendShareError on failure.
"""

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import struct
import urllib.parse


class SendShareError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind          # "dead" | "password" | "network" | "corrupt" | "too_big"
        self.message = message


def _b64url_decode(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def _b64url_encode_nopad(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _hkdf(ikm, salt, info, length):
    """RFC 5869 HKDF-SHA256 (stdlib only)."""
    if not salt:
        salt = b"\x00" * hashlib.sha256().digest_size
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, t, i = b"", b"", 0
    while len(okm) < length:
        i += 1
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
    return okm[:length]


def _parse_share_url(url):
    """Return (base_origin, file_id, secret_bytes)."""
    try:
        parts = urllib.parse.urlparse(url or "")
    except Exception:
        raise SendShareError("dead", "❌ Send link မမှန်ပါ.")
    frag = parts.fragment or ""
    # some clients paste the key after the last '#'; anything before is noise
    key = frag.split("#")[-1] if frag else ""
    m = re.fullmatch(r"[A-Za-z0-9\-_]{16,64}", key or "")
    # strip trailing slashes: ".../download/<fid>/" must work like ".../download/<fid>"
    fid = (parts.path.rstrip("/").rsplit("/", 1)[-1] or "").strip()
    if not m or not re.fullmatch(r"[A-Za-z0-9\-_]+", fid or ""):
        raise SendShareError(
            "dead",
            "❌ Send link မပြည့်စုံပါ — '#' နောက်က key ပါတဲ့ link အပြည့်အစုံ ပို့ပေးပါ.\n"
            "❌ Incomplete Send link — please send the full link including "
            "the key after '#'.")
    try:
        secret = _b64url_decode(key)
    except (binascii.Error, ValueError):
        raise SendShareError("dead", "❌ Send link key မမှန်ပါ.")
    if len(secret) != 16:
        raise SendShareError("dead", "❌ Send link key မမှန်ပါ.")
    origin = "%s://%s" % (parts.scheme or "https", parts.netloc)
    return origin, fid, secret


def _auth_header(secret, nonce_b64):
    auth_key = _hkdf(secret, b"", b"authentication", 64)
    try:
        nonce = _b64url_decode(nonce_b64)
    except (binascii.Error, ValueError):
        raise SendShareError("network", "❌ Send server တုံ့ပြန်ချက် မမှန်ပါ.")
    sig = hmac.new(auth_key, nonce, hashlib.sha256).digest()
    return {"Authorization": "send-v1 " + _b64url_encode_nopad(sig)}


def _fetch_nonce(client, origin, fid):
    r = client.get(origin + "/api/exists/" + fid)
    if r.status_code == 404:
        raise SendShareError(
            "dead",
            "❌ ဒီ Send link က သက်တမ်းကုန်ပြီ (သို့) ဖျက်လိုက်ပါပြီ.\n"
            "❌ This Send link has expired or was deleted.")
    r.raise_for_status()
    try:
        info = r.json()
    except Exception:
        info = {}
    if isinstance(info, dict) and info.get("requiresPassword"):
        raise SendShareError(
            "password",
            "🔑 ဒီ Send link က password တောင်းနေပါတယ် — bot က မဒေါင်းနိုင်ပါ.\n"
            "🔑 This Send link is password-protected — the bot cannot "
            "download it.")
    wa = r.headers.get("www-authenticate", "")
    m = re.search(r"send-v1\s+(\S+)", wa)
    if not m:
        raise SendShareError("network", "❌ Send server တုံ့ပြန်ချက် မမှန်ပါ.")
    return m.group(1)


def _decrypt_metadata(secret, blob_b64):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    meta_key = _hkdf(secret, b"", b"metadata", 16)
    try:
        blob = _b64url_decode(blob_b64)
    except (binascii.Error, ValueError):
        raise SendShareError("corrupt", "❌ Send metadata ဖတ်မရပါ.")
    if len(blob) < 17:
        raise SendShareError("corrupt", "❌ Send metadata ဖတ်မရပါ.")
    try:
        plain = AESGCM(meta_key).decrypt(b"\x00" * 12, blob, None)
    except Exception:
        raise SendShareError("corrupt", "❌ Send metadata decrypt မရပါ.")
    try:
        return json.loads(plain.decode("utf-8"))
    except Exception:
        raise SendShareError("corrupt", "❌ Send metadata ဖတ်မရပါ.")


def _record_keys(secret, salt):
    enc_key = _hkdf(secret, salt, b"Content-Encoding: aes128gcm\x00", 16)
    nonce_base = _hkdf(secret, salt, b"Content-Encoding: nonce\x00", 12)
    return enc_key, nonce_base


def _record_nonce(nonce_base, seq):
    prefix = nonce_base[:8]
    ctr = int.from_bytes(nonce_base[8:12], "big") ^ seq
    return prefix + ctr.to_bytes(4, "big")


def download_sendshare(url, tmpdir, max_mb=1900, progress_cb=None,
                       cancel_event=None):
    """Download a Send-protocol share. Returns (path, filename).

    progress_cb(done_bytes, total_bytes) — called from the download thread.
    cancel_event: threading.Event — aborts with WebDownloadCancelled.
    """
    import httpx
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from web_download import WebDownloadCancelled  # lazy: no cycle

    origin, fid, secret = _parse_share_url(url)
    client = httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, timeout=60,
                          trust_env=False)
    try:
        # metadata first: filename + size cap BEFORE consuming a download
        nonce = _fetch_nonce(client, origin, fid)
        headers = _auth_header(secret, nonce)
        r = client.get(origin + "/api/metadata/" + fid, headers=headers)
        if r.status_code == 401:
            # nonce is single-use — refresh once, then it is a password issue
            nonce = _fetch_nonce(client, origin, fid)
            headers = _auth_header(secret, nonce)
            r = client.get(origin + "/api/metadata/" + fid, headers=headers)
            if r.status_code == 401:
                raise SendShareError(
                    "password",
                    "🔑 ဒီ Send link က password တောင်းနေပါတယ် — bot က မဒေါင်းနိုင်ပါ.\n"
                    "🔑 This Send link is password-protected — the bot cannot "
                    "download it.")
        r.raise_for_status()
        try:
            meta_b64 = (r.json() or {}).get("metadata", "")
        except Exception:
            raise SendShareError("corrupt", "❌ Send metadata ဖတ်မရပါ.")
        meta = _decrypt_metadata(secret, meta_b64)
        name = str(meta.get("name") or "send_file")
        name = re.sub(r'[<>:\"/\\\\|?*\x00-\x1f]', "_", name).strip() or "send_file"
        size = int(meta.get("size") or 0)
        if size and size > max_mb * 1048576:
            raise SendShareError(
                "too_big",
                "❌ File ကြီးလွန်းပါတယ် (%.0fMB > %dMB).\n"
                "❌ File too large." % (size / 1048576, max_mb))

        # download + stream-decrypt (this call may consume the share)
        nonce = _fetch_nonce(client, origin, fid)
        headers = _auth_header(secret, nonce)
        path = os.path.join(tmpdir, name)
        with client.stream("GET", origin + "/api/download/" + fid,
                           headers=headers) as resp:
            if resp.status_code == 404:
                raise SendShareError(
                    "dead",
                    "❌ ဒီ Send link က သက်တမ်းကုန်ပြီ (သို့) ဖျက်လိုက်ပါပြီ.\n"
                    "❌ This Send link has expired or was deleted.")
            resp.raise_for_status()
            it = resp.iter_bytes(65536)
            buf = b""

            def _take(n):
                nonlocal buf
                while len(buf) < n:
                    try:
                        chunk = next(it)
                    except StopIteration:
                        break
                    if chunk:
                        buf += chunk
                out, buf = buf[:n], buf[n:]
                return out

            header = _take(21)
            if len(header) < 21:
                raise SendShareError("corrupt", "❌ Send file ပျက်နေပါတယ်.")
            salt = header[:16]
            rs = struct.unpack(">I", header[16:20])[0]
            if header[20:21] != b"\x00" or not (16 < rs <= 1024 * 1024):
                raise SendShareError("corrupt", "❌ Send file ပျက်နေပါတယ်.")
            enc_key, nonce_base = _record_keys(secret, salt)
            aesgcm = AESGCM(enc_key)
            done, seq = 0, 0
            with open(path, "wb") as f:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise WebDownloadCancelled(
                            WebDownloadCancelled.CANCEL_MSG)
                    rec = _take(rs)
                    if not rec:
                        raise SendShareError(
                            "corrupt", "❌ Send file ဒေါင်းရင်း ပြတ်သွားပါတယ်.")
                    try:
                        plain = aesgcm.decrypt(
                            _record_nonce(nonce_base, seq), rec, None)
                    except Exception:
                        raise SendShareError(
                            "corrupt", "❌ Send file decrypt မရပါ.")
                    if not plain:
                        raise SendShareError(
                            "corrupt", "❌ Send file ပျက်နေပါတယ်.")
                    delim = plain[-1:]
                    content = plain[:-1].rstrip(b"\x00")
                    f.write(content)
                    done += len(content)
                    if progress_cb:
                        try:
                            progress_cb(done, size)
                        except Exception:
                            pass
                    seq += 1
                    if delim == b"\x02":
                        break
                    if delim != b"\x01":
                        raise SendShareError(
                            "corrupt", "❌ Send file ပျက်နေပါတယ်.")
        return path, name
    except SendShareError:
        raise
    except Exception as e:
        raise SendShareError(
            "network", "❌ Send ဆက်သွယ်မရပါ: %s" % str(e)[:150])
    finally:
        try:
            client.close()
        except Exception:
            pass

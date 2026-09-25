"""X/Twitter media extraction without login.

Cascade (from x2t's approach, reimplemented):
  1. FxTwitter public API  (handles sensitive/age-restricted tweets)
  2. VxTwitter fallback
  3. X syndication CDN API (locally-computed token, no auth)

Also: profile timeline scraping via X GraphQL (guest token) for /xtimeline.
"""
import math
import re

# x2t's tweet URL pattern + /i/status/ support
TWEET_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|mobile\.|m\.)?(?:twitter\.com|x\.com)"
    r"/(?:#!/)?(?:[A-Za-z0-9_]+|i)/status/(?P<id>\d+)",
    re.IGNORECASE,
)
PROFILE_RE = re.compile(
    r"(?:https?://)?(?:www\.|mobile\.|m\.)?(?:twitter\.com|x\.com)"
    r"/(?:#!/)?(?P<user>[A-Za-z0-9_]{1,30})/?(?:[?#].*)?$",
    re.IGNORECASE,
)
_RESERVED = {"home", "explore", "notifications", "messages", "settings",
             "search", "tos", "privacy", "i", "account", "login", "signup",
             "intent", "hashtag", "help", "about", "status", "share", "download"}

_X_BEARER = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
             "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")
# X GraphQL UserTweets query id (rotates occasionally — update if 404s)
X_USER_TWEETS_QID = "SXVCYB8XHSS25nzIljNtZA"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def extract_tweet_id(url: str) -> str | None:
    """Numeric tweet id from URL, or None."""
    m = TWEET_URL_RE.search((url or "").strip())
    return m.group("id") if m else None


def extract_profile_user(text: str) -> str | None:
    """@handle / profile URL -> username, or None (tweet URLs excluded)."""
    t = (text or "").strip().lstrip("@")
    if extract_tweet_id(t):
        return None
    m = PROFILE_RE.match(t) or re.match(r"(?P<user>[A-Za-z0-9_]{1,30})$", t)
    if m:
        u = m.group("user")
        if u.lower() not in _RESERVED:
            return u
    return None


def is_x_url(url: str) -> bool:
    u = (url or "").lower()
    return "x.com" in u or "twitter.com" in u


def _base36_encode(number: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if number == 0:
        return "0"
    out = []
    while number:
        number, i = divmod(number, 36)
        out.append(alphabet[i])
    return "".join(reversed(out))


_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def syndication_token(tweet_id: str) -> str:
    """X web-syndication token, computed locally (no auth needed).

    Formula from Twitter's web syndication bundle:
        (Number(id) / 1e15 * Math.PI).toString(36).replace(/(0+|\\.)/g, '')
    Ported 1:1 — float() (not int()) to bit-match JS Number(id),
    24 fixed base-36 fractional digits (proven working against the endpoint).
    """
    try:
        val = (float(tweet_id) / 1e15) * math.pi
        int_part = int(val)
        frac = val - int_part
        digits = []
        for _ in range(24):
            frac *= 36.0
            d = int(frac)
            digits.append(_B36[d])
            frac -= d
            if frac == 0.0:
                break
        # strip ALL zeros and the dot — same as JS .replace(/(0+|\\.)/g, '')
        return (_base36_encode(int_part) + "".join(digits)
                ).replace("0", "").replace(".", "")
    except Exception:
        return "x"


class XMediaError(Exception):
    """kind: not_found | private | age_restricted | rate_limited | network | no_media"""
    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


def _classify_error(text: str) -> str:
    s = (text or "").lower()
    if any(k in s for k in ("404", "not found", "deleted", "no such")):
        return "not_found"
    if any(k in s for k in ("private", "protected", "401", "unauthorized",
                            "suspended")):
        return "private"
    if any(k in s for k in ("sensitive", "age", "nsfw")):
        return "age_restricted"
    if any(k in s for k in ("429", "rate limit", "too many requests")):
        return "rate_limited"
    if any(k in s for k in ("timeout", "connect", "resolve", "network")):
        return "network"
    return "no_media"


def _best_variant(variants):
    """Pick highest-bitrate MP4 variant."""
    best, best_br = None, -1
    for v in variants or []:
        url = v.get("url", "")
        if not url or "m3u8" in url:
            continue
        ct = v.get("content_type", "")
        if ct and "mp4" not in ct and not url.endswith(".mp4"):
            continue
        br = v.get("bitrate") or 0
        if br >= best_br:
            best, best_br = v, br
    return best


def _parse_fx_media(tweet: dict) -> list:
    """fxtwitter tweet dict -> [{'url', 'kind', 'title'}]."""
    media = (tweet.get("media") or {})
    items = media.get("all") or (
        media.get("videos", []) + media.get("gifs", []) + media.get("photos", []))
    out = []
    text = (tweet.get("text") or "").strip()
    for m in items:
        t = (m.get("type") or "").lower()
        if t in ("video", "gif", "animated_gif"):
            v = _best_variant(m.get("variants"))
            url = (v or {}).get("url") or m.get("url")
            if url:
                out.append({"url": url, "kind": "video",
                            "title": text,
                            "is_gif": t != "video"})
        elif t == "photo" and m.get("url"):
            out.append({"url": m["url"], "kind": "photo", "title": text})
    return out


def _parse_syndication_media(data: dict) -> list:
    out = []
    text = (data.get("text") or "").strip()
    for m in data.get("mediaDetails", []) or []:
        t = (m.get("type") or "").lower()
        if t in ("video", "animated_gif"):
            v = _best_variant((m.get("video_info") or {}).get("variants"))
            if v and v.get("url"):
                out.append({"url": v["url"], "kind": "video", "title": text,
                            "is_gif": t != "video"})
        elif t == "photo":
            url = m.get("media_url_https") or m.get("url")
            if url:
                out.append({"url": url, "kind": "photo", "title": text})
    return out


async def extract_x_media(url: str) -> list:
    """Cascade: FxTwitter -> VxTwitter -> syndication. Returns media list.

    Raises XMediaError with kind on failure (after all backends tried).
    """
    try:
        import httpx
    except ImportError:
        raise XMediaError("no_media", "httpx မရှိပါ — update.sh run ပေးပါ")
    tweet_id = extract_tweet_id(url)
    if not tweet_id:
        raise XMediaError("no_media", f"tweet id ရှာမရပါ: {url}")
    author_m = re.search(r"(?:twitter\.com|x\.com)/(?:#!/)?([A-Za-z0-9_]+)/status/",
                         url, re.IGNORECASE)
    author = author_m.group(1) if author_m else "i"
    errors = []

    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as c:
        # 1+2. FxTwitter (+ author variant), then VxTwitter
        for ep in (f"https://api.fxtwitter.com/status/{tweet_id}",
                   f"https://api.fxtwitter.com/{author}/status/{tweet_id}",
                   f"https://api.vxtwitter.com/Twitter/status/{tweet_id}",
                   f"https://api.vxtwitter.com/{author}/status/{tweet_id}"):
            try:
                r = await c.get(ep)
                if r.status_code == 200:
                    data = r.json()
                    tweet = data.get("tweet") or {}
                    # vxtwitter shape: media_extended at top level
                    if not tweet and data.get("media_extended"):
                        items = []
                        for m in data["media_extended"]:
                            mt = (m.get("type") or "").lower()
                            if mt in ("video", "gif", "animated_gif") and m.get("url"):
                                items.append({"url": m["url"], "kind": "video",
                                              "title": data.get("text", "")})
                            elif mt == "photo" and m.get("url"):
                                items.append({"url": m["url"], "kind": "photo",
                                              "title": data.get("text", "")})
                    else:
                        items = _parse_fx_media(tweet)
                    if items:
                        return items
                    errors.append(f"{ep}: media မတွေ့ပါ")
                else:
                    errors.append(f"{ep}: HTTP {r.status_code}")
            except Exception as e:
                errors.append(f"{ep}: {type(e).__name__}: {e}")
        # 3. syndication CDN
        try:
            token = syndication_token(tweet_id)
            r = await c.get(
                "https://cdn.syndication.twimg.com/tweet-result",
                params={"id": tweet_id, "token": token},
                headers={"Referer": "https://platform.twitter.com/",
                         "Origin": "https://platform.twitter.com"})
            if r.status_code == 200:
                items = _parse_syndication_media(r.json())
                if items:
                    return items
                errors.append("syndication: media မတွေ့ပါ")
            else:
                errors.append(f"syndication: HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"syndication: {type(e).__name__}: {e}")

    combined = " | ".join(errors)
    raise XMediaError(_classify_error(combined),
                      f"X media မရပါ (tweet {tweet_id}): {combined[:300]}")


def parse_timeline_args(args) -> tuple:
    """(username|None, n) — /xtimeline argument parsing. n clamped 1..10."""
    args = list(args or [])
    if not args:
        return None, 5
    username = extract_profile_user(args[0])
    n = 5
    if len(args) > 1:
        try:
            n = int(args[1])
        except (ValueError, TypeError):
            n = 5
    return username, max(1, min(n, 10))


async def _guest_token(client) -> str:
    r = await client.post(
        "https://api.x.com/1.1/guest/activate.json",
        headers={"Authorization": f"Bearer {_X_BEARER}"})
    if r.status_code == 200:
        return r.json().get("guest_token", "")
    raise RuntimeError(f"guest token HTTP {r.status_code}")


async def fetch_x_timeline(username: str, limit: int = 5) -> list:
    """Recent video tweet ids from a public X profile (guest token, no login).

    Returns [tweet_id, ...] (video/animated_gif only, newest first).
    Raises RuntimeError on failure (e.g. X rotated the GraphQL query id).
    """
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx မရှိပါ — update.sh run ပေးပါ")
    limit = max(1, min(int(limit or 5), 10))
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as c:
        # user id via fxtwitter
        r = await c.get(f"https://api.fxtwitter.com/{username}")
        if r.status_code != 200:
            raise RuntimeError(f"@{username} မတွေ့ပါ (HTTP {r.status_code})")
        user = (r.json().get("user") or {})
        if user.get("protected"):
            raise RuntimeError(f"@{username} က protected account ပါ")
        user_id = str(user.get("id") or "")
        if not user_id:
            raise RuntimeError(f"@{username} id မရပါ")
        gt = await _guest_token(c)
        headers = {"Authorization": f"Bearer {_X_BEARER}",
                   "x-guest-token": gt, "x-twitter-active-user": "yes",
                   "x-twitter-client-language": "en"}
        variables = {"userId": user_id, "count": 20,
                     "includePromotedContent": False,
                     "withClientEventToken": False, "withBirdwatchNotes": False,
                     "withVoice": False, "withV2Timeline": True}
        features = {"responsive_web_graphql_exclude_directive_enabled": True,
                    "verified_phone_label_enabled": False,
                    "responsive_web_graphql_timeline_navigation_enabled": True,
                    "view_counts_everywhere_api_enabled": True,
                    "longform_notetweets_consumption_enabled": True,
                    "tweetypie_unmention_optimization_enabled": True,
                    "responsive_web_edit_tweet_api_enabled": True,
                    "rweb_video_timestamps_enabled": True,
                    "longform_notetweets_rich_text_read_enabled": True,
                    "longform_notetweets_inline_media_enabled": True}
        r = await c.get(
            f"https://x.com/i/api/graphql/{X_USER_TWEETS_QID}/UserTweets",
            params={"variables": __import__("json").dumps(variables),
                    "features": __import__("json").dumps(features)},
            headers=headers)
        if r.status_code == 404:
            raise RuntimeError(
                "X timeline API ပြောင်းသွားပါပြီ (query id expired) — "
                "link တွေ တိုက်ရိုက်ပို့ပေးပါ")
        if r.status_code != 200:
            raise RuntimeError(f"X timeline HTTP {r.status_code}")
        ids = []
        try:
            result = r.json()["data"]["user"]["result"]["timeline"]["timeline"]
            for ins in result.get("instructions", []):
                if ins.get("type") != "TimelineAddEntries":
                    continue
                for e in ins.get("entries", []):
                    item = (e.get("content") or {}).get("itemContent") or {}
                    if item.get("__typename") != "TimelineTweet":
                        continue
                    tw = ((item.get("tweet_results") or {}).get("result")
                          or {})
                    leg = tw.get("legacy") or {}
                    media = ((leg.get("extended_entities") or {}).get("media")
                             or [])
                    if any((m.get("type") or "").lower()
                           in ("video", "animated_gif") for m in media):
                        tid = tw.get("rest_id")
                        if tid and tid not in ids:
                            ids.append(tid)
                    if len(ids) >= limit:
                        break
                if len(ids) >= limit:
                    break
        except (KeyError, TypeError) as e:
            raise RuntimeError(f"X timeline parse failed: {e}")
        return ids

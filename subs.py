"""YIFY subtitles search/download (no API key needed).

Flow: /ajax/search/?mov=<q> -> /movie-imdb/<imdb> -> /subtitles/<slug>
-> /subtitle/<slug>.zip -> extract .srt
"""
import io
import os
import re
import zipfile

import httpx

YIFY_BASE = "https://yifysubtitles.ch"
_UA = {"User-Agent": "tg-video-bot/1.0"}


def _search_word(word: str, timeout: int) -> list:
    r = httpx.get(f"{YIFY_BASE}/ajax/search/", params={"mov": word},
                  timeout=timeout, headers=_UA)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def search_movies(query: str, timeout: int = 25) -> list:
    """-> [{'movie': 'Dune: Part Two 2024', 'imdb': 'tt15239678'}].

    The ajax endpoint only accepts single-word queries, so multi-word
    queries are split and results merged (best matches first).
    """
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower())][:4]
    if not words:
        return []
    merged = {}
    for w in words:
        try:
            for m in _search_word(w, timeout):
                imdb = m.get("imdb")
                if not imdb:
                    continue
                if imdb not in merged:
                    merged[imdb] = {"movie": m.get("movie", imdb),
                                    "imdb": imdb, "_hits": 0}
                merged[imdb]["_hits"] += 1
        except Exception:
            continue
    out = sorted(merged.values(), key=lambda m: -m["_hits"])
    for m in out:
        del m["_hits"]
    return out


_ROW_RE = re.compile(
    r'<tr data-id="(\d+)">.*?'
    r'<td class="rating-cell"><span class="label">(-?\d+)</span></td>.*?'
    r'<span class="sub-lang">([^<]+)</span>.*?'
    r'<a href="(/subtitles/[^"]+)">(.*?)</a>',
    re.S)


LANG_ALIASES = {
    "en": "English", "eng": "English", "english": "English",
    "mm": "Burmese", "my": "Burmese", "burmese": "Burmese", "myanmar": "Burmese",
    "fr": "French", "french": "French",
    "es": "Spanish", "spanish": "Spanish",
    "de": "German", "german": "German",
    "zh": "Chinese", "chinese": "Chinese",
    "ja": "Japanese", "japanese": "Japanese",
    "ko": "Korean", "korean": "Korean",
    "th": "Thai", "thai": "Thai",
}


def movie_languages(imdb: str, timeout: int = 25) -> list:
    """Available languages on the movie page, most-subs first
    (English pinned first if present)."""
    r = httpx.get(f"{YIFY_BASE}/movie-imdb/{imdb}",
                  timeout=timeout, headers=_UA)
    r.raise_for_status()
    counts = {}
    for _sid, _rating, slang, _href, _cell in _ROW_RE.findall(r.text):
        lang = slang.strip()
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    langs = sorted(counts, key=lambda l: -counts[l])
    if "English" in langs:
        langs.remove("English")
        langs.insert(0, "English")
    return langs[:12]


def movie_subtitles(imdb: str, lang: str = "English",
                    timeout: int = 25) -> tuple:
    """-> (movie_title, [subs]) where sub =
    {'id', 'slug', 'lang', 'rating', 'releases', 'uploader'}.
    Sorted by rating desc. Raises on failure."""
    r = httpx.get(f"{YIFY_BASE}/movie-imdb/{imdb}",
                  timeout=timeout, headers=_UA)
    r.raise_for_status()
    h = r.text
    title_m = re.search(r"<title>(.*?)</title>", h, re.S)
    title = (re.sub(r"<[^>]+>", "", title_m.group(1)).strip()
             if title_m else imdb)
    subs = []
    for sid, rating, slang, href, cell in _ROW_RE.findall(h):
        if slang.strip().lower() != lang.lower():
            continue
        releases = [x.strip() for x in
                    re.sub(r"<[^>]+>", "\n", cell).split("\n")
                    if x.strip() and x.strip().lower() != "subtitle"]
        subs.append({
            "id": sid,
            "slug": href.rsplit("/", 1)[-1],
            "lang": slang.strip(),
            "rating": int(rating),
            "releases": releases,
        })
    subs.sort(key=lambda s: -s["rating"])
    return title, subs


def subtitle_zip_url(slug: str, timeout: int = 25) -> str:
    """Subtitle page -> direct .zip URL. Raises on failure."""
    r = httpx.get(f"{YIFY_BASE}/subtitles/{slug}",
                  timeout=timeout, headers=_UA)
    r.raise_for_status()
    m = re.search(r'href="(/subtitle/[^"]+\.zip)"', r.text)
    if not m:
        raise RuntimeError("zip link မတွေ့ပါ")
    return YIFY_BASE + m.group(1)


def download_subtitle(slug: str, name: str, tmpdir: str,
                      timeout: int = 60) -> str:
    """Download zip -> extract first .srt -> tmpdir/<name>.srt. Returns path."""
    url = subtitle_zip_url(slug, timeout=timeout)
    # site requires Referer (hotlink protection) or it cuts the connection
    headers = dict(_UA, Referer=f"{YIFY_BASE}/subtitles/{slug}")
    r = httpx.get(url, timeout=timeout, headers=headers,
                  follow_redirects=True)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    srt_names = [n for n in zf.namelist()
                 if n.lower().endswith(".srt")]
    if not srt_names:
        raise RuntimeError("zip ထဲမှာ .srt မပါပါ")
    safe = re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "subtitle"
    out = os.path.join(tmpdir, f"{safe}.srt")
    with zf.open(srt_names[0]) as fsrc, open(out, "wb") as fdst:
        fdst.write(fsrc.read())
    return out

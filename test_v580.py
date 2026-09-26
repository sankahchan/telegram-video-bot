"""v5.8.0 tests: /subs subtitle search (mocked httpx, no network).

Run: python3 test_v580.py
"""
import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import subs

PASS = []


def check(name, cond):
    PASS.append(name)
    assert cond, f"FAIL: {name}"


class FakeResp:
    def __init__(self, json_data=None, text="", content=b""):
        self._json = json_data
        self.text = text
        self.content = content

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


SEARCH_JSON = [{"movie": "Dune: Part Two 2024", "imdb": "tt15239678"},
               {"movie": "Dune 1984", "imdb": "tt0087182"}]

MOVIE_HTML = """
<html><head><title>Subtitles for Dune Part Two</title></head><body>
<table><thead><tr> <th>rating</th> <th>language</th> <th>release</th>
<th>other</th> <th>uploader</th> </tr> </thead> <tbody>
<tr data-id="618059">
<td class="rating-cell"><span class="label">5</span></td>
<td class="flag-cell"><span class="flag flag-"></span>
<span class="sub-lang">English</span></td>
<td> <a href="/subtitles/dune-part-two-2024-english-yify-618059">
<span class="text-muted">subtitle</span> Dune.Part.Two.2024.1080p.WEB-DL<br />
Dune.Part.Two.2024.720p.WEBRip.x264-GalaxyRG<br /></a></td>
<td></td><td>user1</td></tr>
<tr data-id="618051">
<td class="rating-cell"><span class="label">0</span></td>
<td class="flag-cell"><span class="flag flag-"></span>
<span class="sub-lang">Arabic</span></td>
<td> <a href="/subtitles/dune-part-two-2024-arabic-yify-618051">
<span class="text-muted">subtitle</span> Dune.Part.Two.2024.1080p.WEB-DL<br /></a></td>
<td></td><td>user2</td></tr>
<tr data-id="618060">
<td class="rating-cell"><span class="label">3</span></td>
<td class="flag-cell"><span class="flag flag-"></span>
<span class="sub-lang">english</span></td>
<td> <a href="/subtitles/dune-part-two-2024-english-yify-618060">
<span class="text-muted">subtitle</span> Dune.Part.Two.2024.HDCAM<br /></a></td>
<td></td><td>user3</td></tr>
</tbody></table></body></html>
"""

SUB_PAGE_HTML = ('<html><body><a class="btn" '
                 'href="/subtitle/dune-part-two-2024-english-yify-618059.zip">'
                 'Download</a></body></html>')


def make_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Dune.Part.Two.2024.srt",
                    "1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        zf.writestr("readme.txt", "hi")
    return buf.getvalue()


real_get = subs.httpx.get


def fake_get(url, **kw):
    if "ajax/search" in url:
        word = kw.get("params", {}).get("mov", "")
        if word == "dune":
            return FakeResp(json_data=SEARCH_JSON)
        if word == "1984":
            return FakeResp(json_data=[SEARCH_JSON[1]])
        return FakeResp(json_data=None)  # site returns null
    if "movie-imdb" in url:
        return FakeResp(text=MOVIE_HTML)
    if "/subtitles/" in url:
        return FakeResp(text=SUB_PAGE_HTML)
    if url.endswith(".zip"):
        return FakeResp(content=make_zip())
    raise AssertionError("unexpected url " + url)


subs.httpx.get = fake_get
try:
    movies = subs.search_movies("dune")
    check("search 2 movies", len(movies) == 2)
    check("search imdb", movies[0]["imdb"] == "tt15239678")

    # multi-word: best match (both words) first
    movies = subs.search_movies("dune 1984")
    check("multi-word best first", movies[0]["imdb"] == "tt0087182")

    title, s = subs.movie_subtitles("tt15239678")
    check("title parsed", "Dune" in title)
    check("english only (2)", len(s) == 2)
    check("sorted by rating desc",
          [x["rating"] for x in s] == [5, 3])
    check("slug parsed",
          s[0]["slug"] == "dune-part-two-2024-english-yify-618059")
    check("releases parsed", len(s[0]["releases"]) == 2)
    check("lang normalized", s[0]["lang"] == "English")

    zurl = subs.subtitle_zip_url(s[0]["slug"])
    check("zip url",
          zurl == ("https://yifysubtitles.ch/subtitle/"
                   "dune-part-two-2024-english-yify-618059.zip"))

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = subs.download_subtitle(s[0]["slug"], "Dune Part Two", td)
        check("srt extracted", os.path.basename(p) == "Dune Part Two.srt")
        check("srt content", "Hello" in open(p).read())

    # missing zip link -> error
    subs.httpx.get = lambda url, **kw: FakeResp(text="<html></html>")
    try:
        subs.subtitle_zip_url("x")
        check("missing zip raises", False)
    except RuntimeError:
        check("missing zip raises", True)

    # zip without srt -> error
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hi")
    subs.httpx.get = lambda url, **kw: FakeResp(
        text=SUB_PAGE_HTML, content=buf.getvalue())
    try:
        with tempfile.TemporaryDirectory() as td:
            subs.download_subtitle("x", "n", td)
        check("no-srt zip raises", False)
    except RuntimeError:
        check("no-srt zip raises", True)
finally:
    subs.httpx.get = real_get

# --- bot wiring (static) ------------------------------------------------------
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bot.py"), encoding="utf-8").read()
for needle, name in [
    ('("subs", subs_cmd)', "subs_cmd registered"),
    (r'subm:(tt\d+)', "subm route"),
    (r'subs:([a-z0-9-]+)', "subs route"),
    ("subm:", "callback pattern"),
    ('"subs": (', "help topic"),
    ('callback_data="menu:subs"', "menu subs button"),
    ('action == "subs"', "menu subs handled"),
]:
    check(name, needle in src)

print(f"✅ v5.8.0 subs: {len(PASS)} tests passed")

#!/usr/bin/env python3
"""Import media entries into site/data/media/<type>.yaml with local covers.

The YAML files are the source of truth. An import only adds new entries and
fills in what an existing entry is missing (cover, ids); it never changes an
existing entry's title, status, rating or date.

Usage:
  python scripts/import_media.py import <type> scripts/import/<type>.txt --status <default> [--retry]
  python scripts/import_media.py setcover <type> "<title>" <file | image URL | page URL> [--creator "<creator>"]
  python scripts/import_media.py localize <type> "<title>" [--creator "<creator>"]
  python scripts/import_media.py prune-singles
  python scripts/import_media.py regenre music
  python scripts/import_media.py lastfm-dates
  python scripts/import_media.py check

Types: movies, tv, novels, games, music. Input lines (# lines ignored; status
and rating optional, --status is the default status):
  movies, tv, games: Title | Year | status | rating
  novels:            Author | Title | Year | status | rating [| vn]
  music:             Artist | Album | status | rating

Each entry hits its API once. Results (ids, metadata, image filename) are cached
in scripts/import/cache.json keyed by type + the line's identifying fields, so
re-runs make no API calls for cached entries. --retry re-resolves cached weak or
failed matches and entries without a cover.

Sources: TMDB (movies, tv), Open Library and VNDB (novels), Steam (games),
MusicBrainz + Cover Art Archive, then Deezer, then iTunes (music).
Movies and TV need TMDB_API_KEY, regenre needs LASTFM_API_KEY, both in .env at
the repo root (never printed). Entries with genres_locked: true keep their genres.
Anything needing a manual check is listed in scripts/import/report.txt.
"""

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import unicodedata
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    import requests
    import yaml
    from PIL import Image
except ImportError as missing:
    sys.exit(f"Missing Python package: {missing.name}. Install them with "
             f"`python -m pip install -r scripts/requirements.txt` (see scripts/README.md).")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "site" / "data" / "media"
IMG_DIR = ROOT / "site" / "static" / "img" / "media"
IMPORT_DIR = ROOT / "scripts" / "import"
CACHE_FILE = IMPORT_DIR / "cache.json"
REPORT_FILE = IMPORT_DIR / "report.txt"

USER_AGENT = "ryancranie.com-media-import/1.0 (mail@ryancranie.com)"
TYPES = ("movies", "tv", "novels", "games", "music")
STATUSES = {
    "novels": ("completed", "current", "planning"),
    "movies": ("completed", "planning"),
    "tv": ("completed", "current", "planning", "dropped"),
    "games": ("completed", "planning", "active", "inactive"),
    "music": ("completed", "planning"),
}
ALL_STATUSES = ("completed", "active", "inactive", "current", "planning", "dropped")
# nearest allowed status when a type doesn't allow one (completed and planning are always allowed)
NEAREST = {"active": "completed", "inactive": "completed", "current": "planning", "dropped": "planning"}
# per-type overrides: games have no "current", in progress is "active"
NEAREST_BY_TYPE = {"games": {"current": "active"}}
ID_FIELDS = ("tmdb_id", "mbid", "deezer_id", "itunes_id", "steam_appid", "olid", "vndb_id")
MAX_GENRES = 3
IMAGE_MAX = {"music": (500, 500)}  # everything else is portrait
PORTRAIT_MAX = (400, 600)

TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w500"
OPENLIBRARY = "https://openlibrary.org/search.json"
OL_COVER = "https://covers.openlibrary.org/b/id/{}-L.jpg"
VNDB_API = "https://api.vndb.org/kana/vn"
STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/"
STEAM_DETAILS = "https://store.steampowered.com/api/appdetails"
STEAM_COVERS = (
    "https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{}/library_600x900_2x.jpg",
    "https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{}/library_600x900.jpg",
)
MB_API = "https://musicbrainz.org/ws/2"
CAA_FRONT = "https://coverartarchive.org/release-group/{}/front-500"
DEEZER_API = "https://api.deezer.com"
ITUNES_SEARCH = "https://itunes.apple.com/search"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"

MIN_INTERVAL = {"musicbrainz.org": 1.1}  # seconds between requests per host
DEFAULT_INTERVAL = 0.25
MAX_TRIES = 4

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT
stats = {"api": 0, "images": 0}


class FetchError(Exception):
    """A request failed after retries; the message is safe to print (no secrets).
    .response holds the last HTTP response, if there was one."""

    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


# ---------------------------------------------------------------- helpers

def load_env():
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def redact(text):
    for name in ("TMDB_API_KEY", "LASTFM_API_KEY"):
        key = os.environ.get(name)
        if key:
            text = text.replace(key, "<redacted>")
    return text


def norm(text):
    """Casefold and drop punctuation/whitespace (incl. curly quotes), keeping letters in any script."""
    text = re.sub(r"[™®©]", "", str(text or ""))  # NFKC would turn ™ into "TM"
    text = unicodedata.normalize("NFKC", text).casefold().replace("&", "and")
    return re.sub(r"[\W_]+", "", text)


def year_of(text):
    m = re.search(r"\b(1[89]\d\d|20\d\d)\b", str(text or ""))
    return int(m.group(1)) if m else None


def slugify(text):
    ascii_part = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_part.lower()).strip("-")
    non_latin = any(
        unicodedata.category(c).startswith("L") and "LATIN" not in unicodedata.name(c, "")
        for c in text
    )
    if non_latin or not slug:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
        slug = f"{slug}-{digest}" if slug else digest
    return slug


def strip_edition(text):
    """Drop bracketed and dash suffixes: "Inside (Deluxe)", "Album - Single"."""
    text = re.sub(r"\s*[\(\[].*?[\)\]]", "", text or "")
    return re.sub(r"\s+-\s+(single|ep)$", "", text, flags=re.I)


EDITION_BRACKETS = re.compile(
    r"\s*[\(\[][^\)\]]*\b(deluxe|remaster(ed)?|expanded|anniversary|reissue|bonus tracks?)\b[^\)\]]*[\)\]]\s*$", re.I)
EDITION_DASH = re.compile(r"\s+-\s+(reissue|remaster(ed)?(\s+\d{4})?|deluxe(\s+edition)?)\s*$", re.I)


def strip_edition_suffix(title):
    """Drop edition suffixes from an album title: "(Deluxe ...)", "(Remastered 2006)",
    "(Expanded ...)", "(15th Anniversary ...)", "- Reissue"."""
    previous = None
    while previous != title:
        previous = title
        title = EDITION_DASH.sub("", EDITION_BRACKETS.sub("", title))
    return title.strip()


def title_match(wanted, *candidates):
    """'exact' if a candidate equals wanted, 'partial' if one contains the other, else None."""
    w = norm(wanted)
    cands = [norm(c) for c in candidates if c]
    if w in cands:
        return "exact"
    if w and any(c and (w in c or c in w) for c in cands):
        return "partial"
    return None


def creator_ok(wanted, found):
    w, f = norm(wanted), norm(found)
    return bool(w) and bool(f) and (w == f or w in f or f in w)


def has_cover(entry):
    cover = str(entry.get("cover") or "")
    return cover.startswith("/img/media/") and not cover.endswith("placeholder.svg")


def entry_ids(entry):
    return {(f, str(entry[f])) for f in ID_FIELDS if entry.get(f)}


# ---------------------------------------------------------------- http

_last_request = {}


def _throttle(host):
    wait = MIN_INTERVAL.get(host, DEFAULT_INTERVAL) - (time.monotonic() - _last_request.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_request[host] = time.monotonic()


def _retry_after(response, attempt):
    """Exponential backoff (2, 4, 8s), longer if Retry-After asks for more.
    MusicBrainz sends "Retry-After: 0" with its 503s, so never go below the backoff."""
    backoff = 2 ** attempt
    header = response.headers.get("Retry-After") if response is not None else None
    if header:
        try:
            return min(max(float(header), backoff), 120)
        except ValueError:
            try:
                return min(max(parsedate_to_datetime(header).timestamp() - time.time(), backoff), 120)
            except (TypeError, ValueError):
                pass
    return backoff


def request(method, url, params=None, label="request", api=True, ok_404=False, json_body=None):
    """One request at a time, rate-limited per host, retried on 429/5xx."""
    host = urlparse(url).hostname or ""
    for attempt in range(1, MAX_TRIES + 1):
        _throttle(host)
        if api:
            stats["api"] += 1
        response = None
        try:
            response = session.request(method, url, params=params, json=json_body,
                                       timeout=30, allow_redirects=True)
        except requests.RequestException as e:
            error = redact(type(e).__name__)
        else:
            if response.status_code == 401 and "themoviedb" in url:
                sys.exit("TMDB rejected the API key (HTTP 401). Check TMDB_API_KEY in .env.")
            if response.status_code == 404 and ok_404:
                return None
            if response.ok:
                return response
            error = f"HTTP {response.status_code}"
            if response.status_code != 429 and response.status_code < 500:
                raise FetchError(f"{label} returned {error}", response)
        if attempt < MAX_TRIES:
            delay = _retry_after(response, attempt)
            print(f"    {label}: {error}, retrying in {delay:.0f}s")
            time.sleep(delay)
    raise FetchError(f"{label} failed after {MAX_TRIES} tries ({error})", response)


def get_json(url, params=None, label="request"):
    return request("GET", url, params, label).json()


# ---------------------------------------------------------------- cache, data, input files, report

def load_cache():
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache):
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(CACHE_FILE)


def load_entries(media_type):
    path = DATA_DIR / f"{media_type}.yaml"
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


class QuotedStr(str):
    """Written with double quotes, so "2026-10" stays a string."""


yaml.SafeDumper.add_representer(
    QuotedStr, lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:str", value, style='"'))


def month_of(value):
    """Any date-ish value → "YYYY-MM" (dates are month precision), or None."""
    m = re.match(r"(\d{4})-(\d{2})", str(value or ""))
    return f"{m.group(1)}-{m.group(2)}" if m else None


def tidy_entry(entry):
    """Normalise an entry before saving: dates as quoted "YYYY-MM", no ™/® in titles."""
    entry["title"] = re.sub(r"\s{2,}", " ", re.sub(r"[™®]", "", str(entry["title"]))).strip()
    if entry.get("date"):
        entry["date"] = QuotedStr(month_of(entry["date"]) or entry["date"])
    return entry


def save_entries(media_type, entries):
    chunks = [
        yaml.safe_dump([tidy_entry(e)], sort_keys=False, allow_unicode=True, default_flow_style=None, width=1000)
        for e in entries
    ]
    (DATA_DIR / f"{media_type}.yaml").write_text("\n".join(chunks), encoding="utf-8")


def input_path(media_type):
    return IMPORT_DIR / f"{media_type}.txt"


def input_lines(path):
    lines = [l.strip() for l in Path(path).read_text(encoding="utf-8").splitlines()]
    return [l for l in lines if l and not l.startswith("#")]


def find_entries(entries, title, creator=None):
    return [e for e in entries if norm(e.get("title")) == norm(title)
            and (creator is None or norm(e.get("creator")) == norm(creator))]


def remove_input_lines(media_type, removed, cache=None):
    """Drop the input-file lines that produced the removed entries, so a re-run can't re-add them.
    Lines are matched by the ids in their cached result, else by title (+ creator when given)."""
    path = input_path(media_type)
    if not path.exists() or not removed:
        return []
    cache = cache if cache is not None else load_cache()
    removed_ids = set().union(*(entry_ids(e) for e in removed))
    removed_titles = {norm(e.get("title")) for e in removed}
    removed_pairs = {(norm(e.get("title")), norm(e.get("creator"))) for e in removed}
    kept, dropped = [], []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            try:
                item = parse_line(media_type, line)
            except ValueError:
                kept.append(raw)
                continue
            meta = (cache.get(cache_key(media_type, item)) or {}).get("meta") or {}
            if (entry_ids(meta) & removed_ids
                    or norm(meta.get("title")) in removed_titles and meta.get("title")
                    or (item["creator"] and (norm(item["title"]), norm(item["creator"])) in removed_pairs)
                    or (not item["creator"] and norm(item["title"]) in removed_titles)):
                dropped.append(line)
                continue
        kept.append(raw)
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return dropped


def remove_unused_images(media_type, removed, entries):
    """Delete local covers of removed entries that no remaining entry uses."""
    used = {e.get("cover") for e in entries}
    for e in removed:
        cover = e.get("cover")
        if has_cover(e) and cover not in used:
            (ROOT / "site" / "static" / cover.lstrip("/")).unlink(missing_ok=True)


def read_report_section(name):
    if not REPORT_FILE.exists():
        return []
    lines, current = [], None
    for line in REPORT_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
        elif current == name and line.startswith("- "):
            lines.append(line)
    return lines


def write_report_section(name, lines):
    """Replace one section of report.txt, keeping the others."""
    sections = {}
    if REPORT_FILE.exists():
        current = None
        for line in REPORT_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                current = line[3:].strip()
                sections[current] = []
            elif current and line.strip():
                sections[current].append(line)
    sections[name] = lines or ["(nothing to check)"]
    order = [*TYPES, *sorted(s for s in sections if s not in TYPES)]
    out = ["# Media import report", "# Entries below need a manual check.", ""]
    for section in order:
        if section in sections:
            out += [f"## {section}", *sections[section], ""]
    REPORT_FILE.write_text("\n".join(out), encoding="utf-8")


# ---------------------------------------------------------------- input parsing

def parse_line(media_type, line):
    """Returns dict(title, creator, year, status, rating, vn) or raises ValueError."""
    parts = [p.strip() for p in line.split("|")]
    vn = media_type == "novels" and parts[-1].lower() == "vn"
    if vn:
        parts = parts[:-1]
    if media_type in ("novels", "music"):
        if len(parts) < 2:
            raise ValueError("expected Author | Title" if media_type == "novels" else "expected Artist | Album")
        creator, title, rest = parts[0], parts[1], parts[2:]
    else:
        creator, title, rest = "", parts[0], parts[1:]
    year = rest.pop(0) if media_type != "music" and rest and rest[0].isdigit() else None

    status = rest.pop(0).lower() if rest else ""
    if status and status not in ALL_STATUSES:
        raise ValueError(f"unknown status {status!r}")
    rating = rest.pop(0) if rest else ""
    if rating:
        rating = float(rating)
        if not (1 <= rating <= 5) or (rating * 2) % 1:
            raise ValueError(f"rating must be 1-5 in halves, got {rating}")
        rating = int(rating) if rating.is_integer() else rating
    if not title:
        raise ValueError("missing title")
    return {"title": title, "creator": creator, "year": int(year) if year else None,
            "status": status or None, "rating": rating or None, "vn": vn}


def cache_key(media_type, item):
    """type + the line's identifying fields, so editing status/rating keeps the cache."""
    parts = (media_type, norm(item["creator"]), norm(item["title"]), str(item["year"] or ""))
    return "|".join(p for p in parts if p)


def allowed_status(media_type, status):
    if status in STATUSES[media_type]:
        return status
    return NEAREST_BY_TYPE.get(media_type, {}).get(status) or NEAREST.get(status, "planning")


# ---------------------------------------------------------------- lookups
# Each returns dict(match, reason, meta, image_urls) where match is
# "exact" | "partial" (used, but listed in the report) | "weak" | "failed".

def result(match, reason=None, meta=None, image_urls=()):
    return {"match": match, "reason": reason, "meta": meta, "image_urls": [u for u in image_urls if u]}


def tmdb(path, **params):
    params["api_key"] = os.environ["TMDB_API_KEY"]
    return get_json(TMDB_API + path, params, label="TMDB")


def pick(candidates, item, name_keys, year_key):
    """Best candidate: exact title (+year ±1), else partial title with exact year."""
    year = item["year"]
    partial = None
    for c in candidates:
        grade = title_match(item["title"], *(c.get(k) for k in name_keys))
        cy = year_of(c.get(year_key))
        if grade == "exact" and (year is None or (cy and abs(cy - year) <= 1)):
            return c, "exact"
        if grade == "partial" and partial is None and year is not None and cy == year:
            partial = c
    return (partial, "partial") if partial else (None, None)


def tmdb_lookup(item, kind):
    """kind is "movie" or "tv". Year search, then no-year search, then alternative titles."""
    name_keys = ("title", "original_title") if kind == "movie" else ("name", "original_name")
    year_key = "release_date" if kind == "movie" else "first_air_date"
    year_param = "primary_release_year" if kind == "movie" else "first_air_date_year"
    search = {"query": item["title"], "language": "en-US", "include_adult": "false"}

    results = tmdb(f"/search/{kind}", **{year_param: item["year"] or ""}, **search).get("results", [])
    best, grade = pick(results, item, name_keys, year_key)
    if not best and item["year"]:
        more = tmdb(f"/search/{kind}", **search).get("results", [])
        best, grade = pick(more, item, name_keys, year_key)
        results = results or more
    if not best:
        # alternative titles, e.g. the US "Sorcerer's Stone" for "Philosopher's Stone"
        for c in [c for c in results if not item["year"] or
                  (year_of(c.get(year_key)) and abs(year_of(c.get(year_key)) - item["year"]) <= 1)][:3]:
            alts = tmdb(f"/{kind}/{c['id']}/alternative_titles")
            titles = [t.get("title") for t in alts.get("titles", alts.get("results", []))]
            if title_match(item["title"], *titles) == "exact":
                best, grade = c, "partial"
                break
    if not results:
        return result("failed", "no TMDB results")
    if not best:
        top = results[0]
        return result("weak", f'closest TMDB result "{top.get(name_keys[0])}" '
                              f'({year_of(top.get(year_key)) or "?"}), tmdb_id {top["id"]}')
    return best, grade


def lookup_movies(item):
    found = tmdb_lookup(item, "movie")
    if isinstance(found, dict):
        return found
    best, grade = found
    movie = tmdb(f"/movie/{best['id']}", append_to_response="credits", language="en-US")
    directors = [c["name"] for c in movie.get("credits", {}).get("crew", []) if c.get("job") == "Director"]
    meta = {"title": movie.get("title") or item["title"], "creator": ", ".join(directors),
            "year": year_of(movie.get("release_date")) or item["year"],
            "genre": [g["name"] for g in movie.get("genres", [])][:MAX_GENRES], "tmdb_id": movie["id"]}
    poster = movie.get("poster_path")
    reason = f'matched "{meta["title"]}" ({meta["year"]})' if grade == "partial" else None
    return result(grade, reason, meta, [TMDB_IMG + poster] if poster else [])


def lookup_tv(item):
    found = tmdb_lookup(item, "tv")
    if isinstance(found, dict):
        return found
    best, grade = found
    show = tmdb(f"/tv/{best['id']}", language="en-US")
    creators = [c["name"] for c in show.get("created_by", [])]
    networks = [n["name"] for n in show.get("networks", [])]
    meta = {"title": show.get("name") or item["title"], "creator": (creators or networks or [""])[0],
            "year": year_of(show.get("first_air_date")) or item["year"],
            "genre": [g["name"] for g in show.get("genres", [])][:MAX_GENRES], "tmdb_id": show["id"]}
    poster = show.get("poster_path")
    reason = f'matched "{meta["title"]}" ({meta["year"]})' if grade == "partial" else None
    return result(grade, reason, meta, [TMDB_IMG + poster] if poster else [])


def vndb_lookup(item):
    """Visual novels from VNDB. Returns a result dict, or None when nothing matches."""
    fields = ("id,title,alttitle,aliases,titles.title,titles.lang,titles.official,released,"
              "developers.name,image.url,image.sexual,image.violence")
    queries = [item["title"], strip_edition(item["title"]),
               *re.findall(r"\(([^)]+)\)", item["title"])]
    seen = set()
    for q in queries:
        q = q.strip()
        if not q or q in seen:
            continue
        seen.add(q)
        body = {"filters": ["search", "=", q], "fields": fields, "results": 10}
        vns = request("POST", VNDB_API, label="VNDB", json_body=body).json().get("results", [])
        for vn in vns:
            names = [vn.get("title"), vn.get("alttitle"), *vn.get("aliases", []),
                     *(t.get("title") for t in vn.get("titles", []))]
            grade = title_match(q, *names)
            vy = year_of(vn.get("released"))
            if grade and (item["year"] is None or (vy and abs(vy - item["year"]) <= 1)):
                english = next((t["title"] for t in vn.get("titles", [])
                                if t.get("lang") == "en" and t.get("official")), None)
                image = vn.get("image") or {}
                meta = {"title": english or vn.get("title") or item["title"],
                        "creator": ", ".join(d["name"] for d in vn.get("developers", [])[:1]),
                        "year": vy or item["year"], "genre": ["Visual Novel"], "vndb_id": vn["id"]}
                # VNDB flags are 0 safe, 1 suggestive, 2 explicit (averaged votes)
                if (image.get("sexual") or 0) >= 0.5 or (image.get("violence") or 0) >= 0.5:
                    meta["nsfw"] = True
                return result("exact", None if grade == "exact" else f'matched VNDB "{meta["title"]}"',
                              meta, [image.get("url")])
    return None


def lookup_novels(item):
    if item["vn"]:
        return vndb_lookup(item) or result("failed", "no VNDB match")

    def search(title):
        params = {"title": title, "author": item["creator"], "limit": 10,
                  "fields": "key,title,author_name,first_publish_year,cover_i"}
        docs = get_json(OPENLIBRARY, params, label="Open Library").get("docs", [])
        best, grade = None, None
        for d in docs:
            g = title_match(title, d.get("title"))
            if g and any(creator_ok(item["creator"], a) for a in d.get("author_name", [])):
                if g == "exact" and d.get("cover_i"):
                    return docs, d, g
                if best is None:
                    best, grade = d, g
        return docs, best, grade

    docs, best, grade = search(item["title"])
    main_title = item["title"].split(":")[0].strip()
    if not (best and best.get("cover_i")) and main_title != item["title"]:
        # "Perfume: The Story of a Murderer" is catalogued as "Perfume": retry with the main title,
        # but keep the full title from the input line
        more, alt, alt_grade = search(main_title)
        if alt and alt.get("cover_i") and alt_grade == "exact":
            docs, best, grade = more, dict(alt, title=item["title"]), "exact"
    if best and best.get("cover_i"):
        authors = best.get("author_name", [])
        meta = {"title": best.get("title") or item["title"],
                "creator": next((a for a in authors if creator_ok(item["creator"], a)), item["creator"]),
                "year": item["year"] or best.get("first_publish_year"),
                "genre": [], "olid": best.get("key", "").rsplit("/", 1)[-1]}  # subjects are too noisy
        reason = f'matched "{meta["title"]}"' if grade == "partial" else None
        return result(grade, reason, meta, [OL_COVER.format(best["cover_i"])])

    # no Open Library cover: try VNDB before giving up
    vn = vndb_lookup(item)
    if vn:
        vn["reason"] = "matched on VNDB (no Open Library cover)"
        return vn
    if best:
        meta = {"title": best.get("title") or item["title"], "creator": item["creator"],
                "year": item["year"] or best.get("first_publish_year"),
                "genre": [], "olid": best.get("key", "").rsplit("/", 1)[-1]}
        return result(grade, "no cover on Open Library or VNDB", meta, [])
    if not docs:
        return result("failed", "no Open Library or VNDB results")
    return result("weak", f'closest Open Library result "{docs[0].get("title")}" '
                          f'by {", ".join(docs[0].get("author_name", [])[:1]) or "?"}')


def lookup_games(item):
    data = get_json(STEAM_SEARCH, {"term": item["title"], "cc": "GB", "l": "english"}, label="Steam search")
    items = data.get("items", [])
    if not items:
        return result("failed", "no Steam results (may not be on Steam)")

    def app_details(appid):
        data = get_json(STEAM_DETAILS, {"appids": appid, "cc": "GB", "l": "english"}, label="Steam appdetails")
        return (data.get(str(appid)) or {}).get("data") or {}

    best = next((c for c in items if title_match(item["title"], c.get("name")) == "exact"), None)
    grade, app = ("exact", None) if best else (None, None)
    if not best and item["year"]:
        # partial names need the same release year: "Tomb Raider" (2013) is listed as
        # "Tomb Raider Game of the Year", after several other Tomb Raider games
        w = norm(item["title"])
        prefixed = [c for c in items if norm(c.get("name")).startswith(w) or w.startswith(norm(c.get("name")))]
        for c in prefixed[:4]:
            details = app_details(c["id"])
            if year_of(details.get("release_date", {}).get("date")) == item["year"]:
                best, grade, app = c, "partial", details
                break
    if not best:
        return result("weak", f'closest Steam result "{items[0].get("name")}", appid {items[0]["id"]}')
    appid = best["id"]
    app = app if app is not None else app_details(appid)
    meta = {"title": app.get("name") or best.get("name") or item["title"],
            "creator": ", ".join(app.get("developers", [])[:1]),
            # the input year wins: Steam dates re-releases (e.g. CS2 under CS:GO's 2012 page)
            "year": item["year"] or year_of(app.get("release_date", {}).get("date")),
            "genre": [g["description"] for g in app.get("genres", [])][:MAX_GENRES], "steam_appid": appid}
    reason = f'matched "{meta["title"]}" (appid {appid})' if grade == "partial" else None
    return result(grade, reason, meta, [u.format(appid) for u in STEAM_COVERS])


def lucene_phrase(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def credit_name(artist_credit):
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in artist_credit or [])


def album_title_ok(wanted, found):
    return norm(found) == norm(wanted) or norm(strip_edition(found)) == norm(strip_edition(wanted))


def deezer_find(item):
    for q in (f'artist:{lucene_phrase(item["creator"])} album:{lucene_phrase(item["title"])}',
              f'{item["creator"]} {item["title"]}'):
        data = get_json(f"{DEEZER_API}/search/album", {"q": q, "limit": 10}, label="Deezer search")
        for a in data.get("data", []):
            if creator_ok(item["creator"], a.get("artist", {}).get("name")) and album_title_ok(item["title"], a.get("title")):
                return a
    return None


def itunes_find(item):
    data = get_json(ITUNES_SEARCH, {"term": f'{item["creator"]} {item["title"]}', "entity": "album",
                                    "limit": 15, "country": "GB"}, label="iTunes search")
    for a in data.get("results", []):
        if creator_ok(item["creator"], a.get("artistName")) and album_title_ok(item["title"], a.get("collectionName")):
            return a
    return None


def itunes_art(album):
    url = album.get("artworkUrl100") or ""
    return re.sub(r"/\d+x\d+bb\.", "/600x600bb.", url) if url else None


def caa_exists(mbid):
    """HEAD the Cover Art Archive front image (an image host, not the MusicBrainz API)."""
    try:
        return request("HEAD", CAA_FRONT.format(mbid), label="Cover Art Archive", api=False, ok_404=True) is not None
    except FetchError:
        return False  # a persistent 5xx means the cover is broken there; fall back


def music_fallback_cover(item, meta):
    """Deezer cover_big, then iTunes artwork at 600x600."""
    dz = deezer_find(item)
    if dz and dz.get("cover_big"):
        meta["deezer_id"] = dz["id"]
        return [dz["cover_big"]]
    it = itunes_find(item)
    if it and itunes_art(it):
        meta["itunes_id"] = it["collectionId"]
        return [itunes_art(it)]
    return []


def lookup_music(item):
    query = f'releasegroup:{lucene_phrase(item["title"])} AND artist:{lucene_phrase(item["creator"])}'
    groups = get_json(f"{MB_API}/release-group", {"query": query, "limit": 10, "fmt": "json"},
                      label="MusicBrainz").get("release-groups", [])
    matches = [g for g in groups
               if g.get("score", 0) >= 85
               and norm(g.get("title")) == norm(item["title"])
               and creator_ok(item["creator"], credit_name(g.get("artist-credit")))]

    if matches:
        rank = {"Album": 0, "EP": 1, "Single": 2}
        group = sorted(matches, key=lambda g: (rank.get(g.get("primary-type"), 3), -g.get("score", 0)))[0]
        mbid = group["id"]
        details = get_json(f"{MB_API}/release-group/{mbid}", {"inc": "genres", "fmt": "json"}, label="MusicBrainz")
        genres = sorted(details.get("genres", []), key=lambda g: -g.get("count", 0))
        meta = {"title": strip_edition_suffix(group.get("title") or item["title"]),
                "creator": credit_name(group.get("artist-credit")) or item["creator"],
                "year": year_of(group.get("first-release-date")),
                "genre": [g["name"] for g in genres][:MAX_GENRES], "mbid": mbid,
                "primary_type": group.get("primary-type")}
        images = [CAA_FRONT.format(mbid)] if caa_exists(mbid) else music_fallback_cover(item, meta)
        return result("exact", None, meta, images)

    mb_reason = "no MusicBrainz results" if not groups else (
        f'no exact MusicBrainz match (closest "{groups[0].get("title")}" by '
        f'{credit_name(groups[0].get("artist-credit"))})')
    dz = deezer_find(item)
    if dz:
        details = get_json(f"{DEEZER_API}/album/{dz['id']}", label="Deezer album")
        meta = {"title": strip_edition_suffix(dz.get("title") or item["title"]),
                "creator": dz.get("artist", {}).get("name") or item["creator"],
                "year": year_of(details.get("release_date")),
                "genre": [g["name"] for g in details.get("genres", {}).get("data", [])][:MAX_GENRES],
                "deezer_id": dz["id"], "primary_type": (details.get("record_type") or "").title() or None}
        return result("partial", f"Deezer fallback ({mb_reason})", meta, [dz.get("cover_big")])
    it = itunes_find(item)
    if it:
        name = it.get("collectionName") or item["title"]
        meta = {"title": strip_edition_suffix(strip_edition(name) if re.search(r" - (Single|EP)$", name) else name),
                "creator": it.get("artistName") or item["creator"],
                "year": year_of(it.get("releaseDate")),
                "genre": [it["primaryGenreName"]] if it.get("primaryGenreName") else [],
                "itunes_id": it["collectionId"],
                "primary_type": "Single" if name.endswith(" - Single") else "EP" if name.endswith(" - EP") else "Album"}
        return result("partial", f"iTunes fallback ({mb_reason}; no Deezer match)", meta, [itunes_art(it)])
    return result("failed", f"{mb_reason}; no Deezer or iTunes match")


LOOKUPS = {"movies": lookup_movies, "tv": lookup_tv, "novels": lookup_novels,
           "games": lookup_games, "music": lookup_music}


# ---------------------------------------------------------------- images

def image_name(media_type, cache, key, title, creator):
    """Slug from the title; disambiguate with the creator if another entry owns it."""
    taken = {v.get("image") for k, v in cache.items() if k != key and k.startswith(media_type + "|")}
    name = slugify(title) + ".jpg"
    if name in taken and creator:
        name = slugify(f"{title} {creator}") + ".jpg"
    return name


def save_image(media_type, data, filename):
    """Resize (never upscale or crop) and save as JPEG q85. Returns False if it isn't an image."""
    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
    except (OSError, Image.DecompressionBombError):
        return False
    img.thumbnail(IMAGE_MAX.get(media_type, PORTRAIT_MAX), Image.LANCZOS)
    dest = IMG_DIR / media_type / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=85, optimize=True)
    return True


def download_image(media_type, urls, filename):
    """Download the first URL that works, unless the file is already on disk."""
    if (IMG_DIR / media_type / filename).exists():
        return True
    for url in urls:
        response = request("GET", url, label="image", api=False, ok_404=True)
        if response is None or not response.headers.get("Content-Type", "").startswith("image/"):
            continue
        if save_image(media_type, response.content, filename):
            stats["images"] += 1
            return True
    return False


# ---------------------------------------------------------------- commands

def check_type(media_type):
    """Tidy one data file: drop old placeholder entries and non-local covers, convert
    statuses the type doesn't allow. Returns report lines for the conversions."""
    entries = load_entries(media_type)
    kept = [e for e in entries if not str(e.get("title", "")).startswith("Placeholder ")]
    notes = []
    for e in kept:
        if "cover" in e and not has_cover(e):
            del e["cover"]
        status = e.get("status", "planning")
        if status not in STATUSES[media_type]:
            e["status"] = allowed_status(media_type, status)
            notes.append(f'- {e["title"]}: status {status} → {e["status"]} (not allowed for {media_type})')
    save_entries(media_type, kept)
    return notes


def title_card_section():
    lines = []
    for t in TYPES:
        missing = [e["title"] for e in load_entries(t) if not has_cover(e)]
        lines += [f"- {t}: {title}" for title in missing]
    write_report_section("title cards (no cover yet; use setcover)", lines)


def cmd_import(media_type, input_file, default_status, retry=False):
    if media_type in ("movies", "tv") and not os.environ.get("TMDB_API_KEY"):
        sys.exit("TMDB_API_KEY is not set (add it to .env at the repo root).")
    if default_status not in STATUSES[media_type]:
        sys.exit(f"--status {default_status} is not allowed for {media_type}: {', '.join(STATUSES[media_type])}")

    status_notes = check_type(media_type)
    cache = load_cache()
    entries = load_entries(media_type)
    report, counts = [], {"new": 0, "updated": 0, "cached": 0}

    def find_existing(meta, item):
        ids = entry_ids(meta)
        for e in entries:
            if ids & entry_ids(e):
                return e
        for e in entries:
            if (norm(e.get("title")), norm(e.get("creator"))) == (norm(meta["title"]), norm(meta["creator"])):
                return e
            if item["creator"] and (norm(e.get("title")), norm(e.get("creator"))) == (norm(item["title"]), norm(item["creator"])):
                return e
            if not item["creator"] and norm(e.get("title")) in (norm(meta["title"]), norm(item["title"])) \
                    and e.get("year") in (meta.get("year"), item["year"]):
                return e
        return None

    lines = input_lines(input_file)
    for n, line in enumerate(lines, 1):
        prefix = f"[{n}/{len(lines)}] {line}"
        try:
            item = parse_line(media_type, line)
        except ValueError as e:
            print(f"{prefix} → skipped: {e}")
            report.append(f"- {line}  →  unreadable line: {e}")
            continue

        key = cache_key(media_type, item)
        cached = cache.get(key)
        stale = cached and retry and (cached["match"] in ("weak", "failed") or not cached.get("image"))
        if cached and not stale:
            counts["cached"] += 1
        else:
            try:
                fresh = LOOKUPS[media_type](item)
            except FetchError as e:
                print(f"{prefix} → lookup error, not cached (re-run to retry): {e}")
                report.append(f"- {line}  →  lookup error, re-run to retry: {e}")
                continue
            fresh["image"] = None
            cached = cache[key] = fresh
            save_cache(cache)

        good = cached["match"] in ("exact", "partial") and cached["meta"]
        meta = cached["meta"] if good else {
            "title": item["title"], "creator": item["creator"], "year": item["year"], "genre": []}

        # cover: download once, remember the filename in the cache
        cover, notes = None, []
        if good and cached["image_urls"]:
            if not cached.get("image"):
                cached["image"] = image_name(media_type, cache, key, meta["title"], meta["creator"])
            try:
                if download_image(media_type, cached["image_urls"], cached["image"]):
                    cover = f"/img/media/{media_type}/{cached['image']}"
                else:
                    cached["image"] = None
            except FetchError as e:
                cached["image"] = None
                notes.append(f"image download failed, re-run to retry: {e}")
            save_cache(cache)

        if not good:
            notes.insert(0, f"{cached['match']}: {cached['reason']} (added from input text, title card)")
        elif cached.get("reason"):
            notes.insert(0, cached["reason"])

        existing = find_existing(meta, item)
        if existing is not None:
            # the YAML is the source of truth: only fill in what's missing
            if good and not entry_ids(existing):
                for field in ("creator", "year"):
                    if meta.get(field) and not existing.get(field):
                        existing[field] = meta[field]
                if not existing.get("genres_locked"):
                    existing["genre"] = list(dict.fromkeys([*existing.get("genre", []), *meta.get("genre", [])]))
            if cover and not has_cover(existing) and not existing.get("cover_locked"):
                existing["cover"] = cover
            for field in ID_FIELDS:
                if meta.get(field):
                    existing.setdefault(field, meta[field])
            if meta.get("nsfw"):
                existing.setdefault("nsfw", True)
            counts["updated"] += 1
            action = "kept" if cover is None or existing.get("cover") != cover else "updated"
            shown = existing
        else:
            status = item["status"] or default_status
            if status not in STATUSES[media_type]:
                notes.append(f"status {status} → {allowed_status(media_type, status)} (not allowed for {media_type})")
                status = allowed_status(media_type, status)
            entry = {"title": meta["title"], "creator": meta["creator"]}
            if meta.get("year"):
                entry["year"] = meta["year"]
            entry["genre"] = meta.get("genre", [])
            if media_type == "novels" and not entry["genre"]:
                notes.append("no genres (Open Library subjects aren't used); set them by hand")
            entry["status"] = status
            if item["rating"] is not None:
                entry["rating"] = item["rating"]
            if cover:
                entry["cover"] = cover
            if meta.get("nsfw"):
                entry["nsfw"] = True
            entry.update({f: meta[f] for f in ID_FIELDS if meta.get(f)})
            entries.append(entry)
            counts["new"] += 1
            action = "added"
            shown = entry
        if notes:
            report.append(f"- {line}  →  " + "; ".join(notes))
        save_entries(media_type, entries)
        print(f"{prefix} → {action}: {shown['title']} / {shown.get('creator') or '?'}"
              f"{'' if has_cover(shown) else ' [title card]'}")

    write_report_section(media_type, status_notes + report)
    title_card_section()
    print(f"\nDone: {counts['new']} added, {counts['updated']} existing, {counts['cached']} from cache.")
    print(f"API calls: {stats['api']}, images downloaded: {stats['images']}. "
          f"{media_type}.yaml has {len(entries)} entries; {len(status_notes + report)} lines in report.")


def pick_entry(media_type, title, creator):
    """Loose title match (case, punctuation, full-width characters); falls back to titles
    containing it. Stops with a list if more than one entry matches."""
    entries = load_entries(media_type)
    found = find_entries(entries, title, creator)
    if not found:
        q = norm(title)
        found = [e for e in entries if q and q in norm(e.get("title"))
                 and (creator is None or norm(e.get("creator")) == norm(creator))]
    if not found:
        sys.exit(f'No {media_type} entry titled "{title}".')
    if len(found) > 1:
        listing = "\n".join(f'  - {e["title"]} / {e.get("creator") or "?"}' for e in found)
        sys.exit(f'Several {media_type} entries match "{title}":\n{listing}\n'
                 f"Use the full title, or add --creator.")
    return entries, found[0]


def cover_filename(media_type, entry, entries):
    """Reuse the entry's own file, else a title slug not used by another entry."""
    if has_cover(entry):
        return Path(entry["cover"]).name
    used = {Path(e["cover"]).name for e in entries if has_cover(e) and e is not entry}
    name = slugify(entry["title"]) + ".jpg"
    if name in used or (IMG_DIR / media_type / name).exists():
        name = slugify(f"{entry['title']} {entry.get('creator') or ''}") + ".jpg"
    return name


OG_IMAGE = re.compile(
    r'<meta\s+[^>]*?(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image)["\'][^>]*?content=["\']([^"\']+)'
    r'|<meta\s+[^>]*?content=["\']([^"\']+)["\'][^>]*?(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image)["\']',
    re.I)


def source_image(source):
    """Image bytes from a local file, a direct image URL, or a web page's og:image
    (Spotify, Bandcamp, SteamGridDB...). Bandcamp art is upgraded to its largest size (_10)."""
    if not source.startswith(("http://", "https://")):
        path = Path(source).expanduser()
        if not path.is_file():
            sys.exit(f"No such file: {source}")
        return path.read_bytes(), str(path)
    try:
        response = request("GET", source, label="source", api=False)
        if response.headers.get("Content-Type", "").startswith("image/"):
            return response.content, source
        match = OG_IMAGE.search(response.text)
        if not match:
            sys.exit("That page has no og:image. Pass a direct image URL or a file instead.")
        image_url = urljoin(source, html.unescape(match.group(1) or match.group(2)))
        if "bcbits.com" in image_url or "bandcamp.com" in source:
            image_url = re.sub(r"_\d+\.(jpg|jpeg|png)$", r"_10.\1", image_url)
        return request("GET", image_url, label="image", api=False).content, image_url
    except FetchError as e:
        sys.exit(f"Download failed: {e}")


def save_cover(media_type, data, filename):
    """Centre-crop to the card shape (music 1:1, others 2:3), resize without upscaling,
    flatten transparency onto black, save as JPEG q85. Returns the final (width, height)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (OSError, Image.DecompressionBombError):
        sys.exit("That isn't an image Pillow can read.")
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, (0, 0, 0))
        img.paste(rgba, mask=rgba.getchannel("A"))
    else:
        img = img.convert("RGB")
    ratio = 1 if media_type == "music" else 2 / 3  # width / height
    w, h = img.size
    if w / h > ratio:
        new_w = round(h * ratio)
        img = img.crop(((w - new_w) // 2, 0, (w - new_w) // 2 + new_w, h))
    else:
        new_h = round(w / ratio)
        img = img.crop((0, (h - new_h) // 2, w, (h - new_h) // 2 + new_h))
    img.thumbnail(IMAGE_MAX.get(media_type, PORTRAIT_MAX), Image.LANCZOS)  # never upscales
    dest = IMG_DIR / media_type / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=85, optimize=True)
    return img.size


def cmd_setcover(media_type, title, source, creator=None):
    entries, entry = pick_entry(media_type, title, creator)
    data, origin = source_image(source)
    filename = cover_filename(media_type, entry, entries)
    width, height = save_cover(media_type, data, filename)
    entry["cover"] = f"/img/media/{media_type}/{filename}"
    entry["cover_locked"] = True  # imports never replace a hand-set cover
    save_entries(media_type, entries)
    title_card_section()
    size = (IMG_DIR / media_type / filename).stat().st_size
    print(f"{entry['title']} / {entry.get('creator') or '?'}: cover from {origin}")
    print(f"Saved site/static{entry['cover']} ({width}x{height}, {size // 1024} KB).")


def cmd_localize(media_type, title, creator=None):
    entries, entry = pick_entry(media_type, title, creator)
    cover = str(entry.get("cover", ""))
    if not cover.startswith(("http://", "https://")):
        sys.exit(f"Cover is not a remote URL: {cover or '(none)'}. Use setcover to add one.")
    cmd_setcover(media_type, title, cover, creator)


def cmd_prune_singles():
    """Remove music entries whose release type is Single. Uses the cached type when known,
    otherwise asks MusicBrainz (or Deezer for Deezer-only entries) once and caches it."""
    cache = load_cache()
    by_id = {}
    for key, value in cache.items():
        if key.startswith("music|") and value.get("meta"):
            for ident in entry_ids(value["meta"]):
                by_id[ident] = value["meta"]

    entries = load_entries("music")
    singles, unknown = [], []
    for n, e in enumerate(entries, 1):
        meta = next((by_id[i] for i in entry_ids(e) if i in by_id), None)
        kind = (meta or {}).get("primary_type")
        if kind is None:
            try:
                if e.get("mbid"):
                    rg = get_json(f"{MB_API}/release-group/{e['mbid']}", {"fmt": "json"}, label="MusicBrainz")
                    kind = rg.get("primary-type") or "Unknown"
                elif e.get("deezer_id"):
                    kind = (get_json(f"{DEEZER_API}/album/{e['deezer_id']}", label="Deezer album")
                            .get("record_type") or "unknown").title()
                else:
                    kind = "Unknown"
            except FetchError as err:
                print(f"[{n}/{len(entries)}] {e['title']} → lookup error: {err}")
                unknown.append(e)
                continue
            if meta is not None:
                meta["primary_type"] = kind
                save_cache(cache)
        print(f"[{n}/{len(entries)}] {e['title']} / {e.get('creator')} → {kind}")
        if kind == "Single":
            singles.append(e)

    kept = [e for e in entries if e not in singles]
    save_entries("music", kept)
    remove_unused_images("music", singles, kept)
    dropped = remove_input_lines("music", singles, cache)
    # keep singles removed by earlier runs; "could not check" lines are refreshed each run
    earlier = [l for l in read_report_section("music singles removed") if not l.startswith("- could not check")]
    lines = list(dict.fromkeys(earlier + [f"- {e['title']} / {e.get('creator')}" for e in singles]))
    lines += [f"- could not check: {e['title']} / {e.get('creator')}" for e in unknown]
    write_report_section("music singles removed", lines)
    title_card_section()
    print(f"\nRemoved {len(singles)} singles; dropped {len(dropped)} lines from music.txt. "
          f"API calls: {stats['api']}.")


def genre_whitelist():
    """Every detailed genre in genre_groups.yaml, keyed by its normalised form."""
    groups = yaml.safe_load((DATA_DIR / "genre_groups.yaml").read_text(encoding="utf-8")) or []
    return groups, {norm(g): g for group in groups for g in group.get("genres", [])}


def lastfm_tags(cache, method, **params):
    """Last.fm top tags for an album or artist, cached. Unknown albums/artists give []."""
    key = "lastfm|" + method + "|" + "|".join(norm(v) for v in params.values())
    if key not in cache:
        query = {"method": method, "api_key": os.environ["LASTFM_API_KEY"], "format": "json",
                 "autocorrect": 1, **params}
        try:
            data = get_json(LASTFM_API, query, label="Last.fm")
        except FetchError as e:
            if "returned HTTP 4" not in str(e):  # 4xx = not found; 429/5xx already retried
                raise
            data = {"error": str(e)}
        tags = [] if "error" in data else [t["name"] for t in data.get("toptags", {}).get("tag", [])]
        cache[key] = tags
        save_cache(cache)
    return cache[key]


def genre_groups_of(genres, groups):
    found = [g["group"] for g in groups if set(map(str.lower, genres)) & set(g["genres"])]
    unmapped = [x for x in genres if not any(x.lower() in g["genres"] for g in groups)]
    return found + (["other"] if unmapped or not genres else [])


def cmd_regenre(media_type):
    if media_type != "music":
        sys.exit("regenre supports music only.")
    if not os.environ.get("LASTFM_API_KEY"):
        sys.exit("LASTFM_API_KEY is not set (add it to .env at the repo root).")
    cache = load_cache()
    groups, whitelist = genre_whitelist()
    entries = load_entries(media_type)

    # titles: strip edition suffixes; prefer the input file's spelling when it's the same title
    inputs = {}
    if input_path(media_type).exists():
        for line in input_lines(input_path(media_type)):
            try:
                item = parse_line(media_type, line)
            except ValueError:
                continue
            inputs[(norm(item["title"]), norm(item["creator"]))] = item["title"]
    renamed = []
    for e in entries:
        stripped = strip_edition_suffix(e["title"])
        if stripped != e["title"]:
            new = inputs.get((norm(stripped), norm(e.get("creator"))), stripped)
            renamed.append(f'- {e["title"]} → {new}')
            e["title"] = new

    def keep(tags):
        kept = []
        for t in tags:
            genre = whitelist.get(norm(t))
            if genre and genre not in kept:
                kept.append(genre)
        return kept[:MAX_GENRES]

    untagged = []
    for n, e in enumerate(entries, 1):
        if e.get("genres_locked"):
            continue
        artist = e.get("creator") or ""
        try:
            kept = keep(lastfm_tags(cache, "album.getTopTags", artist=artist, album=e["title"]))
            if not kept:
                kept = keep(lastfm_tags(cache, "artist.getTopTags", artist=artist))
        except FetchError as err:
            print(f"[{n}/{len(entries)}] {e['title']} → lookup error, kept old genres: {err}")
            continue
        e["genre"] = kept
        if not kept:
            untagged.append(f'- {e["title"]} / {artist}')
        print(f"[{n}/{len(entries)}] {e['title']} / {artist} → {', '.join(kept) or '(no usable tags)'}")

    save_entries(media_type, entries)
    write_report_section("music genres: no usable Last.fm tags", untagged)
    if renamed:
        write_report_section("music titles: edition suffixes stripped", renamed)

    counts = {g["group"]: 0 for g in groups}
    for e in entries:
        for group in dict.fromkeys(genre_groups_of(e.get("genre", []), groups)):
            counts[group] = counts.get(group, 0) + 1
    print(f"\nAPI calls: {stats['api']}. {len(untagged)} albums without usable tags; "
          f"{len(renamed)} titles stripped.\nGenre groups:")
    for group, count in counts.items():
        print(f"  {group:28} {count}")


LASTFM_USER = "rudjj"
SCROBBLES_FILE = IMPORT_DIR / "lastfm_scrobbles.json"
SCROBBLES_PARTIAL = IMPORT_DIR / "lastfm_scrobbles.partial.json"


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def fetch_scrobbles(since=None):
    """Every scrobble newer than `since` (unix time), oldest first, as [uts, artist, album].
    Pages are fetched with a fixed upper bound ("to"), so an interrupted run resumes from
    scrobbles.partial.json without pages shifting under it."""
    partial = json.loads(SCROBBLES_PARTIAL.read_text(encoding="utf-8")) if SCROBBLES_PARTIAL.exists() else None
    if partial and partial.get("since") == since:
        print(f"Resuming from page {partial['page'] + 1}")
    else:
        partial = {"since": since, "to": int(time.time()), "page": 0, "pages": None, "scrobbles": []}

    while partial["pages"] is None or partial["page"] < partial["pages"]:
        params = {"method": "user.getrecenttracks", "user": LASTFM_USER, "limit": 200,
                  "page": partial["page"] + 1, "to": partial["to"],
                  "api_key": os.environ["LASTFM_API_KEY"], "format": "json"}
        if since:
            params["from"] = since + 1
        try:
            data = get_json(LASTFM_API, params, label="Last.fm recent tracks")
        except FetchError as e:
            try:
                data = e.response.json() if e.response is not None else {}
            except ValueError:
                data = {}
            if "error" not in data:
                raise
        if "error" in data:
            message = str(data.get("message", ""))
            if data["error"] == 17 or "private" in message.lower() or "logged in" in message.lower():
                sys.exit(f"Last.fm says {LASTFM_USER}'s listening history is private ({message}). "
                         "Make it public in Last.fm privacy settings, then re-run.")
            sys.exit(f"Last.fm error {data['error']}: {message}")
        recent = data.get("recenttracks", {})
        partial["pages"] = int(recent.get("@attr", {}).get("totalPages", 0))
        tracks = recent.get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        for t in tracks:
            if t.get("@attr", {}).get("nowplaying") or "date" not in t:
                continue  # the "now playing" entry has no timestamp
            partial["scrobbles"].append([int(t["date"]["uts"]), t.get("artist", {}).get("#text", ""),
                                         t.get("album", {}).get("#text", "")])
        partial["page"] += 1
        save_json(SCROBBLES_PARTIAL, partial)
        if partial["page"] % 10 == 0 or partial["page"] == partial["pages"]:
            print(f"  page {partial['page']}/{partial['pages']} ({len(partial['scrobbles'])} scrobbles)")

    SCROBBLES_PARTIAL.unlink(missing_ok=True)
    return sorted(partial["scrobbles"])


def cmd_lastfm_dates():
    """Set date ("YYYY-MM" of the first scrobble) on completed music entries that have none."""
    if not os.environ.get("LASTFM_API_KEY"):
        sys.exit("LASTFM_API_KEY is not set (add it to .env at the repo root).")
    saved = json.loads(SCROBBLES_FILE.read_text(encoding="utf-8")) if SCROBBLES_FILE.exists() else []
    since = max((s[0] for s in saved), default=None)
    print(f"{len(saved)} scrobbles saved; fetching {'newer than ' + time.strftime('%Y-%m-%d %H:%M', time.gmtime(since)) if since else 'the full history'}...")
    new = fetch_scrobbles(since)
    scrobbles = sorted({tuple(s) for s in saved + new})
    save_json(SCROBBLES_FILE, [list(s) for s in scrobbles])
    print(f"{len(new)} new; {len(scrobbles)} scrobbles in total")

    def album_keys(title):
        return {norm(strip_edition_suffix(title)), norm(strip_edition(title))} - {""}

    # earliest scrobble per (album key) → list of (artist, uts); scrobbles are oldest first
    first = {}
    for uts, artist, album in scrobbles:
        for key in album_keys(album):
            seen = first.setdefault(key, {})
            seen.setdefault(norm(artist), uts)

    entries = load_entries("music")
    filled, missing = [], []
    for e in entries:
        if e.get("status") != "completed" or e.get("date"):
            continue  # never overwrite an existing date
        hits = [uts for key in album_keys(e["title"]) for artist, uts in first.get(key, {}).items()
                if artist == norm(e.get("creator")) or creator_ok(e.get("creator"), artist)]
        if hits:
            e["date"] = time.strftime("%Y-%m", time.gmtime(min(hits)))
            filled.append(f'- {e["title"]} / {e.get("creator")} → {e["date"]}')
        else:
            missing.append(f'- {e["title"]} / {e.get("creator")}')
    save_entries("music", entries)

    earliest = time.strftime("%Y-%m-%d", time.gmtime(scrobbles[0][0])) if scrobbles else "none"
    write_report_section("music dates from Last.fm", [
        f"- filled {len(filled)} dates; earliest scrobble in the history: {earliest}",
        f"- {len(missing)} completed albums without a date have no matching scrobbles:", *missing])
    print(f"\nFilled {len(filled)} dates; {len(missing)} albums with no matching scrobbles. "
          f"Earliest scrobble: {earliest}. API calls: {stats['api']}.")


def cmd_check():
    notes = []
    for t in TYPES:
        notes += check_type(t)
    write_report_section("converted statuses", notes)
    title_card_section()
    print("\n".join(notes) or "No statuses needed converting.")


def main():
    load_env()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="import entries from a text file")
    imp.add_argument("type", choices=TYPES)
    imp.add_argument("file")
    imp.add_argument("--status", choices=ALL_STATUSES, required=True, help="default status")
    imp.add_argument("--retry", action="store_true", help="re-resolve weak/failed matches and missing covers")
    for name, helptext in (("setcover", "set an entry's cover from a local file or URL"),
                           ("localize", "download an entry's remote cover into site/static")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("type", choices=TYPES)
        p.add_argument("title")
        if name == "setcover":
            p.add_argument("source", help="image file, image URL, or a page URL (uses its og:image)")
        p.add_argument("--creator")
    sub.add_parser("prune-singles", help="remove music entries that are singles")
    sub.add_parser("check", help="convert disallowed statuses and list title-card entries")
    sub.add_parser("lastfm-dates", help="date completed albums by their first Last.fm scrobble")
    rg = sub.add_parser("regenre", help="re-tag genres from Last.fm (music)")
    rg.add_argument("type", choices=["music"])
    args = parser.parse_args()

    if args.command == "import":
        cmd_import(args.type, args.file, args.status, args.retry)
    elif args.command == "setcover":
        cmd_setcover(args.type, args.title, args.source, args.creator)
    elif args.command == "localize":
        cmd_localize(args.type, args.title, args.creator)
    elif args.command == "prune-singles":
        cmd_prune_singles()
    elif args.command == "lastfm-dates":
        cmd_lastfm_dates()
    elif args.command == "regenre":
        cmd_regenre(args.type)
    else:
        cmd_check()


if __name__ == "__main__":
    main()

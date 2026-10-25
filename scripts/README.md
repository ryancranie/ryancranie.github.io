# Media import script

`import_media.py` maintains the media section's data (`site/data/media/*.yaml`) and covers
(`site/static/img/media/<type>/`). The YAML files are the source of truth: the script adds new
entries and fills gaps, but never changes a title, status, rating, date, locked genres or a
locked cover you've set.

## Setup

Python 3 with three packages: `requests`, `PyYAML`, `Pillow` (listed in `requirements.txt`).
If your system Python already has them (e.g. installed through your package manager), there's
nothing to set up. Otherwise use a virtualenv in the repo root:

```sh
python -m venv .venv
.venv/bin/python -m pip install -r scripts/requirements.txt
source .venv/bin/activate   # then plain `python` works in this shell
```

API keys go in `.env` at the repo root (gitignored, never printed):

```
TMDB_API_KEY=...     # movies and tv imports
LASTFM_API_KEY=...   # regenre and lastfm-dates
```

## Commands

Run from the repo root:

```sh
# import a list (scripts/import/<type>.txt); --status is the default for lines without one
python scripts/import_media.py import movies scripts/import/movies.txt --status planning
python scripts/import_media.py import music scripts/import/music.txt --status completed --retry

# set a cover by hand: a file, a direct image URL, or a page URL (uses its og:image,
# e.g. Spotify, Bandcamp, SteamGridDB). Cropped to the card shape, saved locally, and
# marked cover_locked so imports never replace it.
python scripts/import_media.py setcover games "osu!" ~/Downloads/osu.png
python scripts/import_media.py setcover music "OK Computer" https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE

# music upkeep
python scripts/import_media.py regenre music      # genres from Last.fm tags
python scripts/import_media.py lastfm-dates       # first-listen dates from scrobbles
python scripts/import_media.py prune-singles      # drop singles

# tidy statuses and list entries still on title cards
python scripts/import_media.py check
```

`python scripts/import_media.py <command> --help` shows each command's options.

Anything that needs a manual look (weak matches, missing covers or genres) is written to
`scripts/import/report.txt`. API results are cached in `scripts/import/cache.json`, so
re-running an import makes no API calls for entries it has already resolved.

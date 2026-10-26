# Media import script

`import_media.py` maintains the media section's data (`site/data/media/*.yaml`) and covers
(`site/static/img/media/<type>/`). The YAML files are the source of truth: the script adds new
entries and fills gaps, but never changes a title, status, rating, date, locked genres or a
locked cover you've set. Fields it doesn't manage, like `comment` (hover text on the card), are kept
as they are.

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
LASTFM_API_KEY=...   # regenre and lastfm-sync
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
python scripts/import_media.py lastfm-sync        # scrobbles → music_lastfm.json (also runs in CI)
python scripts/import_media.py regenre music      # genres from Last.fm tags
python scripts/import_media.py prune-singles      # drop singles (and add them to the exclude list)

# tidy statuses and list entries still on title cards
python scripts/import_media.py check
```

`python scripts/import_media.py <command> --help` shows each command's options.

Anything that needs a manual look (weak matches, missing covers or genres) is written to
`scripts/import/report.txt`. API results are cached in `scripts/import/cache.json`, so
re-running an import makes no API calls for entries it has already resolved.

## Music and Last.fm

Music is driven by Last.fm scrobbles (user `rudjj`). Three files in `site/data/media/`:

- `music.yaml`: hand-kept, never written by the sync. Per album: title, creator, year, genre,
  genres_locked, rating, comment, nsfw, cover, cover_locked, mbid, and `listened: true` to mark
  an album listened without full scrobbles. No status or date: those are derived.
- `music_exclude.yaml`: albums (title + creator) never shown, even when fully scrobbled.
- `music_lastfm.json`: written by `lastfm-sync` only (committed). Per album: scrobbled tracks,
  tracklist, last listened, complete, release type, and for auto-added albums cover and genres.

An album is **listened** when at least 70% of its tracklist (Last.fm `album.getInfo`, else the
earliest official MusicBrainz release; bonus tracks ignored) has been scrobbled at least once, or
it has `listened: true`. Albums in `music.yaml` below that show as **to listen**.
Changing an entry's `mbid` in `music.yaml` pins its tracklist to that MusicBrainz release group
(e.g. American Football's 1999 LP rather than the same-named EP and later LPs). Last listened
only counts tracks on the album's tracklist. Complete albums not in `music.yaml` are auto-added when MusicBrainz says
Album or EP (not compilation, soundtrack, live, remix or DJ-mix), or, with no MusicBrainz match,
when the tracklist has 4+ tracks. The date shown is the last listen.

The sync fetches only scrobbles newer than the last run. The first run seeds from
`scripts/import/lastfm_scrobbles.json` (gitignored) when it has track names, otherwise it pages
through the full history and saves it there. Each run writes `scripts/import/lastfm_sync_report.txt`.

The deploy workflow runs the sync on every push and every 3 hours (`LASTFM_API_KEY` repo secret),
commits any changes as "music: lastfm sync", then builds. If the sync fails, the site still
deploys from the committed data.

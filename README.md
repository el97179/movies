# movies

Scans the video library (local `~/Videos` plus the Saronic NAS), matches each title
against [OMDb](https://www.omdbapi.com/) for poster, plot and ratings, adds a few
extras from [Wikidata](https://www.wikidata.org/), and writes a static gallery to
`~/Videos/movies.html`.

## Setup

- Python 3, standard library only. `build.py` is the whole program.
- A free OMDb key: `export OMDB_API_KEY=<key>` ([get one](https://www.omdbapi.com/apikey.aspx)).
- SSH access to the NAS without a password prompt (key-based), for the NAS sources.
- Optional: `ffprobe` locally (from ffmpeg) for embedded-subtitle detection, and VLC for
  the ▶ Play buttons.

`~/.local/bin/movies-db` is a symlink to `build.py`, so it runs as `movies-db` from
anywhere. `cache.json`, `corrections.json` and `renames.log` always live in this repo,
whichever way it's invoked: the symlink is resolved.

| Env var | Default | What |
|---|---|---|
| `OMDB_API_KEY` | (required) | OMDb API key |
| `SARONIC_IP` | `saronic` | SSH host for the NAS: an `~/.ssh/config` alias or `user@host` |
| `SARONIC_SMB` | `saronic.local` | SMB host the ▶ Play button mounts NAS shares from |

## Usage

```bash
movies-db                      # scan + look up (cached) + write ~/Videos/movies.html
movies-db --refresh            # ignore the cache and re-hit OMDb for every title (see below)
movies-db --selftest           # parser and logic self-checks, no network
movies-db --rename --dry-run   # show what --rename would change
movies-db --rename             # rename files/folders to Title_(year); writes no page
movies-db --install-handler    # once per machine: make ▶ Play open VLC
```

A plain run never renames or moves video files; only `--rename` does.

## Sources

| Source | Where | Read via |
|---|---|---|
| Local | `~/Videos`, `~/Videos/_watched` | the filesystem |
| Saronic | `~/Videos/movies`, `~/Videos/series` (+ their `_watched/`) on the NAS | one SSH call per folder (`ls`, `stat`, `find`) |
| WD | `/volumeUSB1/usbshare/Videos` (+ `_watched/`), the USB disk on the NAS | same SSH calls |

Each top-level file or folder is one entry. Anything in a `_watched/` folder is marked
watched. The same title found in several places becomes one card listing every copy.

### Series or movie?

An entry is a series when its name has `S01`, `Season N`, `Complete Series` or
`[series]`/`(series)`, carries a year range like `(2008-2013)` or `(2022-)`, or sits
under a `series/` folder. Once OMDb matches it, OMDb's type wins.

## The page

- **Search** matches the title, original title, director and actors, ignoring case and
  accents ("zorbas" finds *Ζορμπάς*). Plot isn't searched.
- **Checkboxes:** Movies / Series, and Local / Saronic / WD. **Watched** switches the
  page to only the watched titles; unticked, only the unwatched ones.
- **Genres:** tick one or more; a title shows if it has any of them.
- **Sort** by Name, Year, IMDb rating, RT rating or Recently added (the folder's date on
  disk). Name defaults to ▲ Asc, every other sort to ▼ Desc; the button flips it.
- **Each card** shows the poster, the original title for international films (from
  Wikidata), rating links (IMDb opens the IMDb page, 🍅 the film's Rotten Tomatoes page,
  or an RT search when Wikidata has no RT ID), where each copy is, ✓ Watched, ✓ Subs,
  genre, runtime, seasons, "Added in <year>", director, actors and plot.
- **Click the poster** for a larger view with the full plot.
- **Trailer** opens a YouTube search; **▶ Play** opens the video in VLC (see below).

### ✓ Subs

Shown when a title has subtitles. Hover for the languages; "language unknown" means
there are subtitles but no file name or stream tag says which language.

- **Sidecar files** (`.srt .sub .idx .ass .ssa .vtt`) anywhere in the title's folder
  (`Subs/`, `Subtitles/`, nested release folders), or beside a bare video file sharing
  its name. Languages come from the file name: `_eng`, `.el`, `.gr`, `2_English`,
  `spa.HI` ("HI" is hearing impaired, not Hindi). These are re-listed on every run.
- **Embedded streams** in the title's largest video: `ffprobe` locally, the NAS's own
  `ffmpeg` remotely (it has no `ffprobe`). Each file is probed once and remembered in
  `cache.json` until its size or date changes. A series is judged by one episode.
- Subtitles burned into the picture can't be detected.

### ▶ Play

Opens the title in VLC: the local copy if there is one, else Saronic, then WD. A movie
folder plays its largest video; a series folder opens whole, so VLC queues the episodes.

- **Setup:** run `movies-db --install-handler` once. It registers the `movies-play://`
  link type (`~/.local/share/applications/movies-play.desktop`, which runs
  `movies-db --play <link>`). The Firefox snap hands such links to xdg-desktop-portal,
  whose "Open with" chooser never launched the handler, so the command also saves that
  choice in the portal's permission store (`desktop-used-apps`). The first click may
  still ask whether the page may open the link.
- **NAS copies** play over **SMB** through GNOME's gvfs mount of the share (`home` for
  Saronic, `usbshare1` for the WD), mounted on demand with the password saved in the
  keyring. Open `smb://saronic.local/` in Files once and save the password. VLC sees a
  normal file path and streams it; nothing is copied. (VLC's own `smb://` input fails
  against this NAS, and its SFTP service is off, so the mount is the only route.)
- The handler only opens paths inside the library folders, since any web page could
  fire a `movies-play://` link. When the NAS can't be reached it shows a desktop
  notification.

## Data files

### `cache.json`: lookups

One entry per lookup: keyed by `Title|year|type` as parsed from the name, or by the IMDb
ID for a pinned correction. The value is OMDb's record, plus what later steps add to it:
`OrigTitle` and `RTid` (from Wikidata) and `Subs` (subtitle probes per copy).

- New or renamed folders are looked up automatically. A rename that parses to the same
  title and year still hits the cache.
- Matched titles never expire.
- "Not found" results are kept too, and retried only when `SEARCH_V` in `build.py` is
  bumped (because the search got smarter). Network errors are never cached.
- `--refresh` re-hits every title: about one call per title against a daily OMDb quota
  (1,000 on the free key). To redo a few titles, delete their keys from `cache.json`
  instead.
- When OMDb answers 401 (quota spent, or a bad key), lookups stop. The run saves what it
  fetched to `cache.json` and leaves `movies.html` as it was, so a half-looked-up page
  never replaces a good one. Re-run later to finish.

### `corrections.json`: fixing a wrong or missing match

Keyed by the file or folder name. Each correction can set `title`, `year`, `imdb_id` (or
a pasted IMDb URL, which wins over the title), or `"ignore": true` to hide an entry:

```json
"Trolosa": {"imdb_id": "tt0157122"},
"Some.Sample.Folder": {"ignore": true}
```

The easiest way to add one: click the ✎ pencil on a card in the page, fill in a title and
year or paste an IMDb URL, then **Download fixes**. The next run picks up
`~/Downloads/corrections.json` (only if it's newer than the repo's), merges it, and
renames the download to `corrections.json.imported`.

`--rename` moves a correction to the folder's new name, so the pin keeps working. It saves
after each rename, so a run that's cut short can't lose a pin.

### `--rename` and `renames.log`

`--rename` gives every *matched* title a canonical name. Check `--dry-run` first: the
sweep covers every matched title, so a wrong match renames a real folder to the wrong name.

- **Movies:** `Title_(year)`, e.g. `The-Holiday_(2006)`. Spaces become `-`, and
  characters not allowed in file names are dropped.
- **Series:** the run, `Dark_(2017-2020)` when finished or `Pluribus_(2025-)` when still
  airing.
- **Year:** the name's own year is kept when OMDb's is within 2 years of it. OMDb often
  gives the US release year ("Take the Money and Run" 1969 → 1970).
- **Files** keep their video extension. A folder named like `X.mkv` doesn't keep the
  extension.
- **Skipped:** unmatched titles, name collisions, and targets that already exist.

`renames.log` is the undo trail, appended after each rename and never read by the
program. One tab-separated line per rename: time, `local`/`remote`, old path, new path.

```
2026-10-18 12:45:14	local	/home/tanman/Videos/Anatomy-Of-A-Fall_(2024)	/home/tanman/Videos/Anatomy-of-a-Fall_(2024)
```

To undo one, move the new path back to the old: `mv -n "<new>" "<old>"` locally, or
`ssh saronic 'mv -n "<new>" "<old>"'` for `remote` lines (leave a leading `~/` outside
the quotes so it expands). If that title had a correction, rename its key in
`corrections.json` back too.

# Plan: "Recently added" sort + genre filter

Sources: 2 scouts (sort/date path, genre/filter path) + 1 researcher (file timestamps). 2026-10-18.

## Code map (all in `build.py`, single file)

- Config: `VIDEOS=~/Videos` (:33), `SARONIC_IP` env (:34, locally `tanman@192.168.0.100`), `CACHE`/`CORRECTIONS_FILE` in repo, `OUT=~/Videos/movies.html` (:40), `VIDEO_EXT` (:42).
- Discovery: `_entry` filter (:179-190); `remote_listing` = `ssh $SARONIC_IP "ls -1p <path>"` (:193-228, cmd at :208), names + dir flag only, **no timestamps**; `REMOTE_DIRS` (:231-238: Saronic ~/Videos/movies, ~/Videos/series + `_watched`; WD /volumeUSB1/usbshare/Videos + `_watched`); `scan()` (:241-254) iterdirs local ~/Videos and `_watched` one level, then remote. NAS never mounted.
- Movie entries are **folders** (verified locally: all ~/Videos/* entries are directories).
- OMDb: fetch at :332, cache key `imdb_id` or `"title|year|typ"` (:264), stored in `cache.json`.
- Record: `build_entry` (:354-379) emits title, year, years, type, director, actors, genre, imdb, rt, imdb_url, plot, runtime, seasons, poster, trailer, matched, raw, sources, paths (list of [source, base, raw]), watched. No date field.
  - `genre` (:363) = OMDb `Genre` string, comma-separated (`"Comedy, Drama, Romance"`), `""` if unmatched, can be literal `"N/A"` (2 cache entries).
- `merge_duplicates` (:382-399) keys on (lowercased title, year); local + NAS copies collapse into one record, first copy's fields win, `paths` accumulates.
- Front end: `HTML = """` template (:402-659), vanilla JS, no framework. `generate()` (:662-665) inlines JSON via `__DATA__`/`__CORRECTIONS__`.
  - Header controls bar `<header>` :471-493. Search `#q` :473 (title+director+actors, :588 — genre not searched). Type checkboxes cMovies/cSeries :474-475; source cLocal/cSaronic/cWD :477-479; cWatched :481 (equality filter :591); `.sep` dividers :476/:480/:482/:488; `label.cb` CSS :450-451.
  - Sort `<select id="sort">` :483-486 (title, year, imdb, rt), no `selected` → Name default. Dir button `#dir` :487, `let asc=true` :583, toggle :652.
  - `num()` :584 (non-numeric → -1). `render()` :585-603: filter :588-591, sort :592-598 (title = lowercase string compare, others num()), reverse :599, count :600, grid :602. `card()` :605-651; genre shown as plain text in `.meta` (:640 card, :568 zoom).
  - Listeners :652-655. No URL/localStorage state for sort/filters (localStorage only `mov_corrections` :519-524).
- Rename: `rename_entries` (:747-856), only with `--rename` (:1077-1082). Same-dir rename / `mv -n` → keeps inode.
- Existing timestamp use: only :722-723 (pickup corrections.json mtime).
- Tests: no test files/config. Only `python3 build.py --selftest` (`selftest()` :917-1018, `_search_selftest()` :859, called :951), asserts, no network, SARONIC_IP=nas.invalid in fixtures.

## Genre data
- 743 cached Genre values, 26 canonical OMDb tokens, consistent casing: Action, Adventure, Animation, Biography, Comedy, Crime, Documentary, Drama, Family, Fantasy, Film-Noir, History, Horror, Music, Musical, Mystery, News, Reality-TV, Romance, Sci-Fi, Short, Sport, Talk-Show, Thriller, War, Western. Plus `N/A` ×2 (exclude from filter options).
- Split on `", "` is sufficient; options can be derived in JS from the embedded data (no build-side change needed).

## Creation-date findings (researcher)
- Python 3.13.5 on Linux: no `st_birthtime`, no `os.statx`. [VERIFIED] Birth time only via `stat -c %W` subprocess.
- `~/Videos` is ext4; `%W` works. [VERIFIED]
- **Birth time does not survive any copy** (cp, cp -p, rsync -a, scp -p, cross-fs mv all create a new inode). Only mtime can survive, and only with mtime-preserving copies. [operations UNVERIFIED from training; consistent with local data]
- Local evidence [VERIFIED]: 4 folders share an identical birth time (1787134046, one bulk copy); mtimes mixed — some preserved (2025-09, 2024-12), some ≈ copy time. ⇒ Neither birth time nor mtime on the laptop reliably means "date added".
- NAS (Synology DSM, likely BusyBox): `stat -c %Y` believed supported, `find -printf` not. [UNVERIFIED — ssh check was blocked by the permission system; not run]

## Open questions — resolved
1. **Date source → folder mtime, read every build, nothing persisted** (user choice). Birth time rejected (changes on every copy). NAS mtimes fetched in the same ssh call as `ls -1p`, fail-soft (missing → `null`). Merged copies take the **oldest** mtime. NAS stat output VERIFIED by user 2026-10-18: `ssh saronic 'cd ~/Videos/movies && stat -c "%Y|%n" -- *'` prints `1535917113|12-Angry-Men_(1957)` lines, matching `parse_remote_listing`'s `digits|name` format.
2. **Genre filter → multi-select, match any** (user choice). Native `<details>` with checkboxes, tokens built in JS, `N/A`/empty dropped.

## Constraints found while speccing
- Single `scan()` consumer at build.py:1059 unpacks a 7-tuple; `build_entry` callers at :954, :984 (selftest), :1073.
- `HTML` is not a raw string → no backslashes in new JS.
- Listeners at :655 are bound once to fixed elements; dynamic genre checkboxes need delegation.
- `asc=true` global; "Recently added" must flip to descending.
- `node` is installed (/usr/bin/node) → `node --check` usable on extracted script.

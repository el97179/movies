# movies

Scans a local `~/Videos` folder, matches each file against [OMDb](https://www.omdbapi.com/)
to fetch poster/plot/rating info, and generates a static `movies.html` gallery.

Results are cached in `cache.json` so repeat runs don't re-hit the API. Any
manual title/year/IMDb-URL corrections you make in the generated page are
saved to `corrections.json` and re-applied (and used to rename mismatched
files) on the next run.

## Usage

```bash
export OMDB_API_KEY=<key>      # free key at https://www.omdbapi.com/apikey.aspx
python3 build.py               # scan + fetch (cached) + write movies.html
python3 build.py --refresh     # ignore cache, re-hit OMDb (~1 call per title)
python3 build.py --selftest    # run parser self-checks, no network
python3 build.py --no-rename   # don't rename files whose fix was just picked up
```

`~/.local/bin/movies-db` is a symlink to `build.py`, so it can be run as
just `movies-db` from anywhere on PATH. Since `build.py` resolves
`cache.json`/`corrections.json` relative to the invoked path, running it
through this symlink reads/writes those files in `~/.local/bin/` rather
than this repo — keep both copies in sync if you edit them by hand.

## Fixing a wrong or missing match

Open `movies.html`, click the pencil on any card, fill in a title/year or
paste an IMDb URL, then "Download fixes". The next run picks the file up
from `~/Downloads`, applies it, and renames those files to the canonical
name (logged to `renames.log`).

#!/usr/bin/env python3
"""Scan ~/Videos, fetch details from OMDb, generate a static movies.html.

Usage:
  export OMDB_API_KEY=<key>      # free key at https://www.omdbapi.com/apikey.aspx
  python3 build.py               # scan + fetch (cached) + write movies.html
  python3 build.py --refresh     # ignore cache, re-hit OMDb (costs ~1 call per title)
  python3 build.py --selftest    # run parser self-checks, no network
  python3 build.py --rename --dry-run  # show what --rename would change
  python3 build.py --rename      # rename files/folders to <Title>_(<year>), no page

A plain run never renames or moves video files.

Fixing a wrong or missing match:
  open movies.html, hit the pencil on any card, fill in a title/year or paste an IMDb
  URL, then "Download fixes". The next run picks the file up from ~/Downloads and
  applies it; --rename then renames to the canonical name (logged to renames.log).
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.parse
import unicodedata
import urllib.request
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

VIDEOS = Path.home() / "Videos"
SARONIC_IP = os.environ.get("SARONIC_IP", "saronic")  # ~/.ssh/config alias
HERE = Path(__file__).resolve().parent  # through the ~/.local/bin symlink
CACHE = HERE / "cache.json"
CORRECTIONS_FILE = HERE / "corrections.json"
# ponytail: the browser's default download dir; add a flag if it's ever not this.
PICKUP = Path.home() / "Downloads" / "corrections.json"
OUT = VIDEOS / "movies.html"

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".webm"}
SUB_EXT = {".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt"}
# Release-tag tokens; first one marks where the real title ends (used when no year).
JUNK = re.compile(
    r"\b(1080p|2160p|720p|480p|4k|bluray|brrip|bdrip|webrip|web-dl|webdl|web|hdrip|"
    r"dvdrip|x264|x265|hevc|h264|h\.264|aac|aac2|dd5|ac3|5\.1|10bit|remux|"
    r"uncut|screener|hdtv|xvid|repack|proper)\b",
    re.I,
)
YEAR = re.compile(r"\b(19|20)\d\d\b")
TRACKER = re.compile(r"^\s*www\.\S+\s*-\s*", re.I)
# `season\s*\d` not `\bseason\b`: "Season1+2" has no word boundary before the digit.
# Not `seasons?` either -- that would truncate "A Man for All Seasons".
SERIES = re.compile(
    r"[\[(]series[\])]|\b(season\s*\d+|complete\s+series|s\d{2})\b", re.I
)
# Edition/packaging tags OMDb never carries in a title. Query-side only -- never applied
# to the parsed title, or "A Complete Unknown" loses a word. `complete` is SERIES' job.
EDITION = re.compile(
    r"\b(extended|theatrical|edition|ece|uncut|director'?s?[ .]cut|remastered|"
    r"collection|trilogy|duology|making[ .]of|a\.?k\.?a\.?)\b",
    re.I,
)
RANGE = re.compile(
    r"\((19|20)\d\d\s*[-–—]\s*((19|20)\d\d)?\)"
)  # "(2008-2013)"/"(2022-)" -> series run
# Our own --rename output: "The-Holiday_(2006)", "Dark_(2017-2020)", "Pluribus_(series)".
OURS = re.compile(
    r"^(?P<t>.+?)_\((?P<s>series[_ ])?"
    r"(?P<y>series|(?:19|20)\d\d(?:\s*[-–—]\s*(?:(?:19|20)\d\d)?)?)\)$",
    re.I,
)


def parse_name(name):
    """Raw folder/file name -> (title, year_or_None, type). Heuristic. # ponytail:
    regex over a personal library; swap in `guessit` only if it gets too noisy."""
    name = TRACKER.sub("", name)  # drop "www.site.com - " prefix
    name = re.sub(r"\.[^.]+$", "", name) if "." in name[-5:] else name  # file ext

    m = OURS.match(name)  # already renamed by us -> read it back
    if m:
        y = m["y"]
        # "(series)", "(series_2017)" (a legacy form), or a run "(2008-2013)"
        series = bool(m["s"]) or y.lower() == "series" or bool(re.search(r"[-–—]", y))
        return (
            m["t"].replace("-", " ").strip(),
            (None if series and not y[:4].isdigit() else y[:4]),
            ("series" if series else "movie"),
        )

    # separators first: "Lies_Season1" and "Complete.Series" hide the markers below
    name = name.replace(".", " ").replace("_", " ")
    is_series = bool(SERIES.search(name)) or bool(RANGE.search(name))
    name = SERIES.split(name)[0]  # "Pluribus - Season 1" -> "Pluribus"

    m = YEAR.search(name)
    if m:
        year = m.group(0)
        title = name[: m.start()]
    else:
        year = None
        j = JUNK.search(name)
        title = name[: j.start()] if j else name

    title = re.sub(r"[\s\-–_([{]+$", "", title)  # trailing separators / open brackets
    title = re.sub(r"\s+", " ", title).strip(" -–([{")
    return title, year, ("series" if is_series else "movie")


def clean_query(title):
    """Parsed title -> the string worth asking OMDb about."""
    if " " not in title and "-" in title:
        title = title.replace("-", " ")  # our own dash-joined rename output
    title = EDITION.sub(" ", title)
    title = re.sub(r"[\[(].*?[\])]", " ", title)  # "[1080p]", "(Downfall)" handled below
    return re.sub(r"\s+", " ", title).strip(" -–_!.")


def search_candidates(title):
    """Progressively simpler queries, most specific first. Returns (candidates, full)."""
    full = clean_query(title)
    out = [full]
    for seg in re.split(r"\s*[-–—]\s*", full):  # "A.Hitchcock-Vertigo" -> "Vertigo"
        out.append(seg.strip())
    m = re.search(r"\((.+?)\)", title)  # "Der Untergang (Downfall)" -> "Downfall"
    if m:
        out.append(m.group(1).strip())
    words = full.split()
    for n in (1, 2, 3):  # "James Bond Thunderball" -> "Thunderball"
        if len(words) > n:
            out.append(" ".join(words[n:]))
    for n in (1, 2):  # "Its A Wonderful Life" -> "A Wonderful Life" (lost apostrophe)
        if len(words) > n + 1:
            out.append(" ".join(words[:-n]))
    seen, uniq = set(), []
    for c in out:
        if c and c.lower() not in seen:
            seen.add(c.lower())
            uniq.append(c)
    return uniq[:7], full


def _norm(s):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split())


def _sim(a, b):
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _year_of(s):
    return re.sub(r"\D.*$", "", s or "")


def pick(results, full, candidate, year, typ):
    """First search result confident enough to assert, else None.

    Two signals, title and year -- not one tuned threshold. Measured good matches sit at
    0.80 similarity and measured bad ones at 0.82, so similarity alone cannot separate
    them. A wrong poster is worse than a blank one: when in doubt, return None.
    """
    for r in results:
        if r.get("Type") != typ:
            continue
        ry = _year_of(r.get("Year", ""))
        yok = (ry.isdigit() and abs(int(ry) - int(year)) <= 1) if year else None
        sf = _sim(full, r.get("Title", ""))
        # No year means one signal instead of two, so the title must be near-exact:
        # "La Casa de Papel" vs "La Casa De Papa" scores .90 and is a different show.
        if sf >= (0.85 if year else 0.95) and yok is not False:
            return r
        # a low-similarity reduced query ("Downfall") needs the year to vouch for it
        if yok and max(sf, _sim(candidate, r.get("Title", ""))) >= 0.85:
            return r
    return None


def _entry(name, is_dir, source, watched, base):
    """Return (raw, title, year, type, source, watched, base, is_dir) or None if skipped."""
    if name.startswith(".") or name == "_watched":
        return None
    if not is_dir and Path(name).suffix.lower() not in VIDEO_EXT:
        return None
    title, year, typ = parse_name(name)
    if (
        "/series" in base
    ):  # a folder under .../series/ is a series, whatever the name says
        typ = "series"
    return (name, title, year, typ, source, watched, base, is_dir) if title else None


def parse_remote_listing(out):
    """Parse `ls -1p` output, optionally followed by a ::STAT:: section of `mtime|name`
    lines and a ::FILES:: section of `size|mtime|relpath` lines (video and subtitle files
    under the base). Returns [(name, is_dir, mtime_or_None, own_files)]."""
    names, mtimes, files, sect = [], {}, [], None
    for line in out.splitlines():
        if line in ("::STAT::", "::FILES::"):
            sect = line
        elif sect == "::STAT::":
            parts = line.split("|", 1)
            if len(parts) == 2 and parts[0].isascii() and parts[0].isdigit():
                mtimes[parts[1]] = int(parts[0])
        elif sect == "::FILES::":
            parts = line.split("|", 2)
            try:
                files.append((parts[2], int(parts[0]), int(float(parts[1]))))
            except (IndexError, ValueError):
                pass
        elif line.strip():
            names.append((line.rstrip("/"), line.endswith("/")))
    return [(n, d, mtimes.get(n), own_files(n, d, files)) for n, d in names]


def own_files(raw, is_dir, files):
    """The (relpath, size, mtime) files belonging to one entry: everything under a folder,
    or a bare video plus the subtitle files beside it that share its stem."""
    if is_dir:
        return [f for f in files if f[0].startswith(raw + "/")]
    stem = Path(raw).stem
    return [
        f
        for f in files
        if f[0] == raw
        or ("/" not in f[0] and Path(f[0]).suffix.lower() in SUB_EXT
            and f[0].startswith(stem))
    ]


_FIND_NAMES = " -o ".join(f"-iname '*{x}'" for x in sorted(VIDEO_EXT | SUB_EXT))
# release folders nest Subs/ (and Fantomas a whole release dir) a few levels down
FIND_FILES = (
    r"find . -mindepth 1 -maxdepth 5 \( -path ./_watched -o -name @eaDir \) -prune"
    rf" -o -type f \( {_FIND_NAMES} \) -printf '%s|%T@|%P\n'"
)


def remote_listing(path="~/Videos"):
    """Yield (name, is_dir, mtime) for a remote path over SSH; mtime is epoch
    seconds or None, plus the entry's own files (see parse_remote_listing)."""
    if not SARONIC_IP:
        print("  [WARN] SARONIC_IP not set, skipping Saronic", file=sys.stderr)
        return
    try:
        r = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                SARONIC_IP,
                f"ls -1p {path} && (echo ::STAT::; cd {path} && stat -c '%Y|%n' -- *"
                f" 2>/dev/null; echo ::FILES::; {FIND_FILES} 2>/dev/null; true)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as e:
        print(f"  [WARN] NAS ssh {path} failed, skipping: {e}", file=sys.stderr)
        return
    if r.returncode == 2:  # `ls`: no such directory -- an absent _watched/ is normal
        return
    if r.returncode:
        print(
            f"  [WARN] NAS ssh {path} failed, skipping: {r.stderr.strip()}",
            file=sys.stderr,
        )
        return
    yield from parse_remote_listing(r.stdout)


REMOTE_DIRS = [
    ("~/Videos/movies", "Saronic", False),
    ("~/Videos/series", "Saronic", False),
    ("~/Videos/movies/_watched", "Saronic", True),
    ("~/Videos/series/_watched", "Saronic", True),
    ("/volumeUSB1/usbshare/Videos", "WD", False),
    ("/volumeUSB1/usbshare/Videos/_watched", "WD", True),
]


def _local_mtime(child):
    """Epoch seconds, or None if the entry can't be stat'ed (dangling symlink)."""
    try:
        return int(child.stat().st_mtime)
    except OSError:
        return None


def local_files(base):
    """(relpath, size, mtime) of every video/subtitle file under base, like FIND_FILES."""
    out = []
    for root, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if not (d == "_watched" and root == str(base))
                   and d != "@eaDir"]
        for n in names:
            if Path(n).suffix.lower() in VIDEO_EXT | SUB_EXT:
                f = Path(root, n)
                try:
                    st = f.stat()
                except OSError:  # dangling symlink
                    continue
                out.append((str(f.relative_to(base)), st.st_size, int(st.st_mtime)))
    return out


def scan():
    """Yield (raw, title, year, type, source, watched, base_dir, is_dir, mtime, files) per
    library entry; mtime is epoch seconds, or None when a remote stat is unavailable, and
    files are the entry's (relpath, size, mtime) videos and subtitles."""
    for base, watched in ((VIDEOS, False), (VIDEOS / "_watched", True)):
        if not base.is_dir():
            continue
        files = local_files(base)
        for child in sorted(base.iterdir()):
            e = _entry(child.name, child.is_dir(), "Local", watched, str(base))
            if e:
                yield e + (_local_mtime(child), own_files(child.name, e[7], files))
    for path, source, watched in REMOTE_DIRS:
        for name, is_dir, mtime, own in remote_listing(path):
            e = _entry(name, is_dir, source, watched, path)
            if e:
                yield e + (mtime, own)


_LANG_ROWS = """en eng english
el gr gre ell greek
es spa spanish
fr fre fra french
de ger deu german
it ita italian
pt por portuguese
nl dut nld dutch
sv swe swedish
da dan danish
no nor nob nno norwegian
fi fin finnish
pl pol polish
ru rus russian
uk ukr ukrainian
tr tur turkish
ja jpn japanese
ko kor korean
zh chi zho chinese
ar ara arabic
he heb hebrew
fa per fas persian farsi
sr srp serbian
hr hrv croatian
bs bos bosnian
ro rum ron romanian
hu hun hungarian
cs cze ces czech
bg bul bulgarian
hin hindi
th tha thai
id ind indonesian
ms may msa malay
vi vie vietnamese
et est estonian
sl slv slovenian
sk slo slk slovak
lt lit lithuanian
lv lav latvian
mk mac mkd macedonian
sq alb sqi albanian
is ice isl icelandic
ca cat catalan
eu baq eus basque
gl glg galician"""
# alias -> 2-letter code ("hi" is left out: in sidecar names it means hearing impaired)
LANG = {a: r.split()[0] for r in _LANG_ROWS.splitlines() for a in r.split()}
LANG["hin"] = LANG["hindi"] = "hi"
SUB_MARKERS = {"hi", "sdh", "cc", "forced", "full", "default"}
SUB_STREAM = re.compile(r"Stream #\d+:\d+(?:\[0x[0-9a-f]+\])?(?:\((\w+)\))?: Subtitle")


def sidecar_lang(name):
    """Language of a subtitle file from the last two tokens of its name ("x_eng.srt",
    "spa.HI.srt", "2_English.srt", "pt-brazil"), or None. Only known names count, so
    "x264" or "BluRay" never pass for one."""
    toks = re.split(r"[^a-z]+", Path(name).stem.lower())
    toks = [t for t in toks if t and t not in SUB_MARKERS]
    return next((LANG[t] for t in reversed(toks[-2:]) if t in LANG), None)


def probe_langs(out):
    """Subtitle stream languages from `ffprobe FILE` / `ffmpeg -i FILE` output ("und" when
    untagged), or None if the file never opened -- a failed probe must not cache as "no
    subtitles"."""
    if "Input #0" not in out:
        return None
    return [LANG.get(t, t) if t else "und" for t in SUB_STREAM.findall(out)]


PROBE_SH = r"""P=$(command -v ffprobe) || P=$(command -v ffmpeg) || { echo ::NOPROBE::; exit 0; }
case $P in *ffmpeg) A=-i ;; *) A= ;; esac
while IFS= read -r f; do
  echo "::F::$f"; "$P" -hide_banner $A "$f" </dev/null 2>&1 | grep -E 'Input #|Stream #'
done"""


def parse_probe_batch(out):
    """Split PROBE_SH output into {path: probe text}; None if the host has no prober."""
    if "::NOPROBE::" in out:
        return None
    res, cur = {}, None
    for line in out.splitlines():
        if line.startswith("::F::"):
            cur = line[5:]
            res[cur] = ""
        elif cur is not None:
            res[cur] += line + "\n"
    return res


def _probe_local(paths, done):
    for path in paths:
        try:
            r = subprocess.run(["ffprobe", "-hide_banner", path], capture_output=True,
                               text=True, timeout=60)
        except FileNotFoundError:
            print("  [info] ffprobe not installed: local subtitles from sidecars only")
            return
        except subprocess.SubprocessError:
            continue
        done(path, r.stderr)


def _probe_remote(paths, done):
    # ~ doesn't expand inside the quoted "$f": strip it, ssh starts in $HOME anyway
    rel = {p[2:] if p.startswith("~/") else p: p for p in paths}
    todo = list(rel)
    for i in range(0, len(todo), 40):  # a chunk per call: a timeout loses 40, not all
        chunk = todo[i : i + 40]
        print(f"  [subs] probing {i + len(chunk)}/{len(todo)} on {SARONIC_IP}")
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", SARONIC_IP,
                 PROBE_SH],
                input="\n".join(chunk) + "\n", capture_output=True, text=True,
                timeout=600,
            )
        except (subprocess.SubprocessError, OSError) as e:
            print(f"  [info] NAS subtitle probe failed, sidecars only: {e}")
            return
        got = parse_probe_batch(r.stdout)
        if got is None:
            print("  [info] no ffprobe/ffmpeg on the NAS: remote subtitles from sidecars only")
            return
        for f, out in got.items():
            if f in rel:
                done(rel[f], out)


def subtitles(rows, cache):
    """rows: (source, base, raw, is_dir, files). Returns per row None (no subtitles) or the
    sorted languages found, [] when there are subtitles of unknown language. Sidecars are
    re-listed every run; embedded streams are probed once per file, cached in
    cache["_subs"] by path|size|mtime. # ponytail: probes only the largest video, so one
    episode stands in for a whole series; probe every episode if packs turn out mixed."""
    known = cache.setdefault("_subs", {})
    mains, todo = [], {"Local": [], "remote": []}
    for source, base, raw, is_dir, files in rows:
        vids = [f for f in files if Path(f[0]).suffix.lower() in VIDEO_EXT]
        path = key = None
        if vids:
            rel, size, mtime = max(vids, key=lambda f: f[1])
            path = f"{base}/{rel}"
            key = f"{source}|{path}|{size}|{mtime}"
            if key not in known:
                todo["Local" if source == "Local" else "remote"].append((path, key))
        mains.append(key)

    for kind, items in todo.items():
        if not items:
            continue
        keys = dict(items)

        def done(path, out):
            langs = probe_langs(out)
            if langs is not None:  # opened and read: worth keeping
                known[keys[path]] = langs

        (_probe_local if kind == "Local" else _probe_remote)(list(keys), done)

    out = []
    for (source, base, raw, is_dir, files), key in zip(rows, mains):
        subs = [f[0] for f in files if Path(f[0]).suffix.lower() in SUB_EXT]
        stem = "" if is_dir else Path(raw).stem
        langs = {sidecar_lang(Path(f).name[len(stem):] if stem else Path(f).name)
                 for f in subs}
        emb = known.get(key)
        langs |= set(emb or [])
        langs -= {None, "und"}
        out.append(sorted(langs) if subs or emb else None)
    return out


# Bump when the lookup logic gets smarter: negatives cached by an older, dumber search
# would otherwise pin those titles as misses forever. See purge_cache().
SEARCH_V = 3
NOT_FOUND = {"Response": "False", "Error": "Movie not found!", "v": SEARCH_V}


def omdb(title, year, typ, key, cache, refresh, imdb_id=None):
    ckey = imdb_id if imdb_id else f"{title}|{year or ''}|{typ}"
    if not refresh and ckey in cache:
        return cache[ckey]
    if imdb_id:
        data = _get({"apikey": key, "i": imdb_id, "plot": "full"})
    else:
        data = _lookup(title, year, typ, key)
    # Only a real answer is worth keeping. A 401/timeout is transient -- caching it once
    # poisoned 88 entries here, and every later run served the poison instead of retrying.
    if data.get("Response") == "True":
        cache[ckey] = data
    elif not data.get("transient"):
        cache[ckey] = dict(NOT_FOUND)
    return data


def purge_cache(cache):
    """Drop cached non-answers. Returns (transient_dropped, outdated_misses_dropped)."""
    stale = [
        k
        for k, v in cache.items()
        if k != "_subs"  # subtitle probes, not OMDb records
        and v.get("Response") != "True" and v.get("Error") != NOT_FOUND["Error"]
    ]
    old = [
        k
        for k, v in cache.items()
        if v.get("Error") == NOT_FOUND["Error"] and v.get("v", 0) < SEARCH_V
    ]
    for k in stale + old:
        del cache[k]
    return len(stale), len(old)


def _lookup(title, year, typ, key):
    """Exact title, then a ladder of simplified search queries behind `pick`."""
    params = {"apikey": key, "t": title, "plot": "full"}
    if typ == "series":  # only ever asserted, never guessed -- "movie" is the default
        params["type"] = typ
    if year and typ == "movie":
        params["y"] = year
    cands, full = search_candidates(title)
    # `t=` is fuzzy: "Goldfinger" 1964 answers with a set-visit short, and with `y=` it
    # prefers any same-year junk ("It's A Wonderful Life (1946)", no votes). Vet each
    # answer like a search hit, then retry without the year, then fall back to search.
    while True:
        data = _get(params)
        if data.get("transient"):
            return data
        if data.get("Response") == "True" and data.get("imdbVotes") != "N/A":
            ry = _year_of(data.get("Year"))
            yok = not year or (ry.isdigit() and abs(int(ry) - int(year)) <= 1)
            if yok and _sim(full, data.get("Title", "")) >= 0.85:
                return data
        if "y" not in params:
            break
        params.pop("y")

    for c in cands:
        sp = {"apikey": key, "s": c}
        if typ == "series":
            sp["type"] = typ
        res = _get(sp)
        if res.get("transient"):
            return res
        if res.get("Response") != "True":
            continue
        hit = pick(res.get("Search", []), full, c, year, typ)
        if hit:
            full_rec = _get({"apikey": key, "i": hit["imdbID"], "plot": "full"})
            if full_rec.get("Response") == "True":
                return full_rec
            if full_rec.get("transient"):
                return full_rec
    return dict(NOT_FOUND)


LIMITED = []  # non-empty once OMDb says 401 (daily quota spent, or a bad key)


def _get(params):
    if LIMITED:  # every further call would 401 too: finish the run from cache alone
        return {"Response": "False", "Error": LIMITED[0], "transient": True}
    url = "https://www.omdbapi.com/?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.load(r)
    except Exception as e:  # network/auth error -> a miss for this run, never cached
        if getattr(e, "code", None) == 401:
            LIMITED.append(json.loads(e.read() or "{}").get("Error") or str(e))
        return {"Response": "False", "Error": str(e), "transient": True}


WIKIDATA_Q = """SELECT ?imdb ?title ?lang WHERE { VALUES ?imdb { %s }
  ?item wdt:P345 ?imdb . OPTIONAL { ?item wdt:P1476 ?title . }
  OPTIONAL { ?item wdt:P364/wdt:P218 ?lang . } }"""


def _same(a, b):  # unicode-aware, unlike _norm: Greek/Cyrillic titles must not all be ""
    # NFKD splits off accents, which [\W_] then drops: "Fantômas" is just "Fantomas"
    key = lambda s: re.sub(r"[\W_]", "", unicodedata.normalize("NFKD", (s or "").lower()))
    return key(a) == key(b)


def pick_original(title, rows):
    """rows: (title, title_lang, original_lang) from Wikidata. A title only counts in one of
    the film's original languages ("Dark" carries an English "Dark hosen"), and never in
    English: OMDb's title already is, so an English one is a variant ("Kill Bill Volume 2").
    If OMDb's title is one of them it already is original; else take one ("Zorba the
    Greek": Αλέξης Ζορμπάς, though Wikidata lists the film as both en and el)."""
    langs = {o for _, _, o in rows if o}
    ok = sorted({t for t, l, _ in rows if t and l != "en" and (l in langs or not langs)})
    if any(_same(t, title) for t in ok):
        return ""
    return (ok or [""])[0]


def original_titles(records):
    """Set "OrigTitle" on each OMDb record lacking it, in place: one Wikidata query per 200
    IDs, so it rides in cache.json with the record and costs one lookup per title, ever."""
    todo = {}
    for d in records:
        if d.get("Response") == "True" and "OrigTitle" not in d and d.get("imdbID"):
            todo.setdefault(d["imdbID"], []).append(d)
    ids = list(todo)
    for i in range(0, len(ids), 200):
        chunk = ids[i : i + 200]
        q = WIKIDATA_Q % " ".join(f'"{x}"' for x in chunk)
        req = urllib.request.Request(
            "https://query.wikidata.org/sparql",
            data=urllib.parse.urlencode({"query": q, "format": "json"}).encode(),
            headers={"User-Agent": "movies-db/1.0 (personal film catalogue)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                bindings = json.load(r)["results"]["bindings"]
        except Exception as e:  # transient: leave them unset, retried next run
            print(f"  [info] Wikidata original titles skipped: {e}")
            return
        rows = {x: [] for x in chunk}  # an ID Wikidata doesn't know caches as ""
        for b in bindings:
            t = b.get("title", {})
            rows[b["imdb"]["value"]].append(
                (t.get("value"), t.get("xml:lang"), b.get("lang", {}).get("value"))
            )
        for x, rs in rows.items():
            for d in todo[x]:
                d["OrigTitle"] = pick_original(d.get("Title", ""), rs)


def rt_rating(data):
    for r in data.get("Ratings", []):
        if r.get("Source") == "Rotten Tomatoes":
            return r["Value"]
    return ""


def build_entry(
    raw,
    title,
    year,
    typ,
    data,
    source="Local",
    watched=False,
    base="",
    added=None,
    is_dir=False,
    subs=None,
):
    matched = data.get("Response") == "True"
    disp_title = data.get("Title") if matched else title
    disp_year = (data.get("Year") if matched else year) or ""
    # OMDb's Year is often the US release ("Take the Money and Run" 1969 -> 1970). Once
    # matched, a nearby year in the name is the original release: keep it.
    if matched and year and disp_year.isdigit() and abs(int(disp_year) - int(year)) <= 2:
        disp_year = year
    q = urllib.parse.quote_plus(f"{disp_title} {disp_year} trailer".strip())
    poster = data.get("Poster", "")
    span = re.sub(r"[–—]", "-", disp_year)  # OMDb uses an en dash: "2008–2011"
    orig = data.get("OrigTitle", "") if matched else ""
    return {
        "title": disp_title,
        # original-language title (Wikidata), only when it isn't just the displayed one
        "orig": "" if _same(orig, disp_title) else orig,
        "year": re.sub(r"\D.*$", "", disp_year)
        or "",  # "2024–" -> "2024", for sort/merge
        # a series' run: "2008-2013" finished, "2008-" still airing. "" for movies.
        "years": span if "-" in span else "",
        "type": (data.get("Type") if matched else typ) or typ,
        "director": data.get("Director", "") if matched else "",
        "actors": data.get("Actors", "") if matched else "",
        "genre": data.get("Genre", "") if matched else "",
        "imdb": data.get("imdbRating", "") if matched else "",
        "rt": rt_rating(data) if matched else "",
        "imdb_url": f"https://www.imdb.com/title/{data['imdbID']}/"
        if matched and data.get("imdbID")
        else "",
        "plot": (data.get("Plot") or "") if matched else "",
        "runtime": (data.get("Runtime") or "") if matched else "",
        "seasons": (data.get("totalSeasons") or "") if matched else "",
        "poster": poster if poster and poster != "N/A" else "",
        "trailer": f"https://www.youtube.com/results?search_query={q}",
        "matched": matched,
        "raw": raw,
        "sources": [source],
        "paths": [[source, base, raw, is_dir]],  # one per physical copy, for --rename
        "watched": watched,
        "added": added,  # epoch seconds (folder mtime) or None
        "subs": subs,  # None: no subtitles; else languages, [] if none are known
    }


def merge_duplicates(entries):
    seen, order = {}, []
    for e in entries:
        key = (e["title"].lower(), e["year"])
        if key not in seen:
            seen[key] = e
            order.append(key)
        else:
            m = seen[key]
            for s in e["sources"]:
                if s not in m["sources"]:
                    m["sources"].append(s)
            for p in e["paths"]:
                if p not in m["paths"]:
                    m["paths"].append(p)
            if e["watched"]:
                m["watched"] = True
            known = [a for a in (m["added"], e["added"]) if a is not None]
            m["added"] = min(known) if known else None
            if e["subs"] is not None:  # explicit: [] (unknown language) still counts
                m["subs"] = sorted(set(m["subs"] or []) | set(e["subs"]))
    return [seen[k] for k in order]


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Video Library</title>
<style>
  :root{color-scheme:dark}
  body{margin:0;font:15px/1.4 system-ui,sans-serif;background:#15171c;color:#e8e8e8}
  header{padding:16px 24px;background:#1d2027;position:sticky;top:0;z-index:5;
    display:flex;gap:12px;align-items:center;flex-wrap:wrap;border-bottom:1px solid #2c303a}
  header h1{font-size:18px;margin:0 12px 0 0}
  input,select,button{font:inherit;padding:7px 10px;border-radius:8px;border:1px solid #363b47;
    background:#0f1115;color:#e8e8e8}
  button{cursor:pointer}
  .count{margin-left:auto;color:#8b93a3;font-size:13px}
  main{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));
    gap:16px;padding:20px 24px}
  .card{background:#1d2027;border:1px solid #2c303a;border-radius:12px;overflow:hidden;
    display:flex;flex-direction:column}
  .poster{aspect-ratio:2/3;background:#0f1115 center/cover no-repeat;display:flex;
    align-items:center;justify-content:center;color:#555;font-size:13px;text-align:center;padding:8px;
    cursor:zoom-in}
  dialog#zoom{background:#1d2027;color:#e8e8e8;border:1px solid #2c303a;border-radius:12px;
    padding:0;max-width:640px;width:92vw;max-height:88vh}
  dialog#zoom::backdrop{background:rgba(0,0,0,.65)}
  dialog#zoom[open]{display:flex}
  #zoomBody{display:flex;flex-direction:column;max-height:88vh;overflow:auto;position:relative}
  #zoomBody .zposter{width:100%;aspect-ratio:2/3;max-height:46vh;background:#0f1115 center/contain no-repeat;
    display:flex;align-items:center;justify-content:center;color:#555;flex:none}
  #zoomBody .zinfo{padding:16px 20px;display:flex;flex-direction:column;gap:8px}
  #zoomClose{position:absolute;top:8px;right:8px;font-size:18px;width:30px;height:30px;
    border-radius:50%;background:#0f1115;border:1px solid #363b47;color:#e8e8e8;cursor:pointer}
  .body{padding:12px 14px;display:flex;flex-direction:column;gap:6px;flex:1}
  .t{font-weight:600;line-height:1.25}
  .meta{font-size:12px;color:#9aa3b2}
  .badges{display:flex;gap:6px;flex-wrap:wrap;font-size:12px}
  .b{padding:2px 7px;border-radius:6px;background:#0f1115;border:1px solid #363b47}
  .imdb{color:#f5c518;border-color:#5a4d12}
  .rt{color:#fa320a;border-color:#5a2012}
  .series{color:#7cc4ff;border-color:#1c4a6e}
  .nomatch{color:#d98b3a;border-color:#6e4a1c}
  .loc{color:#8be0a4;border-color:#1c6e3a}
  .watched-badge{color:#6ee7b7;border-color:#065f46}
  .subs{color:#4ade80;border-color:#166534}
  .card.watched{opacity:.5;filter:grayscale(40%)}
  .card.watched:hover{opacity:.85;filter:none}
  a.trailer{margin-top:auto;text-align:center;padding:8px;border-radius:8px;
    background:#b91c1c;color:#fff;text-decoration:none;font-size:13px}
  a.trailer:hover{background:#dc2626}
  .sep{width:1px;height:24px;background:#2c303a;align-self:center}
  label.cb{display:flex;align-items:center;gap:5px;font-size:13px;cursor:pointer;white-space:nowrap}
  label.cb input{accent-color:#6ee7b7;width:15px;height:15px;cursor:pointer}
  .edit-row{display:flex;gap:6px;align-items:center;margin-top:4px}
  .edit-field{font:inherit;font-size:13px;padding:3px 6px;border-radius:6px;
    border:1px solid #d98b3a;background:#0f1115;color:#e8e8e8;min-width:0;flex:1}
  .edit-year{flex:0 0 54px}
  .edit-box{display:none;flex-direction:column;gap:0}
  .card.editing .edit-box{display:flex}
  .card.dirty{outline:1px solid #d98b3a}
  .pen{background:none;border:0;padding:0 4px;color:#8b93a3;cursor:pointer;font-size:13px}
  .pen:hover{color:#d98b3a}
  .t-row{display:flex;align-items:flex-start;gap:4px}
  .t-row .t{flex:1}
  #fixes{color:#d98b3a;font-size:13px}
  details.plot{font-size:12px;color:#9aa3b2;margin-top:2px}
  details.plot summary{cursor:pointer;color:#7cc4ff;list-style:none;user-select:none}
  details.plot summary::marker{display:none}
  details.plot summary::before{content:'▶ Plot'}
  details[open].plot summary::before{content:'▼ Plot'}
  details.plot p{margin:6px 0 0;line-height:1.5}
  #genres{position:relative}
  #genres summary{cursor:pointer;font-size:13px}
  #genreList{position:absolute;top:100%;left:0;z-index:10;background:#1d2027;border:1px solid #2c303a;
    border-radius:8px;padding:8px 12px;display:grid;grid-template-columns:repeat(2,max-content);
    gap:6px 16px;max-height:60vh;overflow:auto}
</style></head><body>
<header>
  <h1>🎬 My Video Library</h1>
  <input id="q" placeholder="Filter by name, actor, director…" autocomplete="off">
  <label class="cb"><input type="checkbox" id="cMovies" checked> Movies</label>
  <label class="cb"><input type="checkbox" id="cSeries" checked> Series</label>
  <div class="sep"></div>
  <label class="cb"><input type="checkbox" id="cLocal" checked> Local</label>
  <label class="cb"><input type="checkbox" id="cSaronic" checked> Saronic</label>
  <label class="cb"><input type="checkbox" id="cWD" checked> WD</label>
  <div class="sep"></div>
  <label class="cb"><input type="checkbox" id="cWatched"> Watched</label>
  <div class="sep"></div>
  <details id="genres"><summary>Genres</summary><div id="genreList"></div></details>
  <div class="sep"></div>
  <select id="sort">
    <option value="title">Name</option><option value="year">Year</option>
    <option value="imdb">IMDb rating</option><option value="rt">RT rating</option>
    <option value="added">Recently added</option>
  </select>
  <button id="dir" title="Toggle direction">▲ Asc</button>
  <div class="sep"></div>
  <span id="fixes"></span>
  <button id="dl" title="Save corrections.json, then re-run build.py">⬇ Download fixes</button>
  <button id="cp" title="Copy corrections JSON to clipboard">⧉ Copy</button>
  <span class="count" id="count"></span>
</header>
<main id="grid"></main>
<dialog id="zoom"><div id="zoomBody"></div></dialog>
<script>
const DATA = __DATA__;
const SAVED = __CORRECTIONS__;   // what corrections.json already holds, from the build
const grid=document.getElementById('grid'), q=document.getElementById('q'),
  sortSel=document.getElementById('sort'),
  dirBtn=document.getElementById('dir'), count=document.getElementById('count'),
  fixes=document.getElementById('fixes');
const cMovies=document.getElementById('cMovies'), cSeries=document.getElementById('cSeries'),
  cLocal=document.getElementById('cLocal'), cSaronic=document.getElementById('cSaronic'),
  cWD=document.getElementById('cWD'), cWatched=document.getElementById('cWatched');
const FIELDS=['title','year','imdb_id'];
// every raw name still present on disk -- a card owns one per physical copy
const LIVE=new Set(DATA.flatMap(m=>m.paths.map(p=>p[2])));
// the build extracts tt… from a pasted URL, so compare on that, not the raw text
const val=(o,f)=>{const v=((o||{})[f]||'').trim();
  return f==='imdb_id'?(v.match(/tt[0-9]{7,}/)||[''])[0]:v;};
const same=(a,b)=>FIELDS.every(f=>val(a,f)===val(b,f));

// Draft buffer only. Reconcile on load or it goes stale the moment a fix triggers a
// rename: a key that no longer exists on disk, or one the build already ingested, would
// otherwise sit in the pending count forever and get re-injected on every download.
const drafts={};
try{
  const buf=JSON.parse(localStorage.getItem('mov_corrections')||'{}');
  for(const[k,v]of Object.entries(buf))
    if(LIVE.has(k)&&!same(v,SAVED[k])) drafts[k]=v;
}catch{}
function _save(){
  localStorage.setItem('mov_corrections',JSON.stringify(drafts));
  const n=Object.keys(drafts).length;
  fixes.textContent=n?n+' pending fix'+(n>1?'es':''):'';
}
// A merged card covers several files; the correction has to land on all of their names.
grid.addEventListener('input',e=>{
  if(!e.target.classList.contains('edit-field'))return;
  const card=e.target.closest('.card'), raws=JSON.parse(card.dataset.raws);
  const vals={};
  card.querySelectorAll('.edit-field').forEach(f=>vals[f.dataset.field]=f.value.trim());
  for(const r of raws){
    if(FIELDS.some(f=>vals[f])) drafts[r]={...vals}; else delete drafts[r];
  }
  card.classList.toggle('dirty',raws.some(r=>r in drafts));
  _save();
});
grid.addEventListener('click',e=>{
  if(e.target.classList.contains('pen')){e.target.closest('.card').classList.toggle('editing');return;}
  if(e.target.closest('.poster')) openZoom(CURRENT_ROWS[+e.target.closest('.card').dataset.i]);
});
function payload(){
  const out={...SAVED};
  for(const[k,v]of Object.entries(drafts)) out[k]=v;
  return JSON.stringify(out,null,2);
}
document.getElementById('dl').onclick=()=>{
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([payload()],{type:'application/json'}));
  a.download='corrections.json'; a.click(); URL.revokeObjectURL(a.href);
};
document.getElementById('cp').onclick=async e=>{
  try{await navigator.clipboard.writeText(payload());e.target.textContent='✓ Copied';}
  catch{e.target.textContent='✗ blocked';}
  setTimeout(()=>e.target.textContent='⧉ Copy',1500);
};
const zoom=document.getElementById('zoom'), zoomBody=document.getElementById('zoomBody');
let CURRENT_ROWS=[];
function openZoom(m){
  const yr=m.years||m.year;
  zoomBody.innerHTML=`
    <button id="zoomClose" title="Close">✕</button>
    <div class="zposter" style="${m.poster?`background-image:url('${esc(m.poster)}')`:''}">${m.poster?'':esc(m.title)}</div>
    <div class="zinfo">
      <div class="t" style="font-size:18px">${esc(m.title)} ${yr?`<span class="meta">(${esc(yr)})</span>`:''}</div>
      ${m.orig?`<div class="meta"><i>${esc(m.orig)}</i></div>`:''}
      ${m.genre?`<div class="meta">${esc(m.genre)}</div>`:''}
      ${m.runtime&&m.runtime!=='N/A'?`<div class="meta">⏱ ${esc(m.runtime)}</div>`:''}
      ${m.seasons?`<div class="meta">📺 ${esc(m.seasons)} season${m.seasons!=='1'?'s':''}</div>`:''}
      ${m.added!=null?`<div class="meta">Added in ${new Date(m.added*1000).getFullYear()}</div>`:''}
      ${m.director?`<div class="meta">Dir: ${esc(m.director)}</div>`:''}
      ${m.actors?`<div class="meta">${esc(m.actors)}</div>`:''}
      ${m.plot&&m.plot!=='N/A'?`<p style="line-height:1.5">${esc(m.plot)}</p>`:'<p class="meta">No plot available.</p>'}
      <div style="display:flex;gap:6px">
        ${m.imdb_url?`<a class="trailer" style="flex:0 0 auto;background:#f5c518;color:#000" href="${esc(m.imdb_url)}" target="_blank" rel="noopener">IMDb</a>`:''}
        <a class="trailer" style="flex:1" href="${esc(m.trailer)}" target="_blank" rel="noopener">▶ Trailer</a>
      </div>
    </div>`;
  zoomBody.querySelector('#zoomClose').onclick=()=>zoom.close();
  zoom.showModal();
}
zoom.addEventListener('click',e=>{if(e.target===zoom)zoom.close();});  // backdrop click
let asc=true;
const genreList=document.getElementById('genreList'), genreSum=document.querySelector('#genres summary');
const gtok=m=>(m.genre||'').split(',').map(t=>t.trim()).filter(t=>t&&t!=='N/A');
[...new Set(DATA.flatMap(gtok))].sort().forEach(t=>{
  const l=document.createElement('label');l.className='cb';
  l.innerHTML='<input type="checkbox" value="'+esc(t)+'"> '+esc(t);
  genreList.appendChild(l);
});
const num=v=>{const n=parseFloat(v);return isNaN(n)?-1:n};
const fold=s=>s.normalize('NFD').replace(/\\p{M}/gu,'').toLowerCase();  // "zorbas" finds Ζορμπάς
function render(){
  const term=fold(q.value), key=sortSel.value;
  const srcs=new Set([cLocal.checked&&'Local',cSaronic.checked&&'Saronic',cWD.checked&&'WD'].filter(Boolean));
  const gsel=new Set([...genreList.querySelectorAll('input:checked')].map(i=>i.value));
  genreSum.textContent=gsel.size?'Genres ('+gsel.size+')':'Genres';
  let rows=DATA.filter(m=>fold(m.title+' '+m.orig+' '+m.director+' '+m.actors).includes(term)
    &&(m.type==='series'?cSeries.checked:cMovies.checked)
    &&m.sources.some(s=>srcs.has(s))
    &&m.watched===cWatched.checked
    &&(!gsel.size||gtok(m).some(t=>gsel.has(t))));
  rows.sort((a,b)=>{
    let x,y;
    if(key==='title'){x=a.title.toLowerCase();y=b.title.toLowerCase();return x<y?-1:x>y?1:0;}
    if(key==='year'){x=num(a.year);y=num(b.year);}
    else{x=num(a[key]);y=num(b[key]);}
    return x-y;
  });
  if(!asc) rows.reverse();
  count.textContent=rows.length+' titles';
  CURRENT_ROWS=rows;
  grid.innerHTML=rows.map((m,i)=>card(m,i)).join('');
}
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function card(m,i){
  const poster=m.poster?`style="background-image:url('${esc(m.poster)}')"`:'';
  const badges=[
    m.imdb?`<span class="b imdb">IMDb ${esc(m.imdb)}</span>`:'',
    m.rt?`<span class="b rt">🍅 ${esc(m.rt)}</span>`:'',
    m.type==='series'?`<span class="b series">Series</span>`:'',
    !m.matched?`<span class="b nomatch" title="${esc(m.raw)}">no match</span>`:'',
    ...m.sources.map(s=>`<span class="b loc">📍 ${esc(s)}</span>`),
    m.watched?`<span class="b watched-badge">✓ Watched</span>`:'',
    m.subs?`<span class="b subs" title="${esc(m.subs.length?'Subtitles: '+m.subs.join(', '):'Subtitles (language unknown)')}">✓ Subs</span>`:'',
  ].join('');
  const yr=m.years||m.year;
  const raws=m.paths.map(p=>p[2]);
  const cur=drafts[m.raw]||SAVED[m.raw]||{};
  const dirty=raws.some(r=>r in drafts);
  // every card is editable: a wrong match looks matched, so it needs the same escape hatch
  const titleHtml=`<div class="t-row">
      <div class="t">${esc(m.title)} ${yr?`<span class="meta">(${esc(yr)})</span>`:''}</div>
      <button class="pen" title="Fix this entry">✎</button>
    </div>
    ${m.orig?`<div class="meta"><i>${esc(m.orig)}</i></div>`:''}
    <div class="edit-box">
      <div class="edit-row">
        <input class="edit-field" data-field="title" value="${esc(cur.title??(m.matched?'':m.title))}" placeholder="title">
        <input class="edit-field edit-year" data-field="year" value="${esc(cur.year??(m.matched?'':m.year))}" placeholder="year">
      </div>
      <div class="edit-row">
        <input class="edit-field" data-field="imdb_id" value="${esc(cur.imdb_id||'')}" placeholder="IMDb URL or tt… (wins over title)">
      </div>
      <div class="meta" style="margin-top:4px">${esc(m.raw)}</div>
    </div>`;
  return `<div class="card${m.watched?' watched':''}${!m.matched?' editing':''}${dirty?' dirty':''}"
      data-raws='${esc(JSON.stringify(raws))}' data-i="${i}">
    <div class="poster" ${poster}>${m.poster?'':esc(m.title)}</div>
    <div class="body">
      ${titleHtml}
      <div class="badges">${badges}</div>
      ${m.genre?`<div class="meta">${esc(m.genre)}</div>`:''}
      ${m.runtime&&m.runtime!=='N/A'?`<div class="meta">⏱ ${esc(m.runtime)}</div>`:''}
      ${m.seasons?`<div class="meta">📺 ${esc(m.seasons)} season${m.seasons!=='1'?'s':''}</div>`:''}
      ${m.added!=null?`<div class="meta">Added in ${new Date(m.added*1000).getFullYear()}</div>`:''}
      ${m.director?`<div class="meta">Dir: ${esc(m.director)}</div>`:''}
      ${m.actors?`<div class="meta">${esc(m.actors)}</div>`:''}
      ${m.plot&&m.plot!=='N/A'?`<details class="plot"><summary></summary><p>${esc(m.plot)}</p></details>`:''}
      <div style="display:flex;gap:6px;margin-top:auto">
        ${m.imdb_url?`<a class="trailer" style="flex:0 0 auto;background:#f5c518;color:#000" href="${esc(m.imdb_url)}" target="_blank" rel="noopener">IMDb</a>`:''}
        <a class="trailer" style="flex:1" href="${esc(m.trailer)}" target="_blank" rel="noopener">▶ Trailer</a>
      </div>
    </div></div>`;
}
function setDir(a){asc=a;dirBtn.textContent=asc?'▲ Asc':'▼ Desc';}
// derive direction from the sort key: Asc for name, Desc (highest/newest first) for the rest;
// also on load, since a reload restores the dropdown without firing input
const dirFromKey=()=>setDir(sortSel.value==='title');
dirBtn.onclick=()=>{setDir(!asc);render();};
q.addEventListener('input',render);
sortSel.addEventListener('input',()=>{dirFromKey();render();});
genreList.addEventListener('change',render);
[cMovies,cSeries,cLocal,cSaronic,cWD,cWatched].forEach(el=>el.addEventListener('change',render));
_save();
dirFromKey();
render();
</script></body></html>
"""


def generate(entries, corrections):
    html = HTML.replace("__DATA__", json.dumps(entries, ensure_ascii=False))
    html = html.replace("__CORRECTIONS__", json.dumps(corrections, ensure_ascii=False))
    OUT.write_text(html, encoding="utf-8")


IMDB_ID = re.compile(r"tt\d{7,}")


def norm_correction(c):
    """Sanitise one user-entered correction; drop it if it says nothing."""
    out = {}
    for f in ("title", "year"):
        v = (c.get(f) or "").strip()
        if v:
            out[f] = v
    m = IMDB_ID.search(c.get("imdb_id") or "")  # accepts a pasted IMDb URL
    if m:
        out["imdb_id"] = m.group(0)
    if c.get("ignore"):
        out["ignore"] = True
    return out


def load_corrections():
    """Read corrections.json, drop entries that only restate what the parser already says.

    Those stubs used to be auto-seeded, and because main() prefers a correction over the
    parsed title they silently pinned every entry to the old parse -- no parser fix could
    ever take effect. Only real user intent survives.
    """
    if not CORRECTIONS_FILE.exists():
        return {}
    raw = json.loads(CORRECTIONS_FILE.read_text())
    kept, dropped = {}, 0
    for name, c in raw.items():
        c = norm_correction(c)
        if not c:
            dropped += 1
            continue
        if set(c) <= {"title", "year"}:
            t, y, _ = parse_name(name)
            # A stub restates the parse, but a rename may have changed the separators
            # since it was written ("A-Hitchcock-Vertigo" vs "A Hitchcock Vertigo"), and
            # a parser fix may since have learned to strip what it kept ("...Season1+2").
            # Run the stub's own title back through the parser and compare normalised.
            ct = _norm(parse_name(c.get("title", ""))[0])
            if ct in (_norm(t), "") and c.get("year", "") in ("", y or ""):
                dropped += 1  # says nothing the parser can't already work out
                continue
        kept[name] = c
    if dropped:
        print(f"  [info] pruned {dropped} no-op correction(s)")
    return kept


def pickup_corrections(corrections):
    """Merge a corrections.json downloaded from movies.html. Returns the merged keys."""
    if not PICKUP.exists():
        return set()
    if CORRECTIONS_FILE.exists() and PICKUP.stat().st_mtime <= (
        CORRECTIONS_FILE.stat().st_mtime
    ):
        return set()
    try:
        incoming = json.loads(PICKUP.read_text())
    except (OSError, ValueError) as e:
        print(f"  [WARN] ignoring {PICKUP}: {e}", file=sys.stderr)
        return set()
    merged = set()
    for name, c in incoming.items():
        c = norm_correction(c)
        if c and corrections.get(name) != c:
            corrections[name] = c
            merged.add(name)
    PICKUP.rename(PICKUP.with_suffix(".json.imported"))
    print(f"  [info] picked up {len(merged)} fix(es) from {PICKUP}")
    return merged


def rq(p):
    """Quote a remote path, leaving a leading ~/ unquoted so the shell expands it."""
    return "~/" + shlex.quote(p[2:]) if p.startswith("~/") else shlex.quote(p)


def rename_entries(entries, corrections, force=False):
    def safe(s):
        s = SERIES.sub("", s)  # "series"/"season" is a tag, never part of the name
        s = re.sub(r'[/\\:*?"<>|]', "", s).strip(" -–_[({")
        return re.sub(r"\s+", "-", s)  # "The Holiday" -> "The-Holiday"

    planned, seen_per_dir = [], {}
    for e in entries:
        if not e["matched"]:
            # An unmatched title renames to bare "Monty-Pythons-The-meaning-of-life":
            # no _(year) suffix, so OURS can't read it back and the spaces are gone for
            # good. Leave it alone until a lookup or a correction resolves it.
            continue
        title = safe(e["title"] or e["raw"])
        # a finished series gets its full run "(2008-2011)", else the single year
        span = e.get("years") or e["year"]
        stem = (
            f"{title}_({span})"
            if span
            else (f"{title}_(series)" if e["type"] == "series" else title)
        )

        for source, base, raw, is_dir in e["paths"]:
            # a folder named "X.mkv" has no extension to keep
            ext = "" if is_dir else Path(raw).suffix
            new_name = stem + (
                ext if ext.lower() in VIDEO_EXT else ""
            )  # keep file extension
            seen = seen_per_dir.setdefault(base, set())
            if raw == new_name:
                continue
            if new_name in seen:
                print(
                    f"  [WARN] collision: {new_name!r} in {base}, skipping {raw!r}",
                    file=sys.stderr,
                )
                continue
            if source == "Local":
                src = Path(base) / raw
                if not src.exists():
                    print(f"  [SKIP] not found: {src}", file=sys.stderr)
                    continue
                dst = Path(base) / new_name
                if dst.exists():
                    print(f"  [WARN] dst exists: {dst}, skipping", file=sys.stderr)
                    continue
                planned.append(("local", src, dst, raw, new_name))
            else:
                if not SARONIC_IP:
                    print(
                        f"  [SKIP] SARONIC_IP not set, skipping {source}",
                        file=sys.stderr,
                    )
                    continue
                planned.append(
                    ("remote", f"{base}/{raw}", f"{base}/{new_name}", raw, new_name)
                )
            seen.add(new_name)

    if not planned:
        print("Nothing to rename.")
        return planned

    for kind, old, new, _, new_name in planned:
        print(
            f"  {'[REN]' if force else '[DRY]'} [{kind}] {str(old):70s} -> {new_name!r}"
        )

    if not force:
        print(
            f"\n{len(planned)} rename(s) planned. Re-run without --dry-run to apply."
        )
        return planned

    renamed, done = set(), 0
    for kind, old, new, old_raw, new_name in planned:
        try:
            if kind == "local":
                old.rename(new)
            else:
                assert SARONIC_IP
                subprocess.run(
                    [
                        "ssh",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=10",
                        SARONIC_IP,
                        f"mv -n {rq(old)} {rq(new)}",
                    ],
                    check=True,
                    timeout=30,
                    capture_output=True,
                )
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"  [ERR] {old} -> {new}: {exc}", file=sys.stderr)
            continue
        done += 1
        # Saved per rename, not at the end: a run cut short (Ctrl-C, dropped ssh) has
        # already moved files, and a stale log/corrections would orphan them.
        # The log is an undo trail: a mistyped IMDb id renames real files, and the old
        # name is the only way back. Append-only, never read by the program.
        with (HERE / "renames.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%F %T}\t{kind}\t{old}\t{new}\n")
        if old_raw not in renamed:
            renamed.add(old_raw)
            if old_raw in corrections:  # keep the fix pointing at the new filename
                corrections[new_name] = corrections.pop(old_raw)
                CORRECTIONS_FILE.write_text(
                    json.dumps(corrections, ensure_ascii=False, indent=2)
                )

    print(f"\n{done} of {len(planned)} rename(s) applied, logged to {HERE / 'renames.log'}.")
    return planned


def _search_selftest():
    """Ladder + guard, on the real OMDb payloads that motivated them. No network."""
    for title, want in [
        ("James Bond Thunderball", "Thunderball"),
        ("Der Untergang (Downfall)", "Downfall"),
        ("Its A Wonderful Life", "A Wonderful Life"),
        ("A.Hitchcock-Vertigo", "Vertigo"),
        ("The-Lord-of-the-Rings-The-Two-Towers-THEATRICAL-EDITION",
         "The Lord of the Rings The Two Towers"),
    ]:
        cands, _ = search_candidates(title)
        assert want in cands, (title, cands)

    def r(t, y, ty="movie"):
        return {"Title": t, "Year": y, "Type": ty, "imdbID": "tt0"}

    # accept: the title agrees, or a reduced query agrees and the year vouches for it
    ok = [
        ("James Bond Thunderball", "Thunderball", "1965", [r("Thunderball", "1965")]),
        ("Its A Wonderful Life", "A Wonderful Life", "1946",
         [r("It's a Wonderful Life", "1946")]),
        ("Monty Pythons Life of Brian", "Life of Brian", "",
         [r("Monty Python's Life of Brian", "1979")]),
        ("Der Untergang Downfall", "Downfall", "2004", [r("Downfall", "2004")]),
    ]
    for full, cand, year, res in ok:
        assert pick(res, full, cand, year, "movie"), (full, res)

    # reject: every wrong match the ungated version asserted with confidence
    bad = [
        ("Tutti pazzi a tel aviv", "tel aviv", "2019",
         [r("Portrait of a Lady on Fire", "2020")], "movie"),
        ("Monty Pythons The meaning of life", "life", "", [r("Life", "2017")], "movie"),
        ("Perfetti Sconosciuti", "Sconosciuti", "2016",
         [r("I compagni sconosciuti", "2016")], "movie"),
        ("The Lord of the Rings The Return of the King", "The Return of the King",
         "2003",
         [r("The Lord of the Rings: The Return of the King - Special Extended "
            "Edition Scenes", "2004")], "movie"),
        ("Severance", "Severance", "", [r("Severance", "2006", "series")], "movie"),
        # no year to cross-check, and it is a different show
        ("La Casa de Papel", "La Casa de", "",
         [r("La Casa De Papa", "2020-2021", "series")], "series"),
    ]
    for full, cand, year, res, typ in bad:
        assert not pick(res, full, cand, year, typ), (full, res)

    # a correction that only restates the parse is a no-op; real intent survives
    assert norm_correction({"title": "", "year": " ", "imdb_id": ""}) == {}
    assert norm_correction(
        {"imdb_id": "https://www.imdb.com/title/tt0118736/"}
    ) == {"imdb_id": "tt0118736"}
    assert norm_correction({"title": "Vertigo", "ignore": True}) == {
        "title": "Vertigo",
        "ignore": True,
    }


def selftest():
    global SARONIC_IP
    SARONIC_IP = SARONIC_IP or "nas.invalid"  # dry run only, never dialled
    cases = [
        ("Anora 2024 1080p WEB-DL HEVC x265 5.1 BONE", ("Anora", "2024", "movie")),
        (
            "Rental Family 2025 1080p WEB-DL HEVC x265 5.1 BONE.mkv",
            ("Rental Family", "2025", "movie"),
        ),
        ("Pluribus - Season 1", ("Pluribus", None, "series")),
        ("The.Holiday.2006.1080p.BluRay.x265-RARBG", ("The Holiday", "2006", "movie")),
        (
            "www.Torrenting.com   -   Im Still Here A K A Ainda Estou Aqui 2024 PORTUGUESE",
            ("Im Still Here A K A Ainda Estou Aqui", "2024", "movie"),
        ),
        ("Die Hard (1988) [1080p] {5.1}", ("Die Hard", "1988", "movie")),
        # our own --rename output must round-trip, so a second run is a no-op
        ("The-Holiday_(2006).mkv", ("The Holiday", "2006", "movie")),
        ("Pluribus_(series)", ("Pluribus", None, "series")),
        ("Pluribus [series]", ("Pluribus", None, "series")),  # legacy rename pattern
        # "Season1" has no word boundary before the digit -- used to parse as a movie
        ("Big.Little.Lies_Season1+2", ("Big Little Lies", None, "series")),
        ("Severance-Season1+2", ("Severance", None, "series")),
        # ...but a title that merely ends in "Seasons" must survive intact
        ("A Man for All Seasons (1966)", ("A Man for All Seasons", "1966", "movie")),
        # a legacy rename form still on disk
        ("Big-Little-Lies_(series_2017)", ("Big Little Lies", "2017", "series")),
    ]
    for raw, expect in cases:
        got = parse_name(raw)
        assert got == expect, f"\n  {raw!r}\n  got    {got}\n  expect {expect}"

    assert rq("~/Videos/a b") == "~/'Videos/a b'", rq("~/Videos/a b")
    assert rq("/volumeUSB1/a b") == "'/volumeUSB1/a b'", rq("/volumeUSB1/a b")
    _search_selftest()

    # dry run: same title on two NASes, one a file (keeps .mkv), one a folder
    e = build_entry(
        "The.Holiday.2006.1080p.mkv",
        "The Holiday",
        "2006",
        "movie",
        {"Response": "True", "Title": "The Holiday", "Year": "2006", "Type": "movie"},
        "Saronic",
        False,
        "~/Videos/movies",
    )
    e["paths"].append(["WD", "/volumeUSB1/usbshare/Videos", "The Holiday (2006)", True])
    plan = [(old, new) for _, old, new, _, _ in rename_entries([e], {}, force=False)]
    assert plan == [
        (
            "~/Videos/movies/The.Holiday.2006.1080p.mkv",
            "~/Videos/movies/The-Holiday_(2006).mkv",
        ),
        (
            "/volumeUSB1/usbshare/Videos/The Holiday (2006)",
            "/volumeUSB1/usbshare/Videos/The-Holiday_(2006)",
        ),
    ], plan
    # ...and a folder that happens to be named "X.mkv" drops the fake extension
    e["paths"] = [["WD", "/v", "The.Holiday.2006.mkv", True]]
    plan = [new for _, _, _, _, new in rename_entries([e], {}, force=False)]
    assert plan == ["The-Holiday_(2006)"], plan

    # finished series -> full run; still-airing -> first year; no year -> (series)
    def _series(raw, title, year, matched=True):
        data = (
            {"Response": "True", "Title": title, "Year": year or "", "Type": "series"}
            if matched
            else {"Response": "False"}
        )
        return build_entry(
            raw, title, None, "series", data, "Saronic", False, "~/Videos/series"
        )

    done = _series("Breaking.Bad.Complete.Series", "Breaking Bad", "2008–2013")
    airing = _series("Severance.S02", "Severance", "2022–")
    noyear = _series("Pluribus - Season 1", "Pluribus", None)
    # a stale correction title carrying the marker must not leak into the filename
    dirty = _series("Pluribus-[series]_(series)", "Pluribus [series]", None)
    assert [n for _, _, n, _, _ in rename_entries([dirty], {}, force=False)] == [
        "~/Videos/series/Pluribus_(series)"
    ]
    assert (done["years"], done["year"]) == ("2008-2013", "2008")
    assert (airing["years"], airing["year"]) == ("2022-", "2022")
    plan = [
        new
        for _, _, new, _, _ in rename_entries([done, airing, noyear], {}, force=False)
    ]
    assert plan == [
        "~/Videos/series/Breaking-Bad_(2008-2013)",
        "~/Videos/series/Severance_(2022-)",
        "~/Videos/series/Pluribus_(series)",
    ], plan
    # an unmatched entry must never be renamed: "A-B-C" with no _(year) can't round-trip
    unmatched = _series("Monty-Pythons-The-meaning-of-life", "Monty", None, False)
    assert rename_entries([unmatched], {}, force=False) == []
    # our own output must round-trip: dashes back to spaces, so OMDb sees a real title
    for n, want in [
        ("Breaking-Bad_(2008-2013)", ("Breaking Bad", "2008", "series")),
        ("Severance_(2022-)", ("Severance", "2022", "series")),
        ("Pluribus_(series)", ("Pluribus", None, "series")),
        ("La-grande-bellezza_(2013)", ("La grande bellezza", "2013", "movie")),
        ("Mulholland-Dr._(1999).mkv", ("Mulholland Dr.", "1999", "movie")),
    ]:
        assert parse_name(n) == want, (n, parse_name(n))
    # remote listing: names + optional mtimes, failing soft to None
    assert parse_remote_listing("a/\nb.mkv\n::STAT::\n100|a\n200|b.mkv\n") == [
        ("a", True, 100, []),
        ("b.mkv", False, 200, []),
    ]
    assert parse_remote_listing("a/\n") == [("a", True, None, [])]
    assert parse_remote_listing("a/\n::STAT::\nnot-a-line\nx|a\n") == [
        ("a", True, None, [])
    ]
    assert parse_remote_listing("x|y/\n::STAT::\n5|x|y\n") == [("x|y", True, 5, [])]

    assert parse_remote_listing("a/\n::STAT::\n²|a\n") == [("a", True, None, [])]
    # ::FILES:: hands each entry its own videos/subtitles; a bare video gets its siblings
    lst = parse_remote_listing(
        "A/\nM.mkv\n::STAT::\n::FILES::\n9|1.5|A/a.mkv\n1|2.0|A/Subs/eng.srt\n"
        "8|3|M.mkv\n1|4|M.el.srt\n1|4|Other.srt\nbroken\n"
    )
    assert lst == [
        ("A", True, None, [("A/a.mkv", 9, 1), ("A/Subs/eng.srt", 1, 2)]),
        ("M.mkv", False, None, [("M.mkv", 8, 3), ("M.el.srt", 1, 4)]),
    ], lst

    # a dangling symlink must give mtime None, not crash the scan
    with tempfile.TemporaryDirectory() as td:
        link = Path(td) / "dangling"
        link.symlink_to(Path(td) / "missing")
        assert _local_mtime(link) is None
        assert isinstance(_local_mtime(Path(td)), int)

    # year: the name's original-release year beats OMDb's drifted US one, within 2
    def _y(name_year, omdb_year):
        return build_entry("r", "T", name_year, "movie",
                           {"Response": "True", "Year": omdb_year})["year"]
    assert _y("1969", "1970") == "1969"
    assert _y("1994", "1996") == "1994"
    assert _y("1990", "2005") == "2005"  # far off: the name is wrong, not OMDb
    assert _y(None, "1970") == "1970"

    # added: carried by build_entry, oldest non-null wins when merging copies
    miss = {"Response": "False"}
    assert build_entry("r", "T", "2000", "movie", miss)["added"] is None
    assert build_entry("r", "T", "2000", "movie", miss, added=123)["added"] == 123

    def _merged(a, b):
        es = [
            build_entry("r", "T", "2000", "movie", miss, added=x) for x in (a, b)
        ]
        return merge_duplicates(es)[0]["added"]

    assert _merged(300, 100) == 100
    assert _merged(None, 100) == 100
    assert _merged(100, None) == 100
    assert _merged(None, None) is None

    # subtitles: sidecar names, probe output (NAS ffmpeg and newer ffprobe), and caching
    assert sidecar_lang("City.of.God.2002.1080p.BluRay.x264.anoXmous_eng.srt") == "en"
    assert sidecar_lang("spa.HI.srt") == "es" and sidecar_lang("2_English.srt") == "en"
    assert sidecar_lang("Don Juan (1948).portuguese-brazil.srt") == "pt"
    assert sidecar_lang("Die.Hard.BluRay.1080p.x264.5.1.Judas.srt") is None
    assert sidecar_lang("The.Three.Faces.of.Eve.1957..srt") is None
    assert sidecar_lang(".el.srt") == "el"  # a bare video's sibling, stem cut off
    assert sidecar_lang("Before Night Falls.2000.DVDRip-gr.srt") == "el"
    crimson = """Input #0, matroska,webm, from 'x.mkv':
    Stream #0:1(spa): Audio: ac3, 48000 Hz, stereo, 192 kb/s (default)
    Stream #0:3(spa): Subtitle: subrip
    Stream #0:4(eng): Subtitle: subrip
    Stream #0:5(fre): Subtitle: subrip (forced)
  Stream #0:2[0x3](gre): Subtitle: mov_text (default)
  Stream #0:6: Subtitle: hdmv_pgs_subtitle
  Stream #0:7(xyz): Subtitle: subrip"""
    assert probe_langs(crimson) == ["es", "en", "fr", "el", "und", "xyz"]
    assert probe_langs("Input #0, avi\n  Stream #0:0: Video: mpeg4") == []
    assert probe_langs("x.mkv: Input/output error") is None  # never cached as "none"
    assert parse_probe_batch("::NOPROBE::\n") is None
    assert parse_probe_batch("::F::a b.mkv\nInput #0\n::F::c\n") == {
        "a b.mkv": "Input #0\n", "c": ""}
    calls = []
    saved_probe = _probe_local

    def _fake(paths, done):
        calls.extend(paths)
        for path in paths:
            done(path, {"/b/A/a.mkv": "Input #0\n Stream #0:2(eng): Subtitle: x",
                        "/b/B.mkv": "Input #0\n", "/b/D/d.mkv": "Input #0\n"}.get(
                            path, "broken"))

    globals()["_probe_local"] = _fake
    c = {}
    rows = [("Local", "/b", "A", True, [("A/a.mkv", 9, 1), ("A/s.mkv", 1, 1)]),
            ("Local", "/b", "B.mkv", False, [("B.mkv", 5, 1), ("B.Greek.srt", 1, 1)]),
            ("Local", "/b", "C.mkv", False, [("C.mkv", 5, 1)]),
            ("Local", "/b", "D", True, [("D/d.mkv", 5, 1), ("D/Subs/x.srt", 1, 1)])]
    assert subtitles(rows, c) == [["en"], ["el"], None, []]
    assert calls == ["/b/A/a.mkv", "/b/B.mkv", "/b/C.mkv", "/b/D/d.mkv"]
    assert "Local|/b/C.mkv|5|1" not in c["_subs"]  # failed probe: retried next run
    calls.clear()
    subtitles(rows, c)
    assert calls == ["/b/C.mkv"]  # the rest came from the cache
    globals()["_probe_local"] = saved_probe
    assert purge_cache(c) == (0, 0) and "_subs" in c

    def _msubs(a, b):
        es = [build_entry("r", "T", "2000", "movie", miss, subs=x) for x in (a, b)]
        return merge_duplicates(es)[0]["subs"]

    assert _msubs(None, []) == [] and _msubs(["en"], ["el"]) == ["el", "en"]
    assert _msubs(None, None) is None

    # original title: only in an original language, preferring one that isn't OMDb's
    zorba = [("Αλέξης Ζορμπάς", "el", "en"), ("Αλέξης Ζορμπάς", "el", "el"),
             ("Zorba the Greek", "en", "en"), ("Zorba the Greek", "en", "el")]
    assert pick_original("Zorba the Greek", zorba) == "Αλέξης Ζορμπάς"
    assert pick_original("Dark", [("Dark hosen", "en", "de")]) == ""
    assert pick_original("Severance", [("Severance", "en", "en")]) == ""
    assert pick_original("Kill Bill: Vol. 2", [("Kill Bill Volume 2", "en", "en")]) == ""
    assert pick_original("Porodica", [(None, None, "sr")]) == ""
    assert pick_original("X", []) == ""
    assert pick_original("Sirat", [("Sirāt", "es", "es"), ("صراط", "ar", "ar")]) == ""
    assert pick_original("Dogtooth", [("Κυνόδοντας", "el", "el")]) == "Κυνόδοντας"
    assert not _same("Αλέξης Ζορμπάς", "Ζ") and _same("Amici  Miei!", "amici miei")
    assert _same("Fantômas", "Fantomas") and _same("Katyń", "Katyn")
    hit = {"Response": "True", "Title": "Malena", "OrigTitle": "Malèna"}
    assert build_entry("r", "T", None, "movie", hit)["orig"] == ""  # same: not shown

    # a real rename saves the moved correction and the log as it goes, not at the end
    global HERE, CORRECTIONS_FILE
    saved = HERE, CORRECTIONS_FILE
    with tempfile.TemporaryDirectory() as td:
        HERE, CORRECTIONS_FILE = Path(td), Path(td) / "corrections.json"
        (Path(td) / "Trolosa").mkdir()
        hit = {"Response": "True", "Title": "Faithless", "Year": "2000"}
        corr = {"Trolosa": {"imdb_id": "tt0157122"}}
        rename_entries([build_entry("Trolosa", "Trolosa", None, "movie", hit,
                                    base=td)], corr, force=True)
        assert (Path(td) / "Faithless_(2000)").is_dir()
        assert json.loads(CORRECTIONS_FILE.read_text()) == {
            "Faithless_(2000)": {"imdb_id": "tt0157122"}}
        assert "Faithless_(2000)" in (Path(td) / "renames.log").read_text()
    HERE, CORRECTIONS_FILE = saved
    print(f"selftest: {len(cases)} cases passed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="ignore cache")
    ap.add_argument(
        "--selftest", action="store_true", help="run parser checks, no network"
    )
    ap.add_argument(
        "--rename",
        action="store_true",
        help="rename files/folders to the <Title>_(<year>) pattern",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="with --rename: only show the plan"
    )
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return

    key = os.environ.get("OMDB_API_KEY")
    if not key:
        sys.exit(
            "OMDB_API_KEY not set. Get a free key at https://www.omdbapi.com/apikey.aspx"
        )
    if not VIDEOS.is_dir():
        sys.exit(f"No such folder: {VIDEOS}")

    corrections = load_corrections()
    pickup_corrections(corrections)
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    stale, old = purge_cache(cache)
    if stale:
        print(f"  [info] purged {stale} transient error(s) cached by an older build")
    if old:
        print(f"  [info] retrying {old} miss(es) recorded before the current search")

    entries, pending = [], []
    try:  # quota is spent per lookup: keep what this run paid for, even on Ctrl-C
        for raw, title, year, typ, source, watched, base, is_dir, mtime, files in scan():
            corr = corrections.get(raw, {})
            if corr.get("ignore"):
                continue
            title = corr.get("title") or title
            year = corr.get("year") or year
            imdb_id = corr.get("imdb_id")
            data = omdb(title, year, typ, key, cache, args.refresh, imdb_id=imdb_id)
            ok = data.get("Response") == "True"
            tag = " OK " if ok else ("ERR " if data.get("transient") else "MISS")
            print(
                f"  [{tag}] {title} [{typ}] [{source}]{' [watched]' if watched else ''}"
                + (f" -- {data.get('Error')}" if data.get("transient") else "")
            )
            pending.append(
                (raw, title, year, typ, data, source, watched, base, mtime, is_dir, files)
            )
        original_titles([p[4] for p in pending])
        subs = subtitles([(p[5], p[7], p[0], p[9], p[10]) for p in pending], cache)
        for (raw, title, year, typ, data, source, watched, base, mtime, is_dir, _), sub in zip(
            pending, subs
        ):
            entries.append(
                build_entry(
                    raw, title, year, typ, data, source, watched, base, added=mtime,
                    is_dir=is_dir, subs=sub,
                )
            )
    finally:
        CACHE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    entries = merge_duplicates(entries)
    if LIMITED:
        pending = sum(not e["matched"] for e in entries)
        print(
            f"\n  [info] OMDb: {LIMITED[0]} Progress is saved in {CACHE.name}; re-run"
            f" later to look up the rest (up to {pending} unmatched)."
        )

    if args.rename:
        rename_entries(entries, corrections, force=not args.dry_run)
        CORRECTIONS_FILE.write_text(json.dumps(corrections, ensure_ascii=False, indent=2))
        return
    # A plain run never touches files: renaming is opt-in via --rename.
    CORRECTIONS_FILE.write_text(json.dumps(corrections, ensure_ascii=False, indent=2))
    if LIMITED:  # a half-looked-up page would replace a good one; keep the old page
        print(f"  [info] {OUT.name} left as it was.")
        return

    generate(entries, corrections)
    matched = sum(e["matched"] for e in entries)
    print(f"\n{len(entries)} titles ({matched} matched) -> {OUT}")


if __name__ == "__main__":
    main()

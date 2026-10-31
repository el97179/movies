# Spec: Mark a title as watched from the page (issue #8)

Files to change: `build.py` and `README.md` only. Python stdlib only, no new dependencies.
Read `.claude/specs/mark-watched/plan.md` first for the code map (line numbers at 8e5beae).

Do NOT click/trigger a real mark-watched against the real library or the NAS, and do NOT
run a publishing build: use `--no-publish` for any build. Moving real files is the user's
call; tests use a temp dir.

## Behaviour

On an unwatched card, a secondary "✓ Mark watched" button sits next to ▶ Play. Clicking it
asks `confirm('Move "<title>" to _watched?')`. On OK the browser follows a
`movies-play://watched?...` link, and the card is marked watched in the page right away (it
leaves the default unwatched view). The handler moves every copy of the title into
`<its base>/_watched/` and then rebuilds and republishes the page.

## B1. Page (HTML/CSS/JS template in build.py)

- CSS: add an outlined secondary button `a.watch`, based on the removed trailer style:
  `margin-top:auto;text-align:center;padding:8px;border-radius:8px;background:transparent;
  border:1px solid #363b47;color:#e8e8e8;text-decoration:none;font-size:13px`, hover
  `background:#262b36`. Play stays the filled main action (`flex:1`); the watch button does
  not grow.
- JS `watchBtn(m, i)`: returns `''` if `m.watched`. Otherwise the copies are `m.paths`
  (`[source, base, raw, is_dir]`). Build
  `'movies-play://watched?' + m.paths.map(p => 'src='+encodeURIComponent(p[0])+'&path='+encodeURIComponent(p[1]+'/'+p[2])).join('&')`.
  Render `<a class="watch" href="..." title="Move to _watched" onclick="...">✓ Mark watched</a>`.
- onclick: if `!confirm(...)` return false (no navigation). Otherwise let the default
  navigation happen and, in a `setTimeout(..., 0)`, set `m.watched = true` and call `render()`.
  Reach `m` the same way existing per-card handlers do (by card index / DATA). Escape the
  title for the attribute/JS context like the rest of the template does.
- Put `${watchBtn(m,i)}` in the card's button row (:1061-1063) after `${playBtn(m)}`. Not in
  the zoom modal.

## B2. Handler (Python)

- `watched_target(src, path)` -> destination path string, or `None` if not allowed. Pure, no
  I/O. Allowed only when:
  - `name = path.rsplit("/", 1)[1]` is non-empty, not `.`/`..`/`_watched`, doesn't start
    with `.`; and `parent = path.rsplit("/", 1)[0]` is
  - `src == "Local"`: exactly `str(VIDEOS)`;
  - otherwise exactly a REMOTE_DIRS path whose source == `src` and whose watched flag is False.
  - Returns `f"{parent}/_watched/{name}"`. (So nested paths, already-watched copies, `..`,
    and a path claimed under the wrong source are all rejected.)
- `mark_watched(pairs)` where pairs is `[(src, path), ...]`:
  1. Empty list, or any pair with `watched_target` None -> `_notify("Not in the library: ...")`
     and `sys.exit(1)`, moving nothing.
  2. For each pair: Local -> fail if source missing or destination exists; `mkdir(exist_ok=True)`
     the `_watched` dir; `Path(path).rename(dst)`. Remote -> one ssh call, same options as
     `rename_entries` (`BatchMode=yes`, `ConnectTimeout=10`, timeout 30, capture_output),
     command `test -e SRC && test ! -e DST && mkdir -p PARENT/_watched && mv SRC DST` with
     every path through `rq()`. A non-zero exit or exception = that copy failed.
     If `SARONIC_IP` is empty, remote copies fail with a message.
  3. Each successful move appends to `HERE/"renames.log"` in the existing format
     `ts\twatched\told\tnew` (undo trail). Failures don't stop the remaining copies.
  4. If at least one copy moved: rebuild by running
     `["bash", "-ic", 'exec "$0"', str(Path(sys.argv[0]).absolute())]` with
     `stdin=DEVNULL`, `capture_output=True`, `timeout=600` (`.bashrc` supplies
     `OMDB_API_KEY`, which the desktop-launched handler doesn't have). Catch
     SubprocessError/OSError.
  5. One `_notify` summary: moved copies (by source), failed copies with reason, and
     "page rebuilt" or "rebuild failed: <last non-empty stderr line>; run movies-db".
     Exit 1 if any copy failed or rebuild failed, else return normally.
- `play(url)`: after parsing, if `u.scheme == "movies-play" and u.netloc == "watched"`:
  `mark_watched(list(zip(q.get("src", []), q.get("path", []))))` and return. Existing
  `movies-play://play?...` behaviour unchanged. Update the docstring.

## B3. Selftest

Add plain asserts to `selftest()`:
- `watched_target`: Local `str(VIDEOS)+"/X_(2000)"` -> `str(VIDEOS)+"/_watched/X_(2000)"`;
  Saronic `~/Videos/movies/X` -> `~/Videos/movies/_watched/X`; WD
  `/volumeUSB1/usbshare/Videos/X` OK; rejected: `str(VIDEOS)+"/_watched/X"`,
  `~/Videos/movies/_watched/X` (Saronic), WD path with src `Saronic`, Saronic path with src
  `Local`, `str(VIDEOS)+"/a/b"`, `str(VIDEOS)+"/.."`, `str(VIDEOS)+"/_watched"`,
  `str(VIDEOS)+"/"`, `/etc/passwd`, unknown src.
- Temp-dir round trip of the local move: patch `VIDEOS` (and `HERE` for the log), stub the
  rebuild and `_notify` (or make the rebuild step injectable via a module-level indirection
  only if needed; keep it minimal), call `mark_watched` on a temp file + temp dir, assert they
  land in `_watched/` and the log line is written; a second call where the destination
  already exists must leave the source in place. Restore every patched global (try/finally),
  like the existing rename round trip.

## B4. README

In "The page" list add a bullet for ✓ Mark watched, and a short `### ✓ Mark watched`
subsection after `### ▶ Play`: what it moves (every copy, into its own folder's `_watched/`),
the confirm prompt, it reuses the Play handler (no extra setup beyond `--install-handler`),
only works on the machine with the handler, logs to `renames.log`, then rebuilds and
republishes (needs `OMDB_API_KEY` exported from `~/.bashrc`).

## Acceptance criteria

1. `./build.py --selftest` passes, including the new cases.
2. `./build.py --no-publish` succeeds; the generated `~/Videos/movies.html` contains the
   `watchBtn` function and `a.watch` CSS.
3. Headless render of `~/Videos/movies.html` (any available headless browser, e.g.
   `firefox --headless --screenshot` or chromium `--dump-dom`): no JS errors; unwatched cards
   show "✓ Mark watched" with a `movies-play://watched?src=...&path=...` href; with the
   Watched box ticked, cards show no such button.
4. `./build.py --play 'movies-play://watched?src=Local&path=%2Fetc%2Fpasswd'` exits 1 and
   moves nothing. `--play 'movies-play://watched'` (no pairs) exits 1.
5. `./build.py --play 'movies-play://play?...'` code path is unchanged (diff review).
6. `git diff --stat` touches only `build.py` and `README.md` (plus the pre-existing
   `cache.json` modification, which must not be reverted or committed by you).

## Test command

```bash
cd /home/tanman/dev/movies-db && ./build.py --selftest && ./build.py --no-publish
```
plus the headless render check and the two `--play` rejection calls above.

## Amendment 1 (refuter round 1 FAIL)

- R1 (blocking): `watched_target` must reject any `name` containing a control character
  (`any(ord(c) < 32 or c == "\x7f" for c in name)`; covers `\x00`, `\n`, `\t`). Also add
  `ValueError` to the `except` around each copy's move in `mark_watched`, so no exception can
  abort the loop. Add selftest rejects for `X\x00`, `X\nY`, `X\tY`.
- R2: a failed remote move's reason in the notification = last non-empty line of the ssh
  stderr (or "exit N" / "destination exists or source missing" when stderr is empty), not the
  `CalledProcessError` repr.
- R3: selftest round trip captures `_notify` text and asserts the second call's message says
  "destination exists", that `_watched/<name>` still holds the original content, and that
  renames.log line count is unchanged.
- R4: README.md:183 — renames.log column 2 can now also be `watched`. Remove the stray
  `pass` at build.py:1641 if it's dead.

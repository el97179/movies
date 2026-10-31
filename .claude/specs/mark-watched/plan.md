# Plan: Mark watched from the page (issue #8)

Sources: 3 scouts (Play handler, page HTML/JS, scan/publish/selftest) + 1 researcher, 2026-10-31.
Line numbers are build.py at commit 8e5beae.

## Code map

- `VIDEOS = Path.home()/"Videos"` :36, `SARONIC_IP` :37 (default alias `saronic`), `OUT` :43.
- `_entry()` :183-194 skips dot-names and `_watched`. Entries are one level deep under each base.
- `REMOTE_DIRS` :279-286: `(path, source, watched)`; Saronic `~/Videos/movies`, `~/Videos/series`
  (+ `/_watched`), WD `/volumeUSB1/usbshare/Videos` (+ `/_watched`).
- `scan()` :314-330: local base is `str(VIDEOS)` or `str(VIDEOS/"_watched")` (absolute); remote
  base is the literal REMOTE_DIRS path (`~/...` unexpanded). So `base` already ends in
  `/_watched` for watched copies.
- `build_entry` :695-747: `"paths": [[source, base, raw, is_dir]]` :740, `"watched"` :741,
  `"play": [[source, base/main-or-raw]]` :746. `merge_duplicates` :750-772 ORs `watched`.
- Page: `const DATA = __DATA__` :881; `playBtn(m)` :1001-1007 builds
  `movies-play://play?src=..&path=..` (encodeURIComponent); `card(m,i)` :1018-1065, button row
  :1061-1063 (`<div style="display:flex...">${playBtn(m)}</div>`); zoom modal `openZoom` :945
  also uses playBtn. Watched filter `&&m.watched===cWatched.checked` :987 in `render()` :979.
  CSS: `a.play` :822-824 (green #15803d); `.watched-badge` :818; `.card.watched` :820-821.
- Removed outlined secondary button CSS (c95a7ca):
  `a.trailer{margin-top:auto;text-align:center;padding:8px;border-radius:8px;background:transparent;border:1px solid #363b47;color:#e8e8e8;text-decoration:none;font-size:13px}`
  `a.trailer:hover{background:#262b36}`
- `publish(page)` :1091-1111 never raises. `generate()` :1082. No standalone rebuild function:
  pipeline is inline in `main()` :1747-1851; OMDb key check :1777-1781 (`sys.exit` if unset)
  comes after the `--play`/`--install-handler` dispatch :1770-1775.
- `rq(p)` :1189-1191 quotes a remote path, leaving `~/` unquoted. `rename_entries` :1194-1308:
  remote move is `ssh -o BatchMode=yes -o ConnectTimeout=10 SARONIC_IP "mv -n ..."`, appends
  `renames.log` as `ts\tkind\told\tnew` per move (:1297).
- Cache: OMDb key is path-free (:524); subtitle probe cache keyed `source|base/rel` (:482) ->
  a moved copy is re-probed once. Corrections keyed by raw name -> unaffected by a move.
- Handler: `play_target` :1658-1686 (validation pattern: rejects `..`, local must be under
  VIDEOS, remote must start with a REMOTE_DIRS base of that src), `_notify` :1689 (stderr +
  notify-send), `play(url)` :1697-1716, `install_handler` :1719-1744 writes `.desktop` with
  `Exec="<exe>" --play %u`, `MimeType=x-scheme-handler/movies-play` + portal permission.
- `selftest()` :1369-1641: plain asserts, temp-dir rename round-trip at :1626-1639 (patches
  HERE/CORRECTIONS_FILE and restores).

## Research findings

- `OMDB_API_KEY` is set only in `~/.bashrc` (not in systemd user env) -> a portal-launched
  handler will NOT have it; `main()` exits without it. Rebuild must source `.bashrc`.
- ssh `saronic`: IdentityFile `~/.ssh/id_ed25519`, unencrypted; SSH_AUTH_SOCK also in user env.
- NAS: `/bin/sh`, GNU coreutils 8.30 `mv`; all three `_watched/` dirs exist.
- zenity installed, but not needed: JS `confirm()` on the page does the confirmation.
- Cached `./build.py --no-publish` takes ~2 s (385 titles, no OMDb calls).
- `urlsplit("movies-play://watched?src=Local&path=%2Fa&src=WD&path=%2Fb")` -> netloc
  `watched`; `parse_qs` -> ordered `src`/`path` lists, decoded.

## Decisions

- Reuse the `movies-play://` scheme with host `watched`, routed inside `play()`: no new flag,
  no re-run of `--install-handler`, no new portal permission.
- Confirmation via JS `confirm()` before following the link (native, no dependency).
- Optimistic client update: mark the entry watched in DATA and re-render.
- Rebuild = run the program with no args via `bash -ic` so `.bashrc` supplies the key.

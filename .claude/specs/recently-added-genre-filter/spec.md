# Spec: "Recently added" sort + genre filter

Only file to change: `/home/tanman/dev/movies-db/build.py`. No new files, no new dependencies.
Do NOT run a live build (`python3 build.py` without `--selftest`) and do NOT ssh anywhere — NAS access is not permitted in this session. Verification is selftest + `node --check` only.

## Feature A — "Recently added" sort (folder/file mtime, nothing persisted)

### A1. Remote listing returns mtimes (build.py:193-228)
- Change the remote command at :208 from `f"ls -1p {path}"` to:
  `f"ls -1p {path} && (echo ::STAT::; cd {path} && stat -c '%Y|%n' -- * 2>/dev/null; true)"`
  - `ls` must stay first and unchanged so its exit code 2 (missing dir) still reaches the existing `r.returncode == 2` check; the subshell always exits 0 so a missing/unsupported `stat` never turns into a failed listing.
- Extract parsing into a pure module-level function `parse_remote_listing(out)` returning a list of `(name, is_dir, mtime)`:
  - Lines before the `::STAT::` line: same parsing as today (skip blank, `rstrip("/")`, `is_dir = endswith("/")`).
  - Lines after it: `mtime_str, name = line.split("|", 1)`; keep only lines that split into 2 parts with an all-digit mtime; map name → `int(mtime_str)`.
  - `mtime` for each listed entry = map lookup by name, else `None`. No sentinel, empty stat section, or garbled lines → `None`, never an exception.
- `remote_listing` yields `(name, is_dir, mtime)` from `parse_remote_listing(r.stdout)`. Update its docstring (keep the `ponytail:` note).

### A2. scan() carries mtime (build.py:241-254)
- Yield 8-tuples: `(raw, title, year, type, source, watched, base, mtime)`. `_entry` unchanged; append mtime in `scan()`.
- Local: `mtime = int(child.stat().st_mtime)`.
- Remote: mtime from A1 (may be `None`).
- Update the scan docstring and the single consumer at :1059 to unpack `mtime` and pass `added=mtime` to `build_entry`.

### A3. Record field
- `build_entry(..., base="", added=None)` emits `"added": added` (epoch seconds int, or `None` → JSON `null`). Existing callers that don't pass it get `None`.

### A4. Merge rule (build.py:382-399)
- In `merge_duplicates`, when merging a duplicate, set `m["added"]` to the **minimum of the non-null** values of `m["added"]` and `e["added"]` (null only if both null). Rationale: a fresh `cp` gives the new copy a new mtime; the oldest copy best represents "added".

### A5. UI
- Add `<option value="added">Recently added</option>` as the last option of `#sort` (:483-486).
- Sorting: the existing generic `else{x=num(a[key]);y=num(b[key]);}` branch already handles it (null → -1). Don't add a new branch unless needed.
- When the sort select changes to `added`, set `asc=false` and update `dirBtn.textContent` to `'▼ Desc'` (same strings as the existing toggle), then render. Other sort choices keep current `asc` behaviour unchanged. The direction button still toggles freely afterwards.
- Result: newest first; titles with `added: null` at the bottom.

## Feature B — Genre filter (multi-select, match any)

### B1. Markup
- In the header, after the Watched checkbox's following `<div class="sep"></div>` (i.e. just before `<select id="sort">`), add a native `<details id="genres"><summary>Genres</summary><div id="genreList"></div></details>` followed by a `<div class="sep"></div>`.
- CSS (in the existing `<style>`): `#genres` is `position:relative`; `#genreList` is an absolutely positioned dropdown panel (below the summary, above the grid via `z-index`, readable background matching the header/page colours already used in the stylesheet, vertical list or wrapping grid, scrollable with a `max-height`). Checkboxes reuse the existing `label.cb` class. `summary` gets `cursor:pointer`.

### B2. Options (JS, built once at load before first `render()`)
- Tokens = for every `DATA` row: `m.genre.split(',')`, `.trim()` each, drop `''` and `'N/A'`; dedupe; sort alphabetically.
- For each token append `<label class="cb"><input type="checkbox" value="TOKEN"> TOKEN</label>` to `#genreList`.
- **No backslashes anywhere in new JS** (the `HTML` template is not a raw string). No regex.

### B3. Filtering
- In `render()`, compute the set of checked genre values. Add to the filter: if the set is empty → no genre restriction; else a row passes iff any of its trimmed genre tokens is in the set (rows with empty/`N/A` genre are hidden while any genre is checked).
- Summary text shows the count when any are checked: `Genres (2)`; plain `Genres` when none.
- Wire with one delegated listener: `genreList.addEventListener('change',render)` (do not rely on the one-time bind list at :655).

## Selftest additions (inside `selftest()` / near the remote fixtures ~:975-1005; update, don't delete existing asserts)
1. `parse_remote_listing("a/\nb.mkv\n::STAT::\n100|a\n200|b.mkv\n")` → `[("a", True, 100), ("b.mkv", False, 200)]`.
2. No sentinel: `parse_remote_listing("a/\n")` → `[("a", True, None)]`.
3. Garbled/empty stat section: `parse_remote_listing("a/\n::STAT::\nnot-a-line\nx|a\n")` → `[("a", True, None)]`.
4. Name containing `|`: `"x|y/\n::STAT::\n5|x|y\n"` → `[("x|y", True, 5)]`.
5. `build_entry(...)` without `added` → `"added"` is `None`; with `added=123` → `123`.
6. `merge_duplicates` of two entries with same title/year and `added` 300 and 100 → merged `added == 100`; with `None` and 100 → `100`; both `None` → `None`.

## Acceptance criteria
- `python3 build.py --selftest` exits 0 and the new asserts above are present and exercised.
- Extracted page script passes `node --check` (command below).
- The remote command string ends with a subshell that always exits 0, and `ls -1p {path}` is still the first command.
- Template JS diff contains no `\` characters.
- `#sort` has a `Recently added` option with value `added`; selecting it sets descending and the button reads `▼ Desc`.
- Genre checkboxes are generated from data, exclude `N/A`/empty, and filter with OR semantics; no selection = no filtering; summary shows count.
- No other behaviour changes (search, source/type/watched filters, rename, corrections) — existing selftest asserts unchanged and passing.

## Test command
```
cd /home/tanman/dev/movies-db && python3 build.py --selftest && \
python3 -c "import re,build;open('/tmp/claude-1000/-home-tanman-dev-movies-db/f4960f30-3332-4e30-b142-079005e0fa06/scratchpad/page.js','w').write(re.findall(r'<script>(.*?)</script>',build.HTML,re.S)[-1])" && \
node --check /tmp/claude-1000/-home-tanman-dev-movies-db/f4960f30-3332-4e30-b142-079005e0fa06/scratchpad/page.js && echo ALL OK
```

## Amendment 1 (after refuter round 1, verdict PASS with non-blocking findings)
- A2: local mtime must not crash the scan. Wrap `child.stat()` so an `OSError` (dangling symlink, entry vanished mid-scan) gives `mtime = None`, matching the remote fail-soft behaviour. Add a selftest assert: a dangling symlink in a temp dir passed through the same helper/path yields `None` (use `tempfile`, no real ~/Videos).
- A1: the mtime check must accept ASCII digits only (e.g. `mtime_str.isascii() and mtime_str.isdigit()`), so `parse_remote_listing("a/\n::STAT::\n²|a\n")` returns `[("a", True, None)]` instead of raising. Add that as a selftest assert.
- Not changing: `::STAT::` as a filename, null-first in ascending order, dropdown outside-click, and descending kept after leaving "Recently added". All accepted as-is.

## Out of scope (known limits)
- NAS `stat -c '%Y|%n'` output is unverified on the real Synology (fails soft to `null`).
- Folder mtime changes when files are added inside the folder (e.g. a subtitle), which can bump an old title.
- No persistence of sort/filter state; no date shown on cards.

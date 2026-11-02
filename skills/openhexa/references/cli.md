# `hexa` CLI notes

Use `hexa <group> <command> --help` for options. This page covers what help does not:
some behavior that is easy to misread.

## Output

- Text output is for reading. `--json` prints one JSON document on stdout, and errors
  come out as JSON too: `{"code", "message", "exit_code", ...}`. Exit status is non-zero
  on error.
- Listings return `{"items": [...], "complete": bool, "pages_read", "total_items"}`.
  `--limit` caps the items shown; `--all` reads every page up to a safety ceiling. If
  `complete` is false, absence from the list proves nothing.
- The last line of `hexa <group> <command> --help` gives the JSON shape, e.g.
  `JSON: items[].dataset.{id,slug,...}`. In `datasets list`, `items[].id` is the
  **link** ID; use `items[].dataset.id`.

## Behavior worth knowing

- **Dataset selectors** accept a UUID, a slug, or `source-workspace/slug` for linked
  datasets. Ambiguous matches are rejected rather than guessed.
- **Versions.** `--version` accepts a name (`v3`), a number or a UUID. Omitting it uses
  the latest version, and the output says which one that was.
- **`download`** never overwrites. `--dir DIR` fetches a whole version with its relative
  paths (`--include GLOB` to filter); rerun it to finish an interrupted download.
- **`datasets diff DATASET V1 V2`** compares two versions by relative path without
  downloading. Same-size files are unverified unless you add `--content`, which hashes
  them in memory. Exit 0 = identical, 1 = different.
- **`upload`** names remote files after their basenames; use `--dir DIR` to keep
  relative paths (`--last FILE` for a completion marker). The new version holds only
  the uploaded files.
  If upload fails, the error reports which files were registered.
- **Write commands** print the target origin and workspace before writing. Once that is printed,
  the write may already have happened even if a later step fails.
- **`runs logs`** fetches all logs and displays the last `--limit` lines.
- **`pipelines source`** extracts uploaded code into a new directory. It never runs it.
- **`pipelines deploy DIR --pipeline CODE --version-name vN --dry-run`** (or
  `--new NAME`) shows the exact files, combined hash, timeout and parameters; rerun
  with `--yes` to deploy. It ships every file, assets included, except tool state and
  `DIR/.hexaignore` globs, and refuses packages over 5 MiB (put data in a dataset).
  The write is sent once; on any error check `hexa pipelines versions` before retrying.
- **`runs start P --version V --parameters FILE --dry-run`** checks inputs against
  that deployed version; `--yes` launches once (no emails). `runs wait RUN` prints
  status changes and exits 0 on success, 1 on failure/stop (showing the log tail),
  124 on timeout. Then download what the run published and check it.
- Not supported yet: sharing, deletion and bucket files. See [api.md](api.md).

# AGENTS.md

Instructions for coding agents working in this repository. Read `README.md` for what the tool does for its users.

## What this repo is

`cbz-shelf` is a local library and reader for `.cbz` comics. It scans folders into an SQLite database, keeps metadata, edits and reading progress there, and serves a library page and reader on cbz-webp's generic console server.

| Repo | Relation |
|---|---|
| `cbz-shelf` (this one, public) | The library and reader |
| `cbz-webp` (public) | A dependency, pinned by tag in `pyproject.toml`. Provides `serve`, `normalize_path`, `natural_key`, `is_junk`, `IMAGE_EXTS` and Pillow |
| `harbor` (private) | Mounts `LibraryAPI` at `/api/library/` and serves `UI_HTML` at `/library` in a tab. Treat `Library`, `LibraryAPI`, `UI_HTML` and the endpoint shapes as public API |

| Path | What it is |
|---|---|
| `cbzshelf/meta.py` | `ComicInfo.xml` and file-name parsing; the file/user/effective metadata layers |
| `cbzshelf/library.py` | `Library`: the schema, the background scanner, queries, edits, progress, pages and thumbnails |
| `cbzshelf/web.py` | `LibraryAPI` (`state`, `get`, `post`) and `serve_shelf` |
| `cbzshelf/ui.html` | The library page and reader: one file, CSS and JS inline, no external requests |
| `cbzshelf/cli.py` | `ui`, `add`, `scan`, `roots`, `search` |
| `tests/` | `test_library.py` (parsing, scanning, edits, missing files, search, pages), `test_web_cli.py` (endpoints, CLI); `helpers.make_cbz` builds archives |

## Rules that protect the user's library

1. **Archives are only ever read.** Nothing here opens an archive for writing, renames or deletes one. `test_archives_are_never_written` checks this.
2. **Two metadata layers.**
   - `file_json` is rebuilt from the archive on every scan.
   - `user_json` holds the user's edits, and a scan never touches it.
   - `meta_json` (effective: file, then user on top) is recomputed whenever either layer changes, and is what filters and search read.
   - Series have the same `user_json`. Never merge the layers into one column: the next rescan would erase the user's edits.
3. **Missing isn't deleted.**
   - A file that disappears gets `missing = 1` and keeps its row, edits and progress. If it comes back at the same path, it's restored.
   - Only `remove_missing` and `remove_root` delete rows, and both are explicit user actions.
   - A cancelled scan must not mark anything missing (it hasn't seen every file).
4. **Ids, never paths, from requests.** `page` and `thumbnail` look up the archive path by book id. Never add an endpoint that takes a file path, other than adding a library folder (`add-root`, which only scans).
5. **Untrusted archives.** `ComicInfo.xml` is size-capped, and a DOCTYPE or ENTITY is refused before parsing. An unreadable archive is recorded with `error` and `pages = 0`, never raised out of the scanner.

## Database and threads

- One SQLite connection, `check_same_thread=False`, with every use under `self._lock` (an `RLock`), in WAL mode. Don't open other connections.
- The schema is `CREATE ... IF NOT EXISTS`. To change it later, add a migration keyed on a stored version; never drop user data.
- `book_search` is an FTS5 table keyed by book id. `_reindex` refreshes it after any change to a book's metadata or its series name. `_fts_query` turns user text into quoted prefix terms, so user input is never FTS syntax.
- Every list endpoint is paginated (`limit` capped at 200). `/api/state` carries only `summary()`, because pages poll it often.
- The scanner runs in its own thread. `scan()` queues work, and the loop's start and exit are decided under `_scan_cond` (`_scan_looping`), so queued work is never stranded.

## The page

- Pages and covers use `&v=<file mtime>` in their URLs and are sent with `Cache-Control: private, max-age=86400`, so a changed file gets new URLs.
- The reader saves progress debounced (800 ms) and on close (`keepalive`). Per-series reader settings (`mode`, `fit`, `rtl`) are saved when changed.
- Direction rules: in right-to-left books, the left screen side and the left arrow go forward, and two-page spreads use `row-reverse`. Vertical mode tracks the current page with an `IntersectionObserver`.
- Works offline and in dark mode (CSS variables on `:root`). The page also runs inside harbor's iframe at `/library`, so keep every URL absolute from `/` and don't assume it's the top window.

## Testing

- Use temp folders and a temp `--db` only. Never scan the user's real library, and never use their database (`~/.local/share/cbz-shelf`, or harbor's).
- To try the page, run your own copy on a free port with a temp `--db` and `XDG_CACHE_HOME`, and stop it by PID.

```bash
pip install --target .deps .        # or: pip install --target .deps ../cbz-webp  (local checkout)
PYTHONPATH=.deps python3 -m unittest discover -s tests
ruff check . && ruff format --check .
```

Both must pass before you hand work back. CI pins ruff (`RUFF_VERSION` in `ci.yml`). Python 3.10+, so no syntax newer than 3.10 (CI runs 3.10, and ruff flags 3.12-only f-strings).

## Releasing

1. Set `__version__` in `cbzshelf/__init__.py`.
2. Move `## Unreleased` notes in `CHANGELOG.md` under `## <version>`.
3. Commit, push to `main`, wait for CI.
4. `git tag v<version> && git push origin v<version>`.

`release.yml` checks the tag against the version and the CHANGELOG, runs CI, and publishes a GitHub release with the wheel. harbor pins this repo by tag. Tag only when the user asks for a release.

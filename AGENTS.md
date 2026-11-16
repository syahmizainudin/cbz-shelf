# AGENTS.md

Instructions for coding agents working in this repository. Read `README.md` for what the tool does for its users.

## What this repo is

`cbz-shelf` is a local library and reader for `.cbz` comics and EPUB books. It scans folders into an SQLite database, keeps metadata, edits and reading progress there, and serves a library page and reader on cbz-webp's generic console server.

| Repo | Relation |
|---|---|
| `cbz-shelf` (this one, public) | The library and reader |
| `cbz-webp` (public) | A dependency, pinned by tag in `pyproject.toml`. Provides `serve`, `normalize_path`, `natural_key`, `is_junk`, `IMAGE_EXTS` and Pillow |
| `harbor` (private) | Mounts `LibraryAPI` at `/api/library/` and serves `UI_HTML` at `/library` in a tab. Treat `Library`, `LibraryAPI`, `UI_HTML` and the endpoint shapes as public API |

| Path | What it is |
|---|---|
| `cbzshelf/meta.py` | `ComicInfo.xml` and file-name parsing; the file/user/effective metadata layers |
| `cbzshelf/epub.py` | EPUB parsing (container, OPF, nav/NCX), picture-or-text classification, and rewriting a chapter's links for serving |
| `cbzshelf/library.py` | `Library`: the schema, the background scanner, queries, edits, progress, pages and thumbnails |
| `cbzshelf/web.py` | `LibraryAPI` (`state`, `get`, `post`) and `serve_shelf` |
| `cbzshelf/ui.html` | The library page and reader: one file, CSS and JS inline, no external requests |
| `cbzshelf/cli.py` | `ui`, `add`, `scan`, `roots`, `search` |
| `tests/` | `test_library.py` (parsing, scanning, edits, missing files, search, pages), `test_web_cli.py` (endpoints, CLI), `test_epub.py` (EPUB parsing, chapters, resources, migration); `helpers.make_cbz` and `helpers.make_epub` build books |

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
   - `res?book=&path=` takes a name *inside* an EPUB, not a file path. `epub.safe_name` normalizes it, refuses `..`, absolute names and backslashes, and requires the exact name to be in that book's zip. Only pictures, stylesheets and fonts are served this way; chapters go through `chapter?book=&n=` by spine index.
5. **Untrusted archives.** `ComicInfo.xml` is size-capped, and a DOCTYPE or ENTITY is refused before parsing. An unreadable archive is recorded with `error` and `pages = 0`, never raised out of the scanner.
6. **Untrusted EPUB content.**
   - EPUB XML (container, OPF, NCX) goes through `epub.safe_xml`: size cap, no ENTITY, no DOCTYPE internal subset. XHTML is only read with `html.parser`, never expanded as XML.
   - `chapter` and `res` responses carry `EPUB_CSP` (no scripts, no outside requests, `sandbox allow-same-origin`). The page shows chapters in `<iframe sandbox="allow-same-origin">`, never with `allow-scripts`, so the page's own code can style the chapter but the chapter can't run any. Keep all three layers: the CSP, the sandbox, and `rewrite_chapter` removing scripts, event attributes, `<base>`, refreshes and embeds.

## Database and threads

- One SQLite connection, `check_same_thread=False`, with every use under `self._lock` (an `RLock`), in WAL mode. Don't open other connections.
- The base schema is `CREATE ... IF NOT EXISTS`. Changes go in `MIGRATIONS` in `library.py`: each step runs once, in order, and `PRAGMA user_version` counts the steps run. Add new steps at the end, never edit an old one, and never drop user data. `test_0_1_database_is_upgraded_and_keeps_data` covers the upgrade.
- Reading progress (`progress`, keyed by `user_id, book_id`, with `hidden` for "left out of Continue reading") and `favourites(user_id, kind, item_id)` are per user. Every query on them takes a `user` (default 0, the only user without accounts). `BOOK_FROM` joins both, so its queries pass the user id twice, first. Metadata edits and series settings are shared.
- `LibraryAPI(library, user_of, can)`: `can(permission)` says whether the request's user may `library.read`, `library.edit` (book and series details) or `library.manage` (folders, scans, forgetting missing books). `POST_NEEDS`/`GET_NEEDS` map actions to permissions; anything unlisted needs `library.read`. A new action that changes shared data must be added to `POST_NEEDS`. `state()` reports `library_can` so the page hides what the user can't use, but the API's check is what counts.
- Text reader settings (`user_prefs`, `TEXT_PREFS`, endpoint `prefs`) are per user too.
- Reader settings (layout, fit, direction) are per user (`reader_prefs`, endpoint `reader-settings`) over the shared series settings, which only editors change.
- `LibraryAPI(library, user_of, can)`: `user_of()` names the user a request acts for. Standalone it's always 0; a host with accounts (harbor) supplies its signed-in user. Never take the user from the request body or query.
- Favourites point at book or series ids without a foreign key; `_drop_orphan_favourites` runs whenever books or series are deleted.
- `books.kind` is `cbz`, `epub-image` or `epub-text`. For text books `pages` counts chapters, `progress.page` is the chapter and `progress.pos` (0–1) how far down it.
- `book_search` is an FTS5 table keyed by book id. `_reindex` refreshes it after any change to a book's metadata or its series name. `_fts_query` turns user text into quoted prefix terms, so user input is never FTS syntax.
- Every list endpoint is paginated (`limit` capped at 200). `/api/state` carries only `summary()`, because pages poll it often.
- The scanner runs in its own thread. `scan()` queues work, and the loop's start and exit are decided under `_scan_cond` (`_scan_looping`), so queued work is never stranded.

## The page

- Pages and covers use `&v=<file mtime>` in their URLs and are sent with `Cache-Control: private, max-age=86400`, so a changed file gets new URLs.
- The reader saves progress debounced (800 ms) and on close (`keepalive`). Per-series reader settings (`mode`, `fit`, `rtl`) are saved when changed.
- Direction rules: in right-to-left books, the left screen side and the left arrow go forward, and two-page spreads use `row-reverse`. Vertical mode tracks the current page with an `IntersectionObserver`.
- Works offline and in dark mode (CSS variables on `:root`). The page also runs inside harbor's iframe at `/library`, so keep every URL absolute from `/` and don't assume it's the top window.
- In a frame, the reader posts `{type: "shelf-reader", open: true|false}` to the parent (same origin only) when a book opens and closes. harbor hides its sidebar on it. Keep the message's shape.

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

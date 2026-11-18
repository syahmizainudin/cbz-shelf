"""The library: an SQLite database of folders, series, books, metadata and reading progress.

Rules (see AGENTS.md):
  * Archives are only ever read. Edits and progress live in the database.
  * A scan rebuilds each book's ``file`` metadata; the ``user`` layer (edits) is never touched.
  * A file that disappears is marked ``missing``, keeping its edits and progress, until the user
    removes missing books on purpose. It comes back if the file returns at the same path.
  * Pages and covers are looked up by book id; a path from a request is never opened.
"""

from __future__ import annotations

import io
import json
import os
import posixpath
import re
import sqlite3
import threading
import time
import zipfile
from collections import OrderedDict
from pathlib import Path, PurePosixPath

from cbzwebp.core import IMAGE_EXTS, is_junk, natural_key, normalize_path
from PIL import Image

from . import epub as E
from . import meta as M

ARCHIVE_EXTS = (".cbz", ".zip")
BOOK_EXTS = (*ARCHIVE_EXTS, ".epub")
THUMB_SIZE = (320, 480)
MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
# What an EPUB chapter may load besides images: its stylesheets and fonts.
RESOURCE_MIME = {
    **MIME,
    ".svg": "image/svg+xml",
    ".css": "text/css; charset=utf-8",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
SCAN_BATCH = 100  # books written per transaction while scanning
SERIES_SETTINGS = {"mode": ("single", "double", "vertical"), "fit": ("width", "height", "original")}

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, added_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    key TEXT UNIQUE NOT NULL,          -- "dir:<folder>" or "ci:<root id>:<series name>"
    folder TEXT NOT NULL,
    file_name TEXT NOT NULL,           -- folder name or ComicInfo Series
    user_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    series_id INTEGER NOT NULL REFERENCES series(id),
    path TEXT UNIQUE NOT NULL,
    size INTEGER NOT NULL, mtime REAL NOT NULL,
    pages INTEGER NOT NULL DEFAULT 0, cover INTEGER NOT NULL DEFAULT 0,
    file_json TEXT NOT NULL DEFAULT '{}',   -- from the archive; rebuilt on every scan
    user_json TEXT NOT NULL DEFAULT '{}',   -- edits; never touched by a scan
    meta_json TEXT NOT NULL DEFAULT '{}',   -- effective = file + user, for filtering
    sort_key TEXT NOT NULL DEFAULT '',
    missing INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    added_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS books_series ON books(series_id);
CREATE INDEX IF NOT EXISTS books_root ON books(root_id);
CREATE TABLE IF NOT EXISTS progress (
    book_id INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
    page INTEGER NOT NULL, finished INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS progress_updated ON progress(updated_at);
CREATE VIRTUAL TABLE IF NOT EXISTS book_search USING fts5(
    title, series, authors, tags, summary, filename,
    tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3');
"""

# Each step runs once, in order; PRAGMA user_version records how many have run. Never drop user data.
MIGRATIONS = [
    # 1 (0.2.0): EPUB books, and a position inside a text book's chapter.
    """ALTER TABLE books ADD COLUMN kind TEXT NOT NULL DEFAULT 'cbz';   -- cbz | epub-image | epub-text
       ALTER TABLE progress ADD COLUMN pos REAL NOT NULL DEFAULT 0;      -- 0-1 down the chapter (text books)""",
    # 2 (0.3.0): progress per user (rows so far belong to user 0, the only user without accounts),
    # hiding a book from Continue reading, and favourites. Statements are split on semicolons.
    """CREATE TABLE progress2 (
           user_id INTEGER NOT NULL DEFAULT 0,
           book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
           page INTEGER NOT NULL, pos REAL NOT NULL DEFAULT 0, finished INTEGER NOT NULL DEFAULT 0,
           hidden INTEGER NOT NULL DEFAULT 0,   -- 1 = left out of Continue reading, progress kept
           updated_at REAL NOT NULL,
           PRIMARY KEY (user_id, book_id));
       INSERT INTO progress2(user_id, book_id, page, pos, finished, updated_at)
           SELECT 0, book_id, page, pos, finished, updated_at FROM progress;
       DROP TABLE progress;
       ALTER TABLE progress2 RENAME TO progress;
       CREATE INDEX progress_updated ON progress(user_id, updated_at);
       CREATE INDEX progress_book ON progress(book_id);
       CREATE TABLE favourites (
           user_id INTEGER NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('series', 'book')),
           item_id INTEGER NOT NULL, added_at REAL NOT NULL,
           PRIMARY KEY (user_id, kind, item_id))""",
    # 3 (0.4.0): each user's own reader settings per series (layout, fit, direction), over the
    # shared series settings.
    """CREATE TABLE reader_prefs (
           user_id INTEGER NOT NULL, series_id INTEGER NOT NULL, prefs_json TEXT NOT NULL DEFAULT '{}',
           PRIMARY KEY (user_id, series_id))""",
    # 4 (0.4.1): each user's own text reader settings (size, line width, colours).
    """CREATE TABLE user_prefs (user_id INTEGER PRIMARY KEY, prefs_json TEXT NOT NULL DEFAULT '{}')""",
    # 5 (next): indexes only. Counting books by missing (summary, roots), and each series' first
    # book in order (series covers).
    """CREATE INDEX books_missing_root ON books(missing, root_id);
       CREATE INDEX books_series_missing_sort ON books(series_id, missing, sort_key)""",
]
# The text reader's settings: name -> check. None as a value removes a setting.
TEXT_PREFS = {
    "text_size": lambda v: isinstance(v, int) and not isinstance(v, bool) and 70 <= v <= 220,
    "text_width": lambda v: v in ("32", "40", "56"),
    "text_theme": lambda v: v in ("original", "light", "sepia", "dark"),
}


def default_db() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "cbz-shelf" / "library.db"


def default_cache() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "cbz-shelf" / "thumbs"


def _sort_key(meta: dict, filename: str) -> str:
    """Volume, then number, then natural file name, as one sortable string."""

    def n(v):
        return f"{float(v):012.3f}" if isinstance(v, (int, float)) else "~" * 12

    nat = "".join(f"{t:010d}" if isinstance(t, int) else t for t in natural_key(filename))
    return f"{n(meta.get('volume'))}|{n(meta.get('number'))}|{nat}"


def page_names(zf: zipfile.ZipFile) -> list[str]:
    """The archive's images in reading order (natural sort, junk skipped)."""
    names = [
        i.filename
        for i in zf.infolist()
        if not i.is_dir()
        and not is_junk(i.filename)
        and PurePosixPath(i.filename).suffix.lower() in IMAGE_EXTS
    ]
    return sorted(names, key=natural_key)


class Library:
    def __init__(self, db_path: str | Path, cache_dir: str | Path | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache()
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            self._migrate()
        self._pages_cache: OrderedDict[tuple[str, float], list[str]] = OrderedDict()
        self._epub_cache: OrderedDict[tuple[str, float], E.EpubBook] = OrderedDict()
        # scanning
        self._scan_lock = threading.Lock()
        self._scan_cond = threading.Condition()
        self._scan_pending: list[int | None] = []
        self._scan_cancel = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._scan_looping = False  # set and cleared under _scan_cond, so scan() never misses an exiting loop
        self._stopping = False
        self.scan_state: dict = {"running": False, "root": None, "done": 0, "total": 0, "last": None}

    def _migrate(self) -> None:
        """Bring an older database up to date. Call with the lock held."""
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        for n, step in enumerate(MIGRATIONS[version:], start=version + 1):
            self._db.execute("BEGIN")
            try:
                for stmt in (x for x in step.split(";") if x.strip()):
                    self._db.execute(stmt)
                self._db.execute(f"PRAGMA user_version = {n}")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------ helpers
    def _q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _one(self, sql: str, args=()) -> sqlite3.Row | None:
        rows = self._q(sql, args)
        return rows[0] if rows else None

    def close(self) -> None:
        self.stop_scanner()
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------ roots
    def roots(self) -> list[dict]:
        rows = self._q(
            """SELECT r.id, r.path, r.added_at,
                      (SELECT count(*) FROM books b WHERE b.root_id = r.id AND b.missing = 0) AS books,
                      (SELECT count(*) FROM books b WHERE b.root_id = r.id AND b.missing = 1) AS missing
               FROM roots r ORDER BY r.path"""
        )
        return [dict(r) for r in rows]

    def add_root(self, raw: str) -> dict:
        path = normalize_path(raw)
        if not path.is_dir():
            raise ValueError(f"not a folder: {path}")
        for r in self.roots():
            other = Path(r["path"])
            if path == other:
                raise ValueError(f"already in the library: {path}")
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise ValueError(f"overlaps a library folder: {other}")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO roots(path, added_at) VALUES (?, ?)", (str(path), time.time())
            )
            root_id = cur.lastrowid
        self.scan(root_id)
        return {"id": root_id, "path": str(path)}

    def remove_root(self, root_id: int) -> bool:
        """Forget a folder and everything in it (books, edits, progress). Files are not touched."""
        with self._lock:
            self._db.execute("BEGIN")
            self._db.execute(
                "DELETE FROM book_search WHERE rowid IN (SELECT id FROM books WHERE root_id = ?)", (root_id,)
            )
            self._db.execute("DELETE FROM books WHERE root_id = ?", (root_id,))
            self._db.execute("DELETE FROM series WHERE root_id = ?", (root_id,))
            n = self._db.execute("DELETE FROM roots WHERE id = ?", (root_id,)).rowcount
            self._drop_orphan_favourites()
            self._db.execute("COMMIT")
        return bool(n)

    def _root_paths(self) -> list[dict]:
        """Every folder's id and path, without the book counts ``roots()`` adds."""
        return [dict(r) for r in self._q("SELECT id, path FROM roots ORDER BY path")]

    def root_for(self, path: Path) -> dict | None:
        for r in self._root_paths():
            if path.is_relative_to(r["path"]):
                return r
        return None

    # ------------------------------------------------------------ scanning
    def scan(self, root_id: int | None = None) -> None:
        """Queue a scan of one folder (or all). Runs in the background."""
        with self._scan_cond:
            if root_id not in self._scan_pending and None not in self._scan_pending:
                self._scan_pending.append(root_id)
            if not self._scan_looping and not self._stopping:
                self._scan_looping = True
                self._scan_thread = threading.Thread(target=self._scan_loop, name="library-scan", daemon=True)
                self._scan_thread.start()
            self._scan_cond.notify_all()

    def cancel_scan(self) -> None:
        with self._scan_cond:
            self._scan_pending.clear()
        self._scan_cancel.set()

    def wait_scan(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._scan_cond:
                if not self._scan_pending and not self.scan_state["running"]:
                    return
            if deadline and time.monotonic() > deadline:
                raise TimeoutError("scan still running")
            time.sleep(0.05)

    def stop_scanner(self) -> None:
        with self._scan_cond:
            self._stopping = True
            self._scan_pending.clear()
            self._scan_cond.notify_all()
        self._scan_cancel.set()
        if self._scan_thread is not None:
            self._scan_thread.join()

    def _scan_loop(self) -> None:
        while True:
            with self._scan_cond:
                if self._stopping or not self._scan_pending:
                    self._scan_looping = False
                    return
                target = self._scan_pending.pop(0)
                self.scan_state.update(running=True, done=0, total=0)
            self._scan_cancel.clear()
            try:
                roots = [r for r in self._root_paths() if target is None or r["id"] == target]
                totals = {"added": 0, "updated": 0, "missing": 0, "errors": 0}
                for r in roots:
                    if self._scan_cancel.is_set():
                        break
                    for k, v in self._scan_root(r).items():
                        totals[k] += v
                totals["cancelled"] = self._scan_cancel.is_set()
                totals["finished_at"] = time.time()
                self.scan_state["last"] = totals
            finally:
                with self._scan_cond:
                    self.scan_state.update(running=False, root=None)

    def _scan_root(self, root: dict) -> dict:
        root_path = Path(root["path"])
        self.scan_state["root"] = root["path"]
        files: list[Path] = []
        if root_path.is_dir():
            for dirpath, dirnames, filenames in os.walk(root_path):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                files += [Path(dirpath, f) for f in filenames if f.lower().endswith(BOOK_EXTS)]
        self.scan_state.update(total=len(files), done=0)
        known = {
            r["path"]: r
            for r in self._q(
                "SELECT id, path, size, mtime, missing FROM books WHERE root_id = ?", (root["id"],)
            )
        }
        counts = {"added": 0, "updated": 0, "missing": 0, "errors": 0}
        seen: set[str] = set()
        # Files are read with no lock held; what they give is written SCAN_BATCH at a time, each
        # batch in one transaction. Whatever is still pending is written on the way out, also when
        # the scan is cancelled or stopped.
        pending: list[dict] = []
        try:
            for f in sorted(files, key=natural_key):
                if self._scan_cancel.is_set():
                    return counts  # don't mark anything missing after a partial walk
                seen.add(str(f))
                try:
                    st = f.stat()
                except OSError:
                    continue
                old = known.get(str(f))
                if old and old["size"] == st.st_size and old["mtime"] == st.st_mtime and not old["missing"]:
                    self.scan_state["done"] += 1
                    continue
                book = self._read_book(f, st)
                pending.append(book)
                counts["added" if not old else "updated"] += 1
                if book["error"]:
                    counts["errors"] += 1
                self.scan_state["done"] += 1
                if len(pending) >= SCAN_BATCH:
                    batch, pending = pending, []
                    self._write_books(root, batch)
        finally:
            if pending:
                self._write_books(root, pending)
        gone = [r["id"] for p, r in known.items() if p not in seen and not r["missing"]]
        if gone:
            with self._lock:
                self._db.executemany("UPDATE books SET missing = 1 WHERE id = ?", [(i,) for i in gone])
            counts["missing"] = len(gone)
        self._drop_empty_series()
        return counts

    def scan_file(self, path: str | Path) -> bool:
        """Index one file now (e.g. a conversion just finished). False if it's outside the library."""
        f = normalize_path(str(path))
        root = self.root_for(f)
        if not root or not f.is_file() or not f.name.lower().endswith(BOOK_EXTS):
            return False
        self._write_books(root, [self._read_book(f, f.stat())])
        self._drop_empty_series()
        return True

    def _read_archive(self, f: Path) -> tuple[int, int, dict, str]:
        """(pages, cover page, metadata, kind) of a book file."""
        if f.suffix.lower() == ".epub":
            with zipfile.ZipFile(f) as zf:
                book = E.parse(zf)
            cover = book.images.index(book.cover) if book.cover in book.images else 0
            return book.pages, cover, book.meta, book.kind
        with zipfile.ZipFile(f) as zf:
            pages = page_names(zf)
            file_meta: dict = {}
            cover = 0
            info = next(
                (i for i in zf.infolist() if PurePosixPath(i.filename).name.lower() == "comicinfo.xml"), None
            )
            if info is not None and info.file_size <= M.COMICINFO_MAX:
                file_meta, ci_cover = M.parse_comicinfo(zf.read(info))
                if ci_cover is not None and 0 <= ci_cover < len(pages):
                    cover = ci_cover
        return len(pages), cover, file_meta, "cbz"

    def _read_book(self, f: Path, st: os.stat_result) -> dict:
        """Everything a book's row needs from its file. Reads the file only; no lock, no database."""
        error = None
        kind = "epub-text" if f.suffix.lower() == ".epub" else "cbz"
        try:
            pages, cover, ci, kind = self._read_archive(f)
        except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as e:
            pages, cover, ci, error = 0, 0, {}, f"cannot read: {e}"
        file_meta = {**M.parse_filename(f.stem), **ci}
        file_meta.setdefault("title", f.stem)
        return {
            "path": f,
            "st": st,
            "pages": pages,
            "cover": cover,
            "file_meta": file_meta,
            "series": ci.get("series"),
            "error": error,
            "kind": kind,
        }

    def _write_books(self, root: dict, books: list[dict]) -> None:
        """Add or update rows for books read by ``_read_book``, in one transaction.

        The transaction holds ``_lock`` from BEGIN to COMMIT and covers only database writes: the
        connection is shared, so another thread's write must never land inside it. Covers made
        for a file's old version are deleted after the commit."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                stale = [t for b in books if (t := self._write_book(root, b))]
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        for thumb in stale:
            thumb.unlink(missing_ok=True)

    def _write_book(self, root: dict, b: dict) -> Path | None:
        """One book's row and search entry. Call inside ``_write_books``' transaction. Returns the
        cached cover of the file's old version, if the file has changed."""
        f, st, file_meta = b["path"], b["st"], b["file_meta"]
        series_id = self._series_for(root, f, b["series"])
        row = self._db.execute("SELECT id, user_json, mtime FROM books WHERE path = ?", (str(f),)).fetchone()
        user = json.loads(row["user_json"]) if row else {}
        eff = M.effective(file_meta, user)
        values = (
            series_id,
            st.st_size,
            st.st_mtime,
            b["pages"],
            b["cover"],
            json.dumps(file_meta),
            json.dumps(eff),
            _sort_key(eff, f.name),
            b["error"],
            b["kind"],
        )
        if row:
            book_id = row["id"]
            self._db.execute(
                """UPDATE books SET series_id=?, size=?, mtime=?, pages=?, cover=?, file_json=?, meta_json=?,
                   sort_key=?, error=?, kind=?, missing=0 WHERE id=?""",
                (*values, book_id),
            )
        else:
            book_id = self._db.execute(
                """INSERT INTO books(series_id, size, mtime, pages, cover, file_json, meta_json, sort_key, error,
                   kind, root_id, path, added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*values, root["id"], str(f), time.time()),
            ).lastrowid
        self._reindex(book_id)
        if row and row["mtime"] != st.st_mtime:
            return self.cache_dir / f"{int(book_id)}-{int(row['mtime'])}.webp"
        return None

    def _series_for(self, root: dict, f: Path, ci_series: str | None) -> int:
        folder = str(f.parent)
        if ci_series:
            key, name = f"ci:{root['id']}:{ci_series.strip().lower()}", ci_series.strip()
        else:
            key, name = f"dir:{folder}", f.parent.name or folder
        with self._lock:
            row = self._db.execute("SELECT id FROM series WHERE key = ?", (key,)).fetchone()
            if row:
                return row["id"]
            return self._db.execute(
                "INSERT INTO series(root_id, key, folder, file_name) VALUES (?,?,?,?)",
                (root["id"], key, folder, name),
            ).lastrowid

    def _drop_empty_series(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM series WHERE id NOT IN (SELECT DISTINCT series_id FROM books)")
            self._drop_orphan_favourites()

    def _drop_orphan_favourites(self) -> None:
        """Favourites of books or series that no longer exist. Call with the lock held."""
        self._db.execute(
            "DELETE FROM favourites WHERE kind = 'book' AND item_id NOT IN (SELECT id FROM books)"
        )
        self._db.execute(
            "DELETE FROM favourites WHERE kind = 'series' AND item_id NOT IN (SELECT id FROM series)"
        )
        self._db.execute("DELETE FROM reader_prefs WHERE series_id NOT IN (SELECT id FROM series)")

    def _reindex(self, book_id: int) -> None:
        """Refresh one book's search entry. Call with the lock held."""
        r = self._db.execute(
            """SELECT b.path, b.meta_json, s.file_name, s.user_json FROM books b JOIN series s ON s.id = b.series_id
               WHERE b.id = ?""",
            (book_id,),
        ).fetchone()
        m = json.loads(r["meta_json"])
        series_name = json.loads(r["user_json"]).get("name") or r["file_name"]
        self._db.execute("DELETE FROM book_search WHERE rowid = ?", (book_id,))
        self._db.execute(
            "INSERT INTO book_search(rowid, title, series, authors, tags, summary, filename) VALUES (?,?,?,?,?,?,?)",
            (
                book_id,
                m.get("title", ""),
                series_name,
                " ".join(m.get("authors", [])),
                " ".join(m.get("tags", []) + m.get("genres", [])),
                m.get("summary", ""),
                Path(r["path"]).stem,
            ),
        )

    def remove_missing(self, root_id: int | None = None) -> int:
        """Forget books whose files are gone, with their edits and progress."""
        where, args = ("missing = 1 AND root_id = ?", (root_id,)) if root_id else ("missing = 1", ())
        with self._lock:
            self._db.execute("BEGIN")
            self._db.execute(
                f"DELETE FROM book_search WHERE rowid IN (SELECT id FROM books WHERE {where})", args
            )
            removed = self._db.execute(f"DELETE FROM books WHERE {where}", args).rowcount
            self._db.execute("COMMIT")
        self._drop_empty_series()
        return removed

    # ------------------------------------------------------------ reading data
    def _book_dict(self, r: sqlite3.Row) -> dict:
        m = json.loads(r["meta_json"])
        d = {
            "id": r["id"],
            "series_id": r["series_id"],
            "title": m.get("title") or Path(r["path"]).stem,
            "number": m.get("number"),
            "volume": m.get("volume"),
            "authors": m.get("authors", []),
            "tags": m.get("tags", []),
            "pages": r["pages"],
            "missing": bool(r["missing"]),
            "error": r["error"],
            "v": int(r["mtime"]),
            "file": Path(r["path"]).name,
            "kind": r["kind"],
        }
        if "favourite" in r.keys():
            d["favourite"] = bool(r["favourite"])
        if "page" in r.keys():
            d["progress"] = (
                None
                if r["page"] is None
                else {
                    "page": r["page"],
                    "pos": r["pos"],
                    "finished": bool(r["finished"]),
                    "at": r["updated_at"],
                }
            )
        return d

    # Progress and favourites are per user: queries using BOOK_FROM pass the user id twice, first.
    BOOK_COLS = (
        "b.id, b.series_id, b.path, b.meta_json, b.pages, b.missing, b.error, b.mtime, b.kind,"
        " p.page, p.pos, p.finished, p.updated_at, f.item_id IS NOT NULL AS favourite"
    )
    BOOK_FROM = (
        "books b LEFT JOIN progress p ON p.book_id = b.id AND p.user_id = ?"
        " LEFT JOIN favourites f ON f.kind = 'book' AND f.item_id = b.id AND f.user_id = ?"
    )

    def series_list(
        self,
        q: str = "",
        tag: str = "",
        author: str = "",
        sort: str = "name",
        limit: int = 60,
        offset: int = 0,
        favourite: bool = False,
        user: int = 0,
    ) -> dict:
        where, args = ["1=1"], []
        if favourite:
            where.append("s.id IN (SELECT item_id FROM favourites WHERE kind = 'series' AND user_id = ?)")
            args.append(int(user))
        if q:
            where.append(
                "s.id IN (SELECT b.series_id FROM books b WHERE b.id IN (SELECT rowid FROM book_search WHERE book_search MATCH ?))"
            )
            args.append(_fts_query(q))
        for field, value in (("tags", tag), ("authors", author)):
            if value:
                where.append(
                    f"s.id IN (SELECT b.series_id FROM books b, json_each(b.meta_json, '$.{field}') j WHERE j.value = ?)"
                )
                args.append(value)
        order = {
            "name": "lower(name)",
            "recent": "last_read DESC NULLS LAST, lower(name)",
            "added": "added DESC",
        }.get(sort, "lower(name)")
        sql = f"""
            SELECT s.id, s.folder, coalesce(json_extract(s.user_json, '$.name'), s.file_name) AS name,
                   count(b.id) AS books,
                   sum(CASE WHEN p.finished = 1 THEN 1 ELSE 0 END) AS finished,
                   sum(b.missing) AS missing,
                   max(b.added_at) AS added, max(p.updated_at) AS last_read,
                   (SELECT b2.id FROM books b2 WHERE b2.series_id = s.id AND b2.missing = 0 ORDER BY b2.sort_key LIMIT 1) AS cover_book,
                   (SELECT CAST(b2.mtime AS INTEGER) FROM books b2 WHERE b2.series_id = s.id AND b2.missing = 0 ORDER BY b2.sort_key LIMIT 1) AS cover_v,
                   EXISTS (SELECT 1 FROM favourites fv WHERE fv.kind = 'series' AND fv.item_id = s.id AND fv.user_id = ?) AS favourite
            FROM series s JOIN books b ON b.series_id = s.id LEFT JOIN progress p ON p.book_id = b.id AND p.user_id = ?
            WHERE {" AND ".join(where)}
            GROUP BY s.id ORDER BY {order} LIMIT ? OFFSET ?"""
        total_sql = f"SELECT count(*) FROM series s WHERE {' AND '.join(where)}"
        items = [dict(r) for r in self._q(sql, (int(user), int(user), *args, int(limit), int(offset)))]
        for it in items:
            it["favourite"] = bool(it["favourite"])
        total = self._one(total_sql, args)[0]
        return {"items": items, "total": total}

    def series_detail(self, series_id: int, user: int = 0) -> dict:
        s = self._one("SELECT * FROM series WHERE id = ?", (series_id,))
        if not s:
            raise KeyError(f"no series {series_id}")
        edits = json.loads(s["user_json"])
        mine_row = self._one(
            "SELECT prefs_json FROM reader_prefs WHERE user_id = ? AND series_id = ?", (int(user), s["id"])
        )
        mine = json.loads(mine_row["prefs_json"]) if mine_row else {}
        rows = self._q(
            f"SELECT {self.BOOK_COLS} FROM {self.BOOK_FROM} WHERE b.series_id = ? ORDER BY b.sort_key",
            (int(user), int(user), series_id),
        )
        books = [self._book_dict(r) for r in rows]
        file_rtl = any(json.loads(r["meta_json"]).get("rtl") is True for r in rows)
        authors: list[str] = []
        tags: list[str] = []
        for b in books:
            authors += [a for a in b["authors"] if a not in authors]
            tags += [t for t in b["tags"] if t not in tags]
        return {
            "id": s["id"],
            "folder": s["folder"],
            "file_name": s["file_name"],
            "name": edits.get("name") or s["file_name"],
            "summary": edits.get("summary", ""),
            "user": edits,
            "authors": edits.get("authors") or authors,
            "tags": edits.get("tags") or tags,
            "reader": {
                "rtl": mine.get("rtl", edits.get("rtl", file_rtl)),
                "mode": mine.get("mode", edits.get("mode", "single")),
                "fit": mine.get("fit", edits.get("fit", "height")),
            },
            "reader_mine": mine,
            "books": books,
            "favourite": self._is_favourite(user, "series", s["id"]),
        }

    def book_detail(self, book_id: int, user: int = 0) -> dict:
        r = self._one(
            f"SELECT {self.BOOK_COLS}, b.file_json, b.user_json FROM {self.BOOK_FROM} WHERE b.id = ?",
            (int(user), int(user), book_id),
        )
        if not r:
            raise KeyError(f"no book {book_id}")
        d = self._book_dict(r)
        d.update(
            file_meta=json.loads(r["file_json"]),
            user_meta=json.loads(r["user_json"]),
            meta=json.loads(r["meta_json"]),
        )
        if d["kind"] == "epub-text" and not d["missing"] and not d["error"]:
            try:
                book = self._epub(self._book_file(book_id))
                d["toc"], d["spine"] = book.toc, book.spine
            except (OSError, ValueError, zipfile.BadZipFile):
                d["toc"], d["spine"] = [], []
        return d

    def search(
        self,
        q: str = "",
        tag: str = "",
        author: str = "",
        limit: int = 60,
        offset: int = 0,
        favourite: bool = False,
        user: int = 0,
    ) -> dict:
        where, args = ["1=1"], []
        if favourite:
            where.append("b.id IN (SELECT item_id FROM favourites WHERE kind = 'book' AND user_id = ?)")
            args.append(int(user))
        if q:
            where.append("b.id IN (SELECT rowid FROM book_search WHERE book_search MATCH ?)")
            args.append(_fts_query(q))
        for field, value in (("tags", tag), ("authors", author)):
            if value:
                where.append(
                    f"EXISTS (SELECT 1 FROM json_each(b.meta_json, '$.{field}') j WHERE j.value = ?)"
                )
                args.append(value)
        w = " AND ".join(where)
        rows = self._q(
            f"SELECT {self.BOOK_COLS} FROM {self.BOOK_FROM} WHERE {w} ORDER BY b.sort_key LIMIT ? OFFSET ?",
            (int(user), int(user), *args, int(limit), int(offset)),
        )
        total = self._one(f"SELECT count(*) FROM books b WHERE {w}", args)[0]
        return {"items": [self._book_dict(r) for r in rows], "total": total}

    def continue_reading(self, limit: int = 12, user: int = 0) -> list[dict]:
        rows = self._q(
            f"""SELECT {self.BOOK_COLS} FROM {self.BOOK_FROM}
                WHERE p.finished = 0 AND p.hidden = 0 AND b.missing = 0 ORDER BY p.updated_at DESC LIMIT ?""",
            (int(user), int(user), int(limit)),
        )
        return [self._book_dict(r) for r in rows]

    def hide_continue(self, book_id: int | None = None, user: int = 0) -> int:
        """Leave a book (or, with no id, every book) out of Continue reading. The progress is kept,
        and reading the book again brings it back. Returns how many were hidden."""
        with self._lock:
            if book_id is None:
                sql, args = "UPDATE progress SET hidden = 1 WHERE user_id = ? AND hidden = 0", (int(user),)
            else:
                sql, args = (
                    "UPDATE progress SET hidden = 1 WHERE user_id = ? AND book_id = ?",
                    (int(user), int(book_id)),
                )
            return self._db.execute(sql, args).rowcount

    def give_user_data(self, from_user: int, to_user: int) -> int:
        """Move one user's progress and favourites to another, keeping the receiver's own where
        both have one (a host turning user 0 into its first account). Returns rows moved."""
        moved = 0
        with self._lock:
            self._db.execute("BEGIN")
            for table, cols in (
                ("progress", "book_id, page, pos, finished, hidden, updated_at"),
                ("favourites", "kind, item_id, added_at"),
                ("reader_prefs", "series_id, prefs_json"),
            ):
                moved += self._db.execute(
                    f"INSERT OR IGNORE INTO {table}(user_id, {cols}) SELECT ?, {cols} FROM {table} WHERE user_id = ?",
                    (int(to_user), int(from_user)),
                ).rowcount
                self._db.execute(f"DELETE FROM {table} WHERE user_id = ?", (int(from_user),))
            self._db.execute("COMMIT")
        return moved

    def set_reader_prefs(self, series_id: int, changes: dict, user: int = 0) -> dict:
        """One user's reader settings for a series (``mode``, ``fit``, ``rtl``); None removes one.
        They win over the shared series settings for that user only."""
        if not self._one("SELECT 1 FROM series WHERE id = ?", (int(series_id),)):
            raise KeyError(f"no series {series_id}")
        row = self._one(
            "SELECT prefs_json FROM reader_prefs WHERE user_id = ? AND series_id = ?",
            (int(user), int(series_id)),
        )
        prefs = json.loads(row["prefs_json"]) if row else {}
        for key, value in changes.items():
            if key not in ("mode", "fit", "rtl"):
                raise ValueError(f"unknown reader setting: {key}")
            if value is None:
                prefs.pop(key, None)
            else:
                prefs[key] = _check_reader_setting(key, value)
        self._save_prefs("reader_prefs", {"user_id": int(user), "series_id": int(series_id)}, prefs)
        return self.series_detail(series_id, user=user)["reader"]

    def user_prefs(self, user: int = 0) -> dict:
        row = self._one("SELECT prefs_json FROM user_prefs WHERE user_id = ?", (int(user),))
        return json.loads(row["prefs_json"]) if row else {}

    def set_user_prefs(self, changes: dict, user: int = 0) -> dict:
        """One user's text reader settings (``TEXT_PREFS``)."""
        prefs = self.user_prefs(user)
        for key, value in changes.items():
            if key not in TEXT_PREFS:
                raise ValueError(f"unknown setting: {key}")
            if value is None:
                prefs.pop(key, None)
            elif not TEXT_PREFS[key](value):
                raise ValueError(f"bad value for {key}")
            else:
                prefs[key] = value
        self._save_prefs("user_prefs", {"user_id": int(user)}, prefs)
        return prefs

    def _save_prefs(self, table: str, key: dict, prefs: dict) -> None:
        """Insert or replace one ``prefs_json`` row of ``reader_prefs`` or ``user_prefs``."""
        cols = ", ".join(key)
        with self._lock:
            self._db.execute(
                f"""INSERT INTO {table}({cols}, prefs_json) VALUES ({", ".join("?" * (len(key) + 1))})
                    ON CONFLICT({cols}) DO UPDATE SET prefs_json = excluded.prefs_json""",
                (*key.values(), json.dumps(prefs)),
            )

    # ------------------------------------------------------------ favourites
    def _is_favourite(self, user: int, kind: str, item_id: int) -> bool:
        return bool(
            self._one(
                "SELECT 1 FROM favourites WHERE user_id = ? AND kind = ? AND item_id = ?",
                (int(user), kind, int(item_id)),
            )
        )

    def set_favourite(self, kind: str, item_id: int, on: bool = True, user: int = 0) -> bool:
        """Star or unstar a book or series. Returns the new state."""
        if kind not in ("series", "book"):
            raise ValueError("kind must be series or book")
        table = "series" if kind == "series" else "books"
        if not self._one(f"SELECT 1 FROM {table} WHERE id = ?", (int(item_id),)):
            raise KeyError(f"no {kind} {item_id}")
        with self._lock:
            if on:
                self._db.execute(
                    "INSERT OR IGNORE INTO favourites(user_id, kind, item_id, added_at) VALUES (?,?,?,?)",
                    (int(user), kind, int(item_id), time.time()),
                )
            else:
                self._db.execute(
                    "DELETE FROM favourites WHERE user_id = ? AND kind = ? AND item_id = ?",
                    (int(user), kind, int(item_id)),
                )
        return bool(on)

    def facets(self, limit: int = 200) -> dict:
        out = {}
        for field in ("tags", "authors"):
            rows = self._q(
                f"""SELECT j.value AS value, count(*) AS n FROM books b, json_each(b.meta_json, '$.{field}') j
                    WHERE b.missing = 0 GROUP BY j.value ORDER BY n DESC, lower(j.value) LIMIT ?""",
                (int(limit),),
            )
            out[field] = [{"value": r["value"], "n": r["n"]} for r in rows]
        return out

    def summary(self) -> dict:
        r = self._one(
            """SELECT (SELECT count(*) FROM books WHERE missing = 0) AS books,
                      (SELECT count(*) FROM books WHERE missing = 1) AS missing,
                      (SELECT count(*) FROM series) AS series,
                      (SELECT count(*) FROM roots) AS roots"""
        )
        return {**dict(r), "scan": dict(self.scan_state)}

    # ------------------------------------------------------------ edits and progress
    def edit_book(self, book_id: int, changes: dict) -> dict:
        if not isinstance(changes, dict):
            raise ValueError("changes must be an object")
        with self._lock:
            r = self._db.execute(
                "SELECT path, file_json, user_json FROM books WHERE id = ?", (book_id,)
            ).fetchone()
            if not r:
                raise KeyError(f"no book {book_id}")
            if "series" in changes:
                raise ValueError(
                    "a book's series comes from its folder or ComicInfo; rename the series instead"
                )
            user = M.apply_changes(json.loads(r["user_json"]), changes)
            eff = M.effective(json.loads(r["file_json"]), user)
            self._db.execute(
                "UPDATE books SET user_json = ?, meta_json = ?, sort_key = ? WHERE id = ?",
                (json.dumps(user), json.dumps(eff), _sort_key(eff, Path(r["path"]).name), book_id),
            )
            self._reindex(book_id)
        return self.book_detail(book_id)

    def edit_series(self, series_id: int, changes: dict) -> dict:
        """Series-level edits: name, summary, authors, tags, and reader settings (rtl, mode, fit)."""
        if not isinstance(changes, dict):
            raise ValueError("changes must be an object")
        with self._lock:
            r = self._db.execute("SELECT user_json FROM series WHERE id = ?", (series_id,)).fetchone()
            if not r:
                raise KeyError(f"no series {series_id}")
            user = json.loads(r["user_json"])
            for key, value in changes.items():
                if value is None or value == "" or value == []:
                    user.pop(key, None)
                elif key in ("name", "summary"):
                    user[key] = str(value).strip()[:10000]
                elif key in ("authors", "tags"):
                    user[key] = M.clean_user_changes({key: value})[key]
                elif key == "rtl" or key in SERIES_SETTINGS:
                    user[key] = _check_reader_setting(key, value)
                else:
                    raise ValueError(f"unknown series field: {key}")
            self._db.execute("BEGIN")
            try:
                self._db.execute(
                    "UPDATE series SET user_json = ? WHERE id = ?", (json.dumps(user), series_id)
                )
                if "name" in changes:
                    for b in self._db.execute(
                        "SELECT id FROM books WHERE series_id = ?", (series_id,)
                    ).fetchall():
                        self._reindex(b["id"])
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return self.series_detail(series_id)

    def set_progress(
        self, book_id: int, page: int, finished: bool | None = None, pos: float = 0.0, user: int = 0
    ) -> dict:
        """Save where the reader is. ``page`` is a page, or a chapter in a text book, where ``pos``
        (0-1) is how far down that chapter."""
        r = self._one("SELECT pages, kind FROM books WHERE id = ?", (book_id,))
        if not r:
            raise KeyError(f"no book {book_id}")
        page = max(0, min(int(page), max(r["pages"] - 1, 0)))
        pos = max(0.0, min(float(pos), 1.0)) if pos == pos else 0.0  # NaN -> 0
        if finished is None:
            last = r["pages"] > 0 and page >= r["pages"] - 1
            finished = last and (r["kind"] != "epub-text" or pos >= 0.98)
        with self._lock:
            self._db.execute(
                """INSERT INTO progress(user_id, book_id, page, pos, finished, hidden, updated_at) VALUES (?,?,?,?,?,0,?)
                   ON CONFLICT(user_id, book_id) DO UPDATE SET page=excluded.page, pos=excluded.pos,
                   finished=excluded.finished, hidden=0, updated_at=excluded.updated_at""",
                (int(user), book_id, page, pos, int(bool(finished)), time.time()),
            )
        return {"page": page, "pos": pos, "finished": bool(finished)}

    def clear_progress(self, book_id: int, user: int = 0) -> None:
        with self._lock:
            self._db.execute("DELETE FROM progress WHERE user_id = ? AND book_id = ?", (int(user), book_id))

    # ------------------------------------------------------------ pages and covers
    def _book_file(self, book_id: int) -> sqlite3.Row:
        r = self._one("SELECT path, mtime, cover, missing, kind FROM books WHERE id = ?", (int(book_id),))
        if not r:
            raise KeyError(f"no book {book_id}")
        if r["missing"]:
            raise FileNotFoundError("the file for this book is missing")
        return r

    def _cached(self, cache: OrderedDict, key, size: int, make):
        """``cache[key]``, made with ``make()`` (outside the lock) if missing; keeps ``size`` entries."""
        with self._lock:
            if key in cache:
                cache.move_to_end(key)
                return cache[key]
        value = make()
        with self._lock:
            cache[key] = value
            while len(cache) > size:
                cache.popitem(last=False)
        return value

    def _pages(self, zf: zipfile.ZipFile, key: tuple[str, float]) -> list[str]:
        return self._cached(self._pages_cache, key, 64, lambda: page_names(zf))

    def _epub(self, r: sqlite3.Row) -> E.EpubBook:
        def parse() -> E.EpubBook:
            with zipfile.ZipFile(r["path"]) as zf:
                return E.parse(zf)

        return self._cached(self._epub_cache, (r["path"], r["mtime"]), 32, parse)

    def page(self, book_id: int, n: int) -> tuple[bytes, str]:
        """Page ``n`` (0-based) of a comic or picture EPUB: (bytes, content type)."""
        r = self._book_file(book_id)
        if r["kind"] != "cbz":
            if r["kind"] != "epub-image":
                raise ValueError("this is a text book: it has chapters, not pages")
            names = self._epub(r).images
            if not 0 <= int(n) < len(names):
                raise ValueError(f"page {n} out of range (0-{len(names) - 1})")
            with zipfile.ZipFile(r["path"]) as zf:
                return zf.read(names[int(n)]), RESOURCE_MIME.get(
                    PurePosixPath(names[int(n)]).suffix.lower(), "application/octet-stream"
                )
        with zipfile.ZipFile(r["path"]) as zf:
            names = self._pages(zf, (r["path"], r["mtime"]))
            if not 0 <= int(n) < len(names):
                raise ValueError(f"page {n} out of range (0-{len(names) - 1})")
            name = names[int(n)]
            return zf.read(name), MIME.get(PurePosixPath(name).suffix.lower(), "application/octet-stream")

    def thumbnail(self, book_id: int) -> bytes:
        """A small WebP cover, made on first request and cached by book id and file time."""
        r = self._book_file(book_id)
        out = self.cache_dir / f"{int(book_id)}-{int(r['mtime'])}.webp"
        if out.is_file():
            return out.read_bytes()
        buf = io.BytesIO()
        try:
            data = self._cover_bytes(book_id, r)
            with Image.open(io.BytesIO(data)) as im:
                im.thumbnail(THUMB_SIZE)
                im.convert("RGB").save(buf, "WEBP", quality=80, method=4)
        except (OSError, ValueError, LookupError, Image.DecompressionBombError):
            if r["kind"] == "cbz":
                raise
            buf = io.BytesIO(self._placeholder_cover(book_id))  # an EPUB without a usable picture
        # A cover made for an older version of the file is deleted when a scan sees the change.
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(buf.getvalue())
        os.replace(tmp, out)
        return buf.getvalue()

    def _cover_bytes(self, book_id: int, r: sqlite3.Row) -> bytes:
        if r["kind"] == "cbz":
            return self.page(book_id, r["cover"])[0]
        book = self._epub(r)
        if not book.cover:
            raise LookupError("no cover")
        with zipfile.ZipFile(r["path"]) as zf:
            if book.cover.lower().endswith(".svg"):
                raise LookupError("no cover (SVG)")
            return zf.read(book.cover)

    def _placeholder_cover(self, book_id: int) -> bytes:
        """A plain cover with the title, for books without a picture."""
        from PIL import ImageDraw, ImageFont

        m = json.loads(self._one("SELECT meta_json FROM books WHERE id = ?", (book_id,))["meta_json"])
        title = str(m.get("title") or "")
        hue = sum(title.encode()) % 360
        im = Image.new("RGB", THUMB_SIZE, f"hsl({hue}, 35%, 38%)")
        d = ImageDraw.Draw(im)
        try:
            font = ImageFont.load_default(size=28)
        except TypeError:  # Pillow < 10.1
            font = ImageFont.load_default()
        lines, line = [], ""
        for word in title.split():
            trial = f"{line} {word}".strip()
            if d.textlength(trial, font=font) <= THUMB_SIZE[0] - 40 or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
        y = 60
        for text in lines[:8]:
            d.text((20, y), text, fill="white", font=font)
            y += 36
        d.rectangle([0, THUMB_SIZE[1] - 14, THUMB_SIZE[0], THUMB_SIZE[1]], fill=f"hsl({hue}, 45%, 60%)")
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=80)
        return buf.getvalue()

    # ------------------------------------------------------------ text EPUBs
    def chapter(self, book_id: int, n: int, url_for) -> tuple[bytes, str]:
        """Chapter ``n`` of a text EPUB, with links rewritten by ``url_for(zip_name, fragment)``."""
        r = self._book_file(book_id)
        if r["kind"] != "epub-text":
            raise ValueError("only text books have chapters")
        book = self._epub(r)
        if not 0 <= int(n) < len(book.spine):
            raise ValueError(f"chapter {n} out of range (0-{len(book.spine) - 1})")
        name = book.spine[int(n)]
        with zipfile.ZipFile(r["path"]) as zf:
            info = zf.getinfo(name)
            if info.file_size > E.DOC_MAX:
                raise ValueError("this chapter is too large to show")
            text = E.rewrite_chapter(E.decode(zf.read(info)), name, url_for)
        xhtml = E.well_formed_xml(text)
        return text.encode("utf-8"), ("application/xhtml+xml" if xhtml else "text/html") + "; charset=utf-8"

    def resource(self, book_id: int, raw_name: str, url_for) -> tuple[bytes, str]:
        """A picture, stylesheet or font inside an EPUB, by its name in the archive.

        The name must be a file that is in this book's archive; nothing outside it can be named."""
        r = self._book_file(book_id)
        if r["kind"] == "cbz":
            raise ValueError("only EPUB books have resources")
        with zipfile.ZipFile(r["path"]) as zf:
            name = E.safe_name(raw_name, set(zf.namelist()))
            ctype = RESOURCE_MIME.get(PurePosixPath(name).suffix.lower())
            if ctype is None:
                raise ValueError("not a picture, stylesheet or font")
            info = zf.getinfo(name)
            if info.file_size > 64 * 1024 * 1024:
                raise ValueError("resource too large")
            data = zf.read(info)
        if name.lower().endswith(".css"):
            data = E.rewrite_css(E.decode(data), posixpath.dirname(name), url_for).encode("utf-8")
        return data, ctype


def _check_reader_setting(key: str, value):
    """A reader setting's value (``rtl``, ``mode``, ``fit``), checked; raises ValueError if bad."""
    if key == "rtl":
        if not isinstance(value, bool):
            raise ValueError("rtl must be true or false")
    elif value not in SERIES_SETTINGS[key]:
        raise ValueError(f"{key} must be one of {', '.join(SERIES_SETTINGS[key])}")
    return value


def _fts_query(q: str) -> str:
    """User text -> a safe FTS5 query: every word must match, as a prefix."""
    words = re.findall(r"\w+", q, re.UNICODE)
    if not words:
        raise ValueError("search needs at least one letter or number")
    return " ".join(f'"{w}"*' for w in words[:20])

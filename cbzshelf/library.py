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
import re
import sqlite3
import threading
import time
import zipfile
from collections import OrderedDict
from pathlib import Path, PurePosixPath

from cbzwebp.core import IMAGE_EXTS, is_junk, natural_key, normalize_path
from PIL import Image

from . import meta as M

ARCHIVE_EXTS = (".cbz", ".zip")
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
        self._pages_cache: OrderedDict[tuple[str, float], list[str]] = OrderedDict()
        # scanning
        self._scan_lock = threading.Lock()
        self._scan_cond = threading.Condition()
        self._scan_pending: list[int | None] = []
        self._scan_cancel = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._scan_looping = False  # set and cleared under _scan_cond, so scan() never misses an exiting loop
        self._stopping = False
        self.scan_state: dict = {"running": False, "root": None, "done": 0, "total": 0, "last": None}

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
            ids = [r["id"] for r in self._db.execute("SELECT id FROM books WHERE root_id = ?", (root_id,))]
            self._db.execute("BEGIN")
            for book_id in ids:
                self._db.execute("DELETE FROM book_search WHERE rowid = ?", (book_id,))
            self._db.execute("DELETE FROM books WHERE root_id = ?", (root_id,))
            self._db.execute("DELETE FROM series WHERE root_id = ?", (root_id,))
            n = self._db.execute("DELETE FROM roots WHERE id = ?", (root_id,)).rowcount
            self._db.execute("COMMIT")
        return bool(n)

    def root_for(self, path: Path) -> dict | None:
        for r in self.roots():
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
                roots = self.roots() if target is None else [r for r in self.roots() if r["id"] == target]
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
                files += [Path(dirpath, f) for f in filenames if f.lower().endswith(ARCHIVE_EXTS)]
        self.scan_state.update(total=len(files), done=0)
        known = {
            r["path"]: r
            for r in self._q(
                "SELECT id, path, size, mtime, missing FROM books WHERE root_id = ?", (root["id"],)
            )
        }
        counts = {"added": 0, "updated": 0, "missing": 0, "errors": 0}
        seen: set[str] = set()
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
            result = self._index_file(root, f, st)
            counts["added" if not old else "updated"] += 1
            if result == "error":
                counts["errors"] += 1
            self.scan_state["done"] += 1
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
        if not root or not f.is_file() or not f.name.lower().endswith(ARCHIVE_EXTS):
            return False
        self._index_file(root, f, f.stat())
        self._drop_empty_series()
        return True

    def _read_archive(self, f: Path) -> tuple[int, int, dict]:
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
        return len(pages), cover, file_meta

    def _index_file(self, root: dict, f: Path, st: os.stat_result) -> str:
        error = None
        try:
            pages, cover, ci = self._read_archive(f)
        except (zipfile.BadZipFile, OSError, RuntimeError) as e:
            pages, cover, ci, error = 0, 0, {}, f"cannot read: {e}"
        file_meta = {**M.parse_filename(f.stem), **ci}
        file_meta.setdefault("title", f.stem)
        series_id = self._series_for(root, f, ci.get("series"))
        with self._lock:
            row = self._db.execute("SELECT id, user_json FROM books WHERE path = ?", (str(f),)).fetchone()
            user = json.loads(row["user_json"]) if row else {}
            eff = M.effective(file_meta, user)
            values = (
                series_id,
                st.st_size,
                st.st_mtime,
                pages,
                cover,
                json.dumps(file_meta),
                json.dumps(eff),
                _sort_key(eff, f.name),
                error,
            )
            if row:
                book_id = row["id"]
                self._db.execute(
                    """UPDATE books SET series_id=?, size=?, mtime=?, pages=?, cover=?, file_json=?, meta_json=?,
                       sort_key=?, error=?, missing=0 WHERE id=?""",
                    (*values, book_id),
                )
            else:
                book_id = self._db.execute(
                    """INSERT INTO books(series_id, size, mtime, pages, cover, file_json, meta_json, sort_key, error,
                       root_id, path, added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*values, root["id"], str(f), time.time()),
                ).lastrowid
            self._reindex(book_id)
        return "error" if error else "ok"

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
            ids = [r["id"] for r in self._db.execute(f"SELECT id FROM books WHERE {where}", args)]
            self._db.execute("BEGIN")
            for book_id in ids:
                self._db.execute("DELETE FROM book_search WHERE rowid = ?", (book_id,))
                self._db.execute("DELETE FROM books WHERE id = ?", (book_id,))
            self._db.execute("COMMIT")
        self._drop_empty_series()
        return len(ids)

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
        }
        if "page" in r.keys():
            d["progress"] = (
                None
                if r["page"] is None
                else {"page": r["page"], "finished": bool(r["finished"]), "at": r["updated_at"]}
            )
        return d

    BOOK_COLS = "b.id, b.series_id, b.path, b.meta_json, b.pages, b.missing, b.error, b.mtime, p.page, p.finished, p.updated_at"
    BOOK_FROM = "books b LEFT JOIN progress p ON p.book_id = b.id"

    def series_list(
        self,
        q: str = "",
        tag: str = "",
        author: str = "",
        sort: str = "name",
        limit: int = 60,
        offset: int = 0,
    ) -> dict:
        where, args = ["1=1"], []
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
                   (SELECT CAST(b2.mtime AS INTEGER) FROM books b2 WHERE b2.series_id = s.id AND b2.missing = 0 ORDER BY b2.sort_key LIMIT 1) AS cover_v
            FROM series s JOIN books b ON b.series_id = s.id LEFT JOIN progress p ON p.book_id = b.id
            WHERE {" AND ".join(where)}
            GROUP BY s.id ORDER BY {order} LIMIT ? OFFSET ?"""
        total_sql = f"SELECT count(*) FROM series s WHERE {' AND '.join(where)}"
        items = [dict(r) for r in self._q(sql, (*args, int(limit), int(offset)))]
        total = self._one(total_sql, args)[0]
        return {"items": items, "total": total}

    def series_detail(self, series_id: int) -> dict:
        s = self._one("SELECT * FROM series WHERE id = ?", (series_id,))
        if not s:
            raise KeyError(f"no series {series_id}")
        user = json.loads(s["user_json"])
        books = [
            self._book_dict(r)
            for r in self._q(
                f"SELECT {self.BOOK_COLS} FROM {self.BOOK_FROM} WHERE b.series_id = ? ORDER BY b.sort_key",
                (series_id,),
            )
        ]
        rtl_votes = [
            json.loads(r["meta_json"]).get("rtl")
            for r in self._q("SELECT meta_json FROM books WHERE series_id = ?", (series_id,))
        ]
        file_rtl = any(v is True for v in rtl_votes)
        authors: list[str] = []
        tags: list[str] = []
        for b in books:
            authors += [a for a in b["authors"] if a not in authors]
            tags += [t for t in b["tags"] if t not in tags]
        return {
            "id": s["id"],
            "folder": s["folder"],
            "file_name": s["file_name"],
            "name": user.get("name") or s["file_name"],
            "summary": user.get("summary", ""),
            "user": user,
            "authors": user.get("authors") or authors,
            "tags": user.get("tags") or tags,
            "reader": {
                "rtl": user.get("rtl", file_rtl),
                "mode": user.get("mode", "single"),
                "fit": user.get("fit", "height"),
            },
            "books": books,
        }

    def book_detail(self, book_id: int) -> dict:
        r = self._one(
            f"SELECT {self.BOOK_COLS}, b.file_json, b.user_json FROM {self.BOOK_FROM} WHERE b.id = ?",
            (book_id,),
        )
        if not r:
            raise KeyError(f"no book {book_id}")
        d = self._book_dict(r)
        d.update(
            file_meta=json.loads(r["file_json"]),
            user_meta=json.loads(r["user_json"]),
            meta=json.loads(r["meta_json"]),
        )
        return d

    def search(self, q: str = "", tag: str = "", author: str = "", limit: int = 60, offset: int = 0) -> dict:
        where, args = ["1=1"], []
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
            (*args, int(limit), int(offset)),
        )
        total = self._one(f"SELECT count(*) FROM books b WHERE {w}", args)[0]
        return {"items": [self._book_dict(r) for r in rows], "total": total}

    def continue_reading(self, limit: int = 12) -> list[dict]:
        rows = self._q(
            f"""SELECT {self.BOOK_COLS} FROM {self.BOOK_FROM}
                WHERE p.finished = 0 AND b.missing = 0 ORDER BY p.updated_at DESC LIMIT ?""",
            (int(limit),),
        )
        return [self._book_dict(r) for r in rows]

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
                elif key == "rtl":
                    if not isinstance(value, bool):
                        raise ValueError("rtl must be true or false")
                    user[key] = value
                elif key in SERIES_SETTINGS:
                    if value not in SERIES_SETTINGS[key]:
                        raise ValueError(f"{key} must be one of {', '.join(SERIES_SETTINGS[key])}")
                    user[key] = value
                else:
                    raise ValueError(f"unknown series field: {key}")
            self._db.execute("UPDATE series SET user_json = ? WHERE id = ?", (json.dumps(user), series_id))
            if "name" in changes:
                for b in self._db.execute(
                    "SELECT id FROM books WHERE series_id = ?", (series_id,)
                ).fetchall():
                    self._reindex(b["id"])
        return self.series_detail(series_id)

    def set_progress(self, book_id: int, page: int, finished: bool | None = None) -> dict:
        r = self._one("SELECT pages FROM books WHERE id = ?", (book_id,))
        if not r:
            raise KeyError(f"no book {book_id}")
        page = max(0, min(int(page), max(r["pages"] - 1, 0)))
        if finished is None:
            finished = r["pages"] > 0 and page >= r["pages"] - 1
        with self._lock:
            self._db.execute(
                """INSERT INTO progress(book_id, page, finished, updated_at) VALUES (?,?,?,?)
                   ON CONFLICT(book_id) DO UPDATE SET page=excluded.page, finished=excluded.finished,
                   updated_at=excluded.updated_at""",
                (book_id, page, int(bool(finished)), time.time()),
            )
        return {"page": page, "finished": bool(finished)}

    def clear_progress(self, book_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM progress WHERE book_id = ?", (book_id,))

    # ------------------------------------------------------------ pages and covers
    def _book_file(self, book_id: int) -> sqlite3.Row:
        r = self._one("SELECT path, mtime, cover, missing FROM books WHERE id = ?", (int(book_id),))
        if not r:
            raise KeyError(f"no book {book_id}")
        if r["missing"]:
            raise FileNotFoundError("the file for this book is missing")
        return r

    def _pages(self, zf: zipfile.ZipFile, key: tuple[str, float]) -> list[str]:
        with self._lock:
            if key in self._pages_cache:
                self._pages_cache.move_to_end(key)
                return self._pages_cache[key]
        names = page_names(zf)
        with self._lock:
            self._pages_cache[key] = names
            while len(self._pages_cache) > 64:
                self._pages_cache.popitem(last=False)
        return names

    def page(self, book_id: int, n: int) -> tuple[bytes, str]:
        """Page ``n`` (0-based) of a book: (bytes, content type)."""
        r = self._book_file(book_id)
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
        data, _ = self.page(book_id, r["cover"])
        with Image.open(io.BytesIO(data)) as im:
            im.thumbnail(THUMB_SIZE)
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "WEBP", quality=80, method=4)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for old in self.cache_dir.glob(f"{int(book_id)}-*.webp"):
            old.unlink(missing_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(buf.getvalue())
        os.replace(tmp, out)
        return buf.getvalue()


def _fts_query(q: str) -> str:
    """User text -> a safe FTS5 query: every word must match, as a prefix."""
    words = re.findall(r"\w+", q, re.UNICODE)
    if not words:
        raise ValueError("search needs at least one letter or number")
    return " ".join(f'"{w}"*' for w in words[:20])

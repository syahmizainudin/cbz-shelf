"""The library's endpoints, mounted at ``/api/library/`` on cbz-webp's generic console server."""

from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from cbzwebp.web import FileBody, serve

from .library import BOOK_EXTS, Library
from .opds import BOOK_MIME, Catalog

UI_HTML = (Path(__file__).parent / "ui.html").read_bytes()
PREFIX = "/api/library/"
# Pages, chapters and EPUB files: their URLs carry the book's file time (&v=), and what they return
# comes from that file alone, so a changed file gets new URLs and a URL's answer never changes.
FILE_CACHE = "private, max-age=31536000, immutable"
# Covers stay at a day. The URL is keyed by the file time too, but a book without a picture gets a
# placeholder drawn from its title, and editing the title changes it without changing the URL.
IMAGE_CACHE = "private, max-age=86400"
MAX_LIMIT = 200
# EPUB chapters and their files are untrusted. They may load pictures, styles and fonts from this
# server and nothing else; no scripts, forms or plugins run, even if a chapter is opened on its own.
# "sandbox allow-same-origin" keeps the origin so the reader page can style and scroll the chapter.
EPUB_CSP = (
    "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
    "script-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'; "
    "sandbox allow-same-origin"
)


def _int(query: dict, key: str, default: int | None = None) -> int:
    if key not in query:
        if default is None:
            raise ValueError(f"missing {key}")
        return default
    return int(query[key])


def _json(obj, status: int = 200) -> tuple[int, dict, bytes]:
    return status, {"Content-Type": "application/json"}, json.dumps(obj).encode()


def _flag(query: dict, key: str) -> bool:
    return str(query.get(key, "")).lower() in ("1", "true", "yes")


# Permissions a host (harbor) can withhold through ``can``. Standalone, everything is allowed.
READ, EDIT, MANAGE = "library.read", "library.edit", "library.manage"
POST_NEEDS = {
    "edit-book": EDIT,
    "edit-series": EDIT,
    "add-root": MANAGE,
    "remove-root": MANAGE,
    "scan": MANAGE,
    "cancel-scan": MANAGE,
    "remove-missing": MANAGE,
    "add-library": MANAGE,
    "rename-library": MANAGE,
    "order-libraries": MANAGE,
    "remove-library": MANAGE,
    "move-root": MANAGE,
    "set-compilation": MANAGE,
}
GET_NEEDS = {"roots": MANAGE}
DENIED = {"error": "you don't have permission for this"}
# Actions that name one book or series, and where its id is: (kind, field). A book or series in a
# library the user can't see answers 404, as if it didn't exist.
GET_ITEM = {
    "series-detail": ("series", "id"),
    "book": ("book", "id"),
    "page": ("book", "book"),
    "thumb": ("book", "book"),
    "chapter": ("book", "book"),
    "res": ("book", "book"),
    "file": ("book", "book"),
}
POST_ITEM = {
    "edit-book": ("book", "id"),
    "edit-series": ("series", "id"),
    "progress": ("book", "id"),
    "clear-progress": ("book", "id"),
    "reader-settings": ("series", "id"),
    "hide-continue": ("book", "id"),
    "set-compilation": ("series", "id"),
}


class LibraryAPI:
    """The library's endpoints. ``user_of()`` names the user each request acts for: reading
    progress, Continue reading, favourites and reader settings are kept per user. Without accounts
    it is user 0. ``can(permission)`` says whether that user may read (``library.read``), edit
    details (``library.edit``) or manage folders and scans (``library.manage``).

    ``visible()`` gives the ids of the libraries the request's user may see, or None for all.
    Everything else (lists, counts, books and series by id, pages) is limited to those; a book or
    series outside them answers 404. Managing folders and libraries isn't limited by it."""

    def __init__(self, library: Library, user_of=None, can=None, visible=None):
        self.lib = library
        self.user_of = user_of or (lambda: 0)
        self.can = can or (lambda permission: True)
        self.visible = visible or (lambda: None)
        self.catalog = Catalog(library, PREFIX)

    def _visible(self):
        v = self.visible()
        return None if v is None else frozenset(int(i) for i in v)

    def _scope(self, visible, raw) -> frozenset | None:
        """The libraries a list covers: the visible ones, or just ``raw`` (a library id) if given."""
        if raw in (None, ""):
            return visible
        lid = int(raw)
        return frozenset({lid}) if visible is None or lid in visible else frozenset()

    def _hide_path(self, series: dict) -> dict:
        """A series' folder is a path on the server: only for those who manage the library."""
        if not self.can(MANAGE):
            series.pop("folder", None)
        return series

    def _check(self, table: dict, action: str, data: dict, visible) -> None:
        kind_field = table.get(action)
        if kind_field and visible is not None and kind_field[1] in data:
            self.lib.check_visible(kind_field[0], int(data[kind_field[1]]), visible)

    def state(self) -> dict:
        if not self.can(READ):
            return {}
        visible, manage = self._visible(), self.can(MANAGE)
        summary = self.lib.summary(visible)
        if not manage:  # the folder being scanned is a path; only managers see paths
            summary["scan"] = {k: v for k, v in summary["scan"].items() if k != "root"}
        return {
            "library": summary,
            "library_list": self.lib.libraries(visible),
            "library_can": {"edit": self.can(EDIT), "manage": manage},
            "library_prefs": self.lib.user_prefs(int(self.user_of())),
        }

    def get(self, action: str, query: dict) -> tuple[int, dict, bytes]:
        lib = self.lib
        limit = min(_int(query, "limit", 60), MAX_LIMIT)
        offset = max(_int(query, "offset", 0), 0)
        filters = {k: query.get(k, "") for k in ("q", "tag", "author")}
        if not self.can(GET_NEEDS.get(action, READ)):
            return _json(DENIED, 403)
        user = int(self.user_of())
        visible = self._visible()
        scope = self._scope(visible, query.get("library"))
        mine = {"favourite": _flag(query, "favourite"), "user": user, "libraries": scope}
        try:
            self._check(GET_ITEM, action, query, visible)
            if action == "series":
                found = lib.series_list(
                    **filters, **mine, sort=query.get("sort", "name"), limit=limit, offset=offset
                )
                for item in found["items"]:
                    self._hide_path(item)
                return _json(found)
            if action == "series-detail":
                return _json(self._hide_path(lib.series_detail(_int(query, "id"), user=user)))
            if action == "opds":
                body, ctype = self.catalog.answer(query, visible)
                return 200, {"Content-Type": ctype}, body
            if action == "file":
                path, kind = lib.book_file(_int(query, "book"))
                headers = {
                    "Content-Type": BOOK_MIME.get(kind, "application/zip"),
                    "Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name, safe='')}",
                    "Cache-Control": FILE_CACHE if query.get("v") else "no-store",
                }
                return 200, headers, FileBody(path)
            if action == "book":
                return _json(lib.book_detail(_int(query, "id"), user=user))
            if action == "search":
                return _json(lib.search(**filters, **mine, limit=limit, offset=offset))
            if action == "continue":
                return _json(lib.continue_reading(min(limit, 30), user=user, libraries=scope))
            if action == "facets":
                return _json(lib.facets(libraries=scope))
            if action == "libraries":
                return _json(lib.libraries(visible))
            if action == "roots":
                return _json(lib.roots())
            if action in ("chapter", "res"):
                book, v = _int(query, "book"), query.get("v", "")

                def url_for(name: str, frag: str) -> str:
                    url = f"{PREFIX}res?book={book}&path={quote(name, safe='/')}&v={quote(v)}"
                    return url + (f"#{quote(frag, safe='')}" if frag else "")

                if action == "chapter":
                    data, ctype = lib.chapter(book, _int(query, "n"), url_for)
                else:
                    data, ctype = lib.resource(book, query.get("path", ""), url_for)
                headers = {"Content-Type": ctype, "Content-Security-Policy": EPUB_CSP}
                headers["Cache-Control"] = FILE_CACHE if v else "no-store"
                return 200, headers, data
            if action == "page":
                data, ctype = lib.page(_int(query, "book"), _int(query, "n"))
                # Without &v= the URL isn't tied to the file's version.
                cache = FILE_CACHE if query.get("v") else IMAGE_CACHE
                return 200, {"Content-Type": ctype, "Cache-Control": cache}, data
            if action == "thumb":
                return (
                    200,
                    {"Content-Type": "image/webp", "Cache-Control": IMAGE_CACHE},
                    lib.thumbnail(_int(query, "book")),
                )
        except KeyError as e:
            if not str(e).startswith(("'no ", '"no ')):
                raise  # a missing parameter: the server answers 400
            return _json({"error": str(e).strip("'\"")}, 404)
        except FileNotFoundError as e:
            return _json({"error": str(e)}, 410)
        return _json({"error": "not found"}, 404)

    def post(self, action: str, body: dict) -> tuple[int, dict]:
        lib = self.lib
        if not self.can(POST_NEEDS.get(action, READ)):
            return 403, DENIED
        user = int(self.user_of())
        visible = self._visible()
        try:
            self._check(POST_ITEM, action, body, visible)
            if action == "add-root":
                return 200, lib.add_root(str(body.get("path", "")), body.get("library_id"))
            if action == "move-root":
                return 200, lib.move_root(int(body["id"]), int(body["library_id"]))
            if action == "add-library":
                return 200, lib.add_library(str(body.get("name", "")))
            if action == "rename-library":
                return 200, lib.rename_library(int(body["id"]), str(body.get("name", "")))
            if action == "order-libraries":
                return 200, {"libraries": lib.order_libraries(body.get("order"))}
            if action == "remove-library":
                return 200, {"ok": lib.remove_library(int(body["id"]))}
            if action == "remove-root":
                return 200, {"ok": lib.remove_root(int(body["id"]))}
            if action == "scan":
                lib.scan(int(body["root_id"]) if body.get("root_id") else None)
                return 200, {"ok": True}
            if action == "cancel-scan":
                lib.cancel_scan()
                return 200, {"ok": True}
            if action == "remove-missing":
                return 200, {
                    "removed": lib.remove_missing(int(body["root_id"]) if body.get("root_id") else None)
                }
            if action == "edit-book":
                return 200, lib.edit_book(int(body["id"]), body.get("changes") or {})
            if action == "edit-series":
                return 200, lib.edit_series(int(body["id"]), body.get("changes") or {})
            if action == "set-compilation":
                return 200, lib.set_compilation(int(body["id"]), body.get("on", True))
            if action == "progress":
                finished = body.get("finished")
                return 200, lib.set_progress(
                    int(body["id"]),
                    int(body.get("page", 0)),
                    None if finished is None else bool(finished),
                    float(body.get("pos") or 0),
                    user=user,
                )
            if action == "clear-progress":
                lib.clear_progress(int(body["id"]), user=user)
                return 200, {"ok": True}
            if action == "favourite":
                kind, on = str(body.get("kind", "")), body.get("on", True)
                if not isinstance(on, bool):
                    raise ValueError("on must be true or false")
                lib.check_visible(kind, int(body["id"]), visible)
                return 200, {"favourite": lib.set_favourite(kind, int(body["id"]), on, user=user)}
            if action == "prefs":
                return 200, lib.set_user_prefs(body.get("changes") or {}, user=user)
            if action == "reader-settings":
                return 200, lib.set_reader_prefs(int(body["id"]), body.get("changes") or {}, user=user)
            if action == "hide-continue":
                return 200, {"hidden": lib.hide_continue(int(body["id"]), user=user)}
            if action == "clear-continue":
                scope = self._scope(visible, body.get("library"))
                return 200, {"hidden": lib.hide_continue(None, user=user, libraries=scope)}
        except KeyError as e:
            if not str(e).startswith(("'no ", '"no ')):
                raise  # a missing field: the server answers 400
            return 404, {"error": str(e).strip("'\"")}
        return 404, {"error": "not found"}


def serve_shelf(port: int, library: Library) -> ThreadingHTTPServer:
    """This package's console: the library page at /."""
    return serve(port, UI_HTML, {PREFIX: LibraryAPI(library)}, BOOK_EXTS)

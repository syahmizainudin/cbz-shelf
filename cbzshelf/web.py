"""The library's endpoints, mounted at ``/api/library/`` on cbz-webp's generic console server."""

from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
from pathlib import Path

from cbzwebp.core import ARCHIVE_EXTS
from cbzwebp.web import serve

from .library import Library

UI_HTML = (Path(__file__).parent / "ui.html").read_bytes()
PREFIX = "/api/library/"
IMAGE_CACHE = "private, max-age=86400"  # URLs carry the file time (&v=), so a changed file gets a new URL
MAX_LIMIT = 200


def _int(query: dict, key: str, default: int | None = None) -> int:
    if key not in query:
        if default is None:
            raise ValueError(f"missing {key}")
        return default
    return int(query[key])


def _json(obj, status: int = 200) -> tuple[int, dict, bytes]:
    return status, {"Content-Type": "application/json"}, json.dumps(obj).encode()


class LibraryAPI:
    def __init__(self, library: Library):
        self.lib = library

    def state(self) -> dict:
        return {"library": self.lib.summary()}

    def get(self, action: str, query: dict) -> tuple[int, dict, bytes]:
        lib = self.lib
        limit = min(_int(query, "limit", 60), MAX_LIMIT)
        offset = max(_int(query, "offset", 0), 0)
        filters = {k: query.get(k, "") for k in ("q", "tag", "author")}
        try:
            if action == "series":
                return _json(
                    lib.series_list(**filters, sort=query.get("sort", "name"), limit=limit, offset=offset)
                )
            if action == "series-detail":
                return _json(lib.series_detail(_int(query, "id")))
            if action == "book":
                return _json(lib.book_detail(_int(query, "id")))
            if action == "search":
                return _json(lib.search(**filters, limit=limit, offset=offset))
            if action == "continue":
                return _json(lib.continue_reading(min(limit, 30)))
            if action == "facets":
                return _json(lib.facets())
            if action == "roots":
                return _json(lib.roots())
            if action == "page":
                data, ctype = lib.page(_int(query, "book"), _int(query, "n"))
                return 200, {"Content-Type": ctype, "Cache-Control": IMAGE_CACHE}, data
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
        try:
            if action == "add-root":
                return 200, lib.add_root(str(body.get("path", "")))
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
            if action == "progress":
                finished = body.get("finished")
                return 200, lib.set_progress(
                    int(body["id"]), int(body.get("page", 0)), None if finished is None else bool(finished)
                )
            if action == "clear-progress":
                lib.clear_progress(int(body["id"]))
                return 200, {"ok": True}
        except KeyError as e:
            if not str(e).startswith(("'no ", '"no ')):
                raise  # a missing field: the server answers 400
            return 404, {"error": str(e).strip("'\"")}
        return 404, {"error": "not found"}


def serve_shelf(port: int, library: Library) -> ThreadingHTTPServer:
    """This package's console: the library page at /."""
    return serve(port, UI_HTML, {PREFIX: LibraryAPI(library)}, ARCHIVE_EXTS)

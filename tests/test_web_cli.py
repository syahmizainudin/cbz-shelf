"""Tests for the library's HTTP endpoints and the cbz-shelf command."""

import contextlib
import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

from helpers import comicinfo, make_cbz, make_epub

from cbzshelf import __version__, cli
from cbzshelf.library import Library
from cbzshelf.web import LibraryAPI, serve_shelf


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.root = self.dir / "Comics"
        make_cbz(
            self.root / "Blue" / "Blue v01.cbz", n_pages=4, info=comicinfo(Writer="A. Writer", Tags="sea")
        )
        make_cbz(self.root / "Blue" / "Blue v02.cbz", n_pages=4)
        self.db = self.dir / "lib.db"

    def tearDown(self):
        self.tmp.cleanup()


class WebTests(Base):
    def setUp(self):
        super().setUp()
        self.lib = Library(self.db, self.dir / "thumbs")
        self.server = serve_shelf(0, self.lib)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.post("add-root", {"path": str(self.root)})
        self.lib.wait_scan(30)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.lib.close()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Content-Type": "application/json"} if body is not None else {}
        hdrs.update(headers or {})
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=hdrs)
        resp = conn.getresponse()
        raw, h = resp.read(), dict(resp.getheaders())
        conn.close()
        ctype = h.get("Content-Type", "")
        return resp.status, h, json.loads(raw) if ctype.startswith("application/json") else raw

    def get(self, action, **params):
        q = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        return self.request("GET", f"/api/library/{action}?{q}")

    def post(self, action, body, **kw):
        return self.request("POST", f"/api/library/{action}", body, **kw)

    def book_ids(self):
        sid = self.get("series")[2]["items"][0]["id"]
        return [b["id"] for b in self.get("series-detail", id=sid)[2]["books"]]

    def test_page_and_state(self):
        status, h, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Shelf</title>", page)
        self.assertEqual(h["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn(b'type: "shelf-reader"', page)  # hosts such as harbor listen for it
        _, _, st = self.request("GET", "/api/state")
        self.assertEqual((st["library"]["books"], st["library"]["series"]), (2, 1))

    def test_browse_and_search(self):
        _, _, series = self.get("series")
        self.assertEqual(
            (series["total"], series["items"][0]["name"], series["items"][0]["books"]), (1, "Blue", 2)
        )
        ids = self.book_ids()
        _, _, book = self.get("book", id=ids[0])
        self.assertEqual(
            (book["title"], book["meta"]["authors"], book["pages"]), ("Blue v01", ["A. Writer"], 4)
        )
        self.assertEqual([b["title"] for b in self.get("search", q="writer")[2]["items"]], ["Blue v01"])
        self.assertEqual(self.get("facets")[2]["tags"], [{"value": "sea", "n": 1}])
        self.assertEqual(len(self.get("roots")[2]), 1)

    def test_images(self):
        bid = self.book_ids()[0]
        status, h, data = self.get("page", book=bid, n=1, v=1)
        self.assertEqual(
            (status, h["Content-Type"], h["Cache-Control"]),
            (200, "image/webp", "private, max-age=31536000, immutable"),
        )
        self.assertTrue(data.startswith(b"RIFF"))
        self.assertEqual(self.get("page", book=bid, n=1)[1]["Cache-Control"], "private, max-age=86400")
        status, h, data = self.get("thumb", book=bid, v=1)
        self.assertEqual(
            (status, h["Content-Type"], h["Cache-Control"]), (200, "image/webp", "private, max-age=86400")
        )

    def test_errors(self):
        self.assertEqual(self.get("book", id=999)[0], 404)
        self.assertEqual(self.get("book")[0], 400)  # missing id
        self.assertEqual(self.get("page", book=self.book_ids()[0], n=99)[0], 400)
        self.assertEqual(self.get("search", q="!!")[0], 400)
        self.assertEqual(self.get("nope")[0], 404)
        self.assertEqual(self.get("page", book="../../etc/passwd", n=0)[0], 400)  # ids only, never paths
        (self.root / "Blue" / "Blue v02.cbz").unlink()
        self.post("scan", {})
        self.lib.wait_scan(30)
        self.assertEqual(self.get("page", book=self.book_ids()[1], n=0)[0], 410)

    def test_edits_progress_and_settings(self):
        b1, _ = self.book_ids()
        status, _, d = self.post("edit-book", {"id": b1, "changes": {"title": "Renamed", "tags": "x, y"}})
        self.assertEqual((status, d["title"], d["meta"]["tags"]), (200, "Renamed", ["x", "y"]))
        self.assertEqual(self.post("edit-book", {"id": b1, "changes": {"year": "soon"}})[0], 400)
        self.assertEqual(
            self.post("progress", {"id": b1, "page": 2})[2], {"page": 2, "pos": 0.0, "finished": False}
        )
        self.assertEqual([b["id"] for b in self.get("continue")[2]], [b1])
        sid = self.get("series")[2]["items"][0]["id"]
        _, _, s = self.post("edit-series", {"id": sid, "changes": {"mode": "vertical", "rtl": True}})
        self.assertEqual(s["reader"], {"rtl": True, "mode": "vertical", "fit": "height"})
        self.assertEqual(self.post("edit-series", {"id": 999, "changes": {}})[0], 404)
        self.assertEqual(self.post("clear-progress", {"id": b1})[2], {"ok": True})

    def test_favourites_and_continue_endpoints(self):
        b1, b2 = self.book_ids()
        sid = self.get("series")[2]["items"][0]["id"]
        self.assertEqual(self.post("favourite", {"kind": "series", "id": sid})[2], {"favourite": True})
        self.assertEqual(
            self.post("favourite", {"kind": "book", "id": b2, "on": True})[2], {"favourite": True}
        )
        self.assertEqual(self.get("series", favourite=1)[2]["total"], 1)
        self.assertEqual([b["id"] for b in self.get("search", favourite=1)[2]["items"]], [b2])
        self.assertEqual(self.post("favourite", {"kind": "book", "id": b2, "on": "no"})[0], 400)
        self.assertEqual(self.post("favourite", {"kind": "book", "id": 999})[0], 404)
        self.post("progress", {"id": b1, "page": 1})
        self.assertEqual(self.post("hide-continue", {"id": b1})[2], {"hidden": 1})
        self.assertEqual(self.get("continue")[2], [])
        self.post("progress", {"id": b2, "page": 1})
        self.assertEqual(self.post("clear-continue", {})[2], {"hidden": 1})

    def test_roots_endpoints(self):
        other = self.dir / "Other"
        make_cbz(other / "x.cbz")
        _, _, r = self.post("add-root", {"path": str(other)})
        self.lib.wait_scan(30)
        self.assertEqual(self.request("GET", "/api/state")[2]["library"]["roots"], 2)
        self.assertEqual(self.post("add-root", {"path": str(other)})[0], 400)
        self.assertEqual(self.post("remove-root", {"id": r["id"]})[2], {"ok": True})
        self.assertEqual(self.post("remove-missing", {})[2], {"removed": 0})

    def test_epub_chapters_and_resources(self):
        make_epub(self.root / "Novel" / "Novel.epub", title="Novel")
        self.post("scan", {})
        self.lib.wait_scan(30)
        bid = self.get("search", q="novel")[2]["items"][0]["id"]
        _, _, book = self.get("book", id=bid)
        self.assertEqual((book["kind"], len(book["toc"])), ("epub-text", 3))
        status, h, body = self.get("chapter", book=bid, n=0, v=book["v"])
        self.assertEqual((status, h["Content-Type"]), (200, "application/xhtml+xml; charset=utf-8"))
        self.assertEqual(h["Cache-Control"], "private, max-age=31536000, immutable")
        self.assertEqual(self.get("chapter", book=bid, n=0)[1]["Cache-Control"], "no-store")
        csp = h["Content-Security-Policy"]
        for part in (
            "default-src 'none'",
            "script-src 'none'",
            "sandbox allow-same-origin",
            "form-action 'none'",
        ):
            self.assertIn(part, csp)
        self.assertIn(f"/api/library/res?book={bid}&amp;path=OEBPS/Styles/book.css".encode(), body)
        status, h, _ = self.get("res", book=bid, path="OEBPS/Images/bg.png", v=1)
        self.assertEqual((status, h["Content-Type"]), (200, "image/png"))
        self.assertEqual(h["Cache-Control"], "private, max-age=31536000, immutable")
        self.assertIn("sandbox", h["Content-Security-Policy"])
        self.assertEqual(self.get("res", book=bid, path="../../etc/passwd")[0], 400)
        self.assertEqual(self.get("res", book=bid, path="OEBPS/missing.png")[0], 404)
        self.assertEqual(self.get("chapter", book=self.book_ids()[0], n=0)[0], 400)  # a comic has no chapters
        _, _, p = self.post("progress", {"id": bid, "page": 1, "pos": 0.25})
        self.assertEqual(p, {"page": 1, "pos": 0.25, "finished": False})
        _, _, r = self.request("GET", "/api/dirs?path=" + quote(str(self.root / "Novel")))
        self.assertEqual(r["files"], 1)  # the folder browser counts .epub files

    def test_dirs_count_archives(self):
        _, _, r = self.request("GET", "/api/dirs?path=" + quote(str(self.root / "Blue")))
        self.assertEqual(r["files"], 2)

    def test_guards(self):
        self.assertEqual(
            self.post("edit-book", {"id": 1, "changes": {}}, headers={"Origin": "http://evil.example"})[0],
            403,
        )
        self.assertEqual(self.request("GET", "/api/library/series", headers={"Host": "evil.example"})[0], 403)


class PermissionTests(Base):
    """A host (harbor) passes ``can``; what it withholds is refused with 403 and hidden in state."""

    def setUp(self):
        super().setUp()
        self.lib = Library(self.db, self.dir / "thumbs")
        self.lib.add_root(str(self.root))
        self.lib.wait_scan(30)
        self.perms = {"library.read"}
        self.api = LibraryAPI(self.lib, user_of=lambda: 3, can=lambda p: p in self.perms)

    def tearDown(self):
        self.lib.close()
        super().tearDown()

    def test_reader_only(self):
        st = self.api.state()
        self.assertEqual(st["library_can"], {"edit": False, "manage": False})
        self.assertEqual(self.api.get("series", {})[0], 200)
        sid = json.loads(self.api.get("series", {})[2])["items"][0]["id"]
        self.assertEqual(self.api.post("reader-settings", {"id": sid, "changes": {"fit": "width"}})[0], 200)
        for action, body in (
            ("edit-series", {"id": sid, "changes": {"name": "X"}}),
            ("scan", {}),
            ("add-root", {"path": "/"}),
        ):
            self.assertEqual(
                self.api.post(action, body), (403, {"error": "you don't have permission for this"})
            )
        self.assertEqual(self.api.get("roots", {})[0], 403)
        self.perms = set()
        self.assertEqual(self.api.state(), {})
        self.assertEqual(self.api.get("series", {})[0], 403)
        self.perms = {"library.read"}
        self.assertEqual(
            self.api.post("prefs", {"changes": {"text_width": "56"}}), (200, {"text_width": "56"})
        )
        self.assertEqual(self.api.state()["library_prefs"], {"text_width": "56"})
        self.perms = {"library.read", "library.edit", "library.manage"}
        self.assertEqual(self.api.post("edit-series", {"id": sid, "changes": {"name": "X"}})[0], 200)


class LibraryTabTests(Base):
    """Named libraries (tabs), and ``visible``: a book or series in a library the user can't see is
    left out of every list and count, and answers 404 by id."""

    def setUp(self):
        super().setUp()
        self.other = self.dir / "Novels"
        make_cbz(self.other / "Red" / "Red 1.cbz", n_pages=3, info=comicinfo(Writer="B. Writer", Tags="fire"))
        self.lib = Library(self.db, self.dir / "thumbs")
        self.lib.add_root(str(self.root))
        self.novels = self.lib.add_library("Novels")["id"]
        self.lib.add_root(str(self.other), self.novels)
        self.lib.wait_scan(30)
        self.visible = None
        self.perms = {"library.read", "library.edit", "library.manage"}
        self.api = LibraryAPI(
            self.lib, user_of=lambda: 3, can=lambda p: p in self.perms, visible=lambda: self.visible
        )
        self.red = self.lib.search(q="Red")["items"][0]
        self.blue = self.lib.search(q="Blue")["items"][0]

    def tearDown(self):
        self.lib.close()
        super().tearDown()

    def get(self, action, **query):
        status, _, body = self.api.get(action, {k: str(v) for k, v in query.items()})
        return status, json.loads(body) if body[:1] in (b"{", b"[") else body

    def test_manage_libraries(self):
        lib = self.lib
        self.assertEqual([x["name"] for x in lib.libraries()], ["Library", "Novels"])
        with self.assertRaisesRegex(ValueError, "already a library"):
            lib.add_library(" novels ")
        with self.assertRaisesRegex(ValueError, "name"):
            lib.add_library("  ")
        comics = lib.add_library("Comics")["id"]
        lib.order_libraries([comics, self.novels, 1])
        self.assertEqual([x["name"] for x in lib.libraries()], ["Comics", "Novels", "Library"])
        with self.assertRaisesRegex(ValueError, "every library"):
            lib.order_libraries([comics, 1])
        lib.rename_library(comics, "Manga")
        with self.assertRaisesRegex(ValueError, "folders first"):
            lib.remove_library(self.novels)  # it has a folder: its books would be lost
        root = next(r for r in lib.roots() if r["library_id"] == self.novels)
        lib.move_root(root["id"], comics)
        self.assertEqual(self.get("series", library=comics)[1]["total"], 1)  # moved with its books
        self.assertTrue(lib.remove_library(self.novels))  # empty now
        with self.assertRaisesRegex(KeyError, "no library"):
            lib.add_root(str(self.dir / "elsewhere"), 999)
        with self.assertRaisesRegex(KeyError, "no library"):
            lib.move_root(root["id"], 999)
        with self.assertRaisesRegex(KeyError, "no folder"):
            lib.move_root(999, 1)
        lib.move_root(root["id"], 1)
        lib.remove_library(comics)
        with self.assertRaisesRegex(ValueError, "last library"):
            lib.remove_library(1)
        self.assertEqual(len(self.lib.search()["items"]), 3)  # no book was lost on the way

    def test_library_tab_limits_lists(self):
        status, body = self.get("series", library=self.novels)
        self.assertEqual((status, [s["name"] for s in body["items"]]), (200, ["Red"]))
        self.assertEqual(
            [b["title"] for b in self.get("search", library=1)[1]["items"]], ["Blue v01", "Blue v02"]
        )
        self.assertEqual([t["value"] for t in self.get("facets", library=self.novels)[1]["tags"]], ["fire"])
        self.api.post("progress", {"id": self.red["id"], "page": 1})
        self.api.post("progress", {"id": self.blue["id"], "page": 1})
        self.assertEqual([b["title"] for b in self.get("continue", library=self.novels)[1]], ["Red 1"])
        self.assertEqual(self.api.post("clear-continue", {"library": self.novels}), (200, {"hidden": 1}))
        self.assertEqual([b["id"] for b in self.get("continue")[1]], [self.blue["id"]])
        self.assertEqual(self.get("series")[1]["total"], 2)  # All

    def test_hidden_library_is_invisible(self):
        self.visible = {1}  # the user may see "Library" only
        self.perms = {"library.read"}
        st = self.api.state()
        self.assertEqual(st["library_list"], [{"id": 1, "name": "Library", "books": 2, "roots": 1}])
        self.assertEqual((st["library"]["books"], st["library"]["series"], st["library"]["roots"]), (2, 1, 1))
        self.assertNotIn("root", st["library"]["scan"])  # a path; managers only
        self.assertEqual([x["id"] for x in self.get("libraries")[1]], [1])
        self.assertEqual(self.get("series")[1]["total"], 1)
        self.assertEqual(self.get("series", library=self.novels)[1]["total"], 0)
        self.assertEqual(self.get("search", q="Red")[1]["total"], 0)
        self.assertNotIn("fire", [t["value"] for t in self.get("facets")[1]["tags"]])
        rid, sid = self.red["id"], self.red["series_id"]
        for action, query in (
            ("book", {"id": rid}),
            ("series-detail", {"id": sid}),
            ("page", {"book": rid, "n": 0}),
            ("thumb", {"book": rid}),
            ("chapter", {"book": rid, "n": 0}),
            ("res", {"book": rid, "path": "x.css"}),
        ):
            self.assertEqual(self.get(action, **query)[0], 404, action)
        self.perms = {"library.read", "library.edit"}
        for action, body in (
            ("progress", {"id": rid, "page": 1}),
            ("clear-progress", {"id": rid}),
            ("hide-continue", {"id": rid}),
            ("reader-settings", {"id": sid, "changes": {"fit": "width"}}),
            ("favourite", {"kind": "book", "id": rid}),
            ("favourite", {"kind": "series", "id": sid}),
            ("edit-book", {"id": rid, "changes": {"title": "X"}}),
            ("edit-series", {"id": sid, "changes": {"name": "X"}}),
        ):
            series = action in ("reader-settings", "edit-series") or body.get("kind") == "series"
            expected = f"no series {sid}" if series else f"no book {rid}"
            self.assertEqual(self.api.post(action, body), (404, {"error": expected}), action)
        self.assertEqual(self.lib.book_detail(rid, user=3)["progress"], None)  # nothing was saved
        self.assertEqual(self.lib.book_detail(rid)["title"], "Red 1")
        # the visible library still works
        self.assertEqual(self.get("book", id=self.blue["id"])[0], 200)
        self.assertEqual(self.api.post("progress", {"id": self.blue["id"], "page": 1})[0], 200)
        self.visible = set()
        self.assertEqual(self.get("series")[1]["total"], 0)
        self.assertEqual(self.api.state()["library_list"], [])

    def test_library_actions_need_manage(self):
        self.perms = {"library.read", "library.edit"}
        for action, body in (
            ("add-library", {"name": "X"}),
            ("rename-library", {"id": 1, "name": "X"}),
            ("order-libraries", {"order": [1]}),
            ("remove-library", {"id": self.novels}),
            ("move-root", {"id": 1, "library_id": self.novels}),
        ):
            self.assertEqual(self.api.post(action, body)[0], 403, action)
        self.perms.add("library.manage")
        self.assertEqual(self.api.post("add-library", {"name": "X"})[1]["name"], "X")
        self.assertEqual(self.api.post("remove-library", {"id": 999})[0], 404)


class CliTests(Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--db", str(self.db), *argv])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_add_roots_search_scan(self):
        code, out, _ = self.run_cli("add", str(self.root))
        self.assertEqual(code, 0, out)
        self.assertIn("2 added", out)
        code, out, _ = self.run_cli("roots")
        self.assertIn("(Library: 2 books)", out)
        code, out, _ = self.run_cli("search", "writer")
        self.assertIn("Blue v01", out)
        self.assertIn("1 match(es)", out)
        make_cbz(self.root / "Blue" / "Blue v03.cbz")
        code, out, _ = self.run_cli("scan", str(self.root))
        self.assertIn("1 added", out)
        self.assertEqual(self.run_cli("scan", str(self.dir))[0], 2)  # not a library folder
        self.assertEqual(self.run_cli("add", str(self.dir / "missing"))[0], 2)

    def test_version(self):
        code, out, _ = self.run_cli("--version")
        self.assertEqual(code, 0)
        self.assertIn(__version__, out)


if __name__ == "__main__":
    unittest.main()

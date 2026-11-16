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

from helpers import comicinfo, make_cbz

from cbzshelf import __version__, cli
from cbzshelf.library import Library
from cbzshelf.web import serve_shelf


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
            (status, h["Content-Type"], h["Cache-Control"]), (200, "image/webp", "private, max-age=86400")
        )
        self.assertTrue(data.startswith(b"RIFF"))
        status, h, data = self.get("thumb", book=bid)
        self.assertEqual((status, h["Content-Type"]), (200, "image/webp"))

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
        self.assertEqual(self.post("progress", {"id": b1, "page": 2})[2], {"page": 2, "finished": False})
        self.assertEqual([b["id"] for b in self.get("continue")[2]], [b1])
        sid = self.get("series")[2]["items"][0]["id"]
        _, _, s = self.post("edit-series", {"id": sid, "changes": {"mode": "vertical", "rtl": True}})
        self.assertEqual(s["reader"], {"rtl": True, "mode": "vertical", "fit": "height"})
        self.assertEqual(self.post("edit-series", {"id": 999, "changes": {}})[0], 404)
        self.assertEqual(self.post("clear-progress", {"id": b1})[2], {"ok": True})

    def test_roots_endpoints(self):
        other = self.dir / "Other"
        make_cbz(other / "x.cbz")
        _, _, r = self.post("add-root", {"path": str(other)})
        self.lib.wait_scan(30)
        self.assertEqual(self.request("GET", "/api/state")[2]["library"]["roots"], 2)
        self.assertEqual(self.post("add-root", {"path": str(other)})[0], 400)
        self.assertEqual(self.post("remove-root", {"id": r["id"]})[2], {"ok": True})
        self.assertEqual(self.post("remove-missing", {})[2], {"removed": 0})

    def test_dirs_count_archives(self):
        _, _, r = self.request("GET", "/api/dirs?path=" + quote(str(self.root / "Blue")))
        self.assertEqual(r["files"], 2)

    def test_guards(self):
        self.assertEqual(
            self.post("edit-book", {"id": 1, "changes": {}}, headers={"Origin": "http://evil.example"})[0],
            403,
        )
        self.assertEqual(self.request("GET", "/api/library/series", headers={"Host": "evil.example"})[0], 403)


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
        self.assertIn("(2 books)", out)
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

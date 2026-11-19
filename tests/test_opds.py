"""Tests for the OPDS catalog, whole-book downloads, and folder paths kept from readers."""

import http.client
import json
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from helpers import comicinfo, make_cbz, make_epub

from cbzshelf import opds
from cbzshelf.library import Library
from cbzshelf.web import LibraryAPI, serve_shelf

A = "{http://www.w3.org/2005/Atom}"
PSE = "{http://vaemendis.net/opds-pse/ns}"


def links(el, rel=None):
    return [x for x in el.findall(f"{A}link") if rel is None or x.get("rel") == rel]


class OpdsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        make_cbz(
            self.dir / "Comics" / "Blue & Co" / "Blue 1.cbz",
            n_pages=4,
            info=comicinfo(Writer="A &lt;Writer&gt;"),
        )
        make_cbz(self.dir / "Comics" / "Blue & Co" / "Blue 2.cbz", n_pages=3)
        make_epub(self.dir / "Comics" / "Prose" / "Novel.epub", kind="text", chapters=2, title="Novel")
        make_epub(self.dir / "Comics" / "Pics" / "Picture.epub", kind="images", chapters=3, title="Picture")
        make_cbz(self.dir / "Private" / "Red" / "Red 1.cbz", n_pages=2)
        self.lib = Library(self.dir / "lib.db", self.dir / "thumbs")
        self.lib.add_root(str(self.dir / "Comics"))
        self.priv = self.lib.add_library("Private")["id"]
        self.lib.add_root(str(self.dir / "Private"), self.priv)
        self.lib.wait_scan(30)
        self.visible = None
        self.perms = {"library.read"}
        self.api = LibraryAPI(self.lib, can=lambda p: p in self.perms, visible=lambda: self.visible)

    def tearDown(self):
        self.lib.close()
        self.tmp.cleanup()

    def feed(self, **query):
        status, headers, body = self.api.get("opds", {k: str(v) for k, v in query.items()})
        self.assertEqual(status, 200, body)
        self.assertIn("profile=opds-catalog", headers["Content-Type"])
        return ET.fromstring(body)

    def series_id(self, name):
        return next(s["id"] for s in self.lib.series_list()["items"] if s["name"] == name)

    def test_root_lists_all_and_each_library(self):
        root = self.feed()
        titles = [e.find(f"{A}title").text for e in root.findall(f"{A}entry")]
        self.assertEqual(titles, ["All series", "Library", "Private"])
        self.assertEqual(links(root, "search")[0].get("href"), "/api/library/opds?feed=opensearch")
        self.visible = {1}
        self.assertEqual([e.find(f"{A}title").text for e in self.feed().findall(f"{A}entry")], ["All series"])

    def test_series_feed_and_paging(self):
        names = [e.find(f"{A}title").text for e in self.feed(feed="series").findall(f"{A}entry")]
        self.assertEqual(names, ["Blue & Co", "Pics", "Prose", "Red"])
        with mock.patch.object(opds, "PAGE", 2):
            first = self.feed(feed="series")
            self.assertEqual(len(first.findall(f"{A}entry")), 2)
            nxt = links(first, "next")[0].get("href")
            self.assertEqual(nxt, "/api/library/opds?feed=series&offset=2")
            second = self.feed(feed="series", offset=2)
            self.assertEqual(
                [e.find(f"{A}title").text for e in second.findall(f"{A}entry")], ["Prose", "Red"]
            )
            self.assertEqual(links(second, "next"), [])
        only = self.feed(feed="series", library=self.priv)
        self.assertEqual([e.find(f"{A}title").text for e in only.findall(f"{A}entry")], ["Red"])
        self.assertEqual(only.find(f"{A}title").text, "Private")

    def test_books_feed_links(self):
        feed = self.feed(feed="books", series=self.series_id("Blue & Co"))
        entries = feed.findall(f"{A}entry")
        self.assertEqual([e.find(f"{A}title").text for e in entries], ["Blue 1", "Blue 2"])
        first = entries[0]
        self.assertEqual(first.find(f"{A}author/{A}name").text, "A <Writer>")
        acq = links(first, "http://opds-spec.org/acquisition")[0]
        self.assertEqual(acq.get("type"), "application/vnd.comicbook+zip")
        self.assertTrue(acq.get("href").startswith("/api/library/file?book="))
        stream = links(first, "http://vaemendis.net/opds-pse/stream")[0]
        self.assertEqual(stream.get(f"{PSE}count"), "4")
        self.assertIn("&n={pageNumber}", stream.get("href"))
        self.assertTrue(links(first, "http://opds-spec.org/image/thumbnail"))
        prose = self.feed(feed="books", series=self.series_id("Prose")).find(f"{A}entry")
        self.assertEqual(
            links(prose, "http://opds-spec.org/acquisition")[0].get("type"), "application/epub+zip"
        )
        self.assertEqual(links(prose, "http://vaemendis.net/opds-pse/stream"), [])  # text: no pages to stream
        pics = self.feed(feed="books", series=self.series_id("Pics")).find(f"{A}entry")
        self.assertEqual(links(pics, "http://vaemendis.net/opds-pse/stream")[0].get(f"{PSE}count"), "3")

    def test_search_and_opensearch(self):
        found = self.feed(feed="search", q="blue")
        self.assertEqual(len(found.findall(f"{A}entry")), 2)
        _, headers, body = self.api.get("opds", {"feed": "opensearch"})
        self.assertEqual(headers["Content-Type"], "application/opensearchdescription+xml")
        self.assertIn(b"feed=search&amp;q={searchTerms}", body)

    def test_hidden_library_stays_hidden(self):
        self.visible = {1}
        red = self.series_id("Red")
        self.assertEqual(self.api.get("opds", {"feed": "books", "series": str(red)})[0], 404)
        self.assertEqual(self.api.get("opds", {"feed": "series", "library": str(self.priv)})[0], 404)
        self.assertEqual(len(self.feed(feed="search", q="red").findall(f"{A}entry")), 0)
        names = [e.find(f"{A}title").text for e in self.feed(feed="series").findall(f"{A}entry")]
        self.assertNotIn("Red", names)
        book = self.lib.search(q="red")["items"][0]["id"]
        self.assertEqual(self.api.get("file", {"book": str(book)})[0], 404)

    def test_bad_requests(self):
        self.assertEqual(self.api.get("opds", {"feed": "nope"})[0], 404)
        with self.assertRaises(KeyError):  # the server answers 400
            self.api.get("opds", {"feed": "books"})

    def test_folder_paths_only_for_managers(self):
        sid = self.series_id("Red")
        detail = json.loads(self.api.get("series-detail", {"id": str(sid)})[2])
        listed = json.loads(self.api.get("series", {})[2])["items"]
        self.assertNotIn("folder", detail)
        self.assertTrue(all("folder" not in s for s in listed))
        self.perms.add("library.manage")
        self.assertEqual(
            json.loads(self.api.get("series-detail", {"id": str(sid)})[2])["folder"],
            str(self.dir / "Private" / "Red"),
        )


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.book = self.dir / "Comics" / "S" / "Big one.cbz"
        make_cbz(self.book, n_pages=6)
        self.lib = Library(self.dir / "lib.db", self.dir / "thumbs")
        self.lib.add_root(str(self.dir / "Comics"))
        self.lib.wait_scan(30)
        self.server = serve_shelf(0, self.lib)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.bid = self.lib.search()["items"][0]["id"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.lib.close()
        self.tmp.cleanup()

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", **(headers or {})})
        r = conn.getresponse()
        body, h = r.read(), dict(r.getheaders())
        conn.close()
        return r.status, h, body

    def test_whole_book_and_a_range(self):
        status, h, body = self.get(f"/api/library/file?book={self.bid}&v=1")
        self.assertEqual((status, body), (200, self.book.read_bytes()))
        self.assertEqual(h["Content-Type"], "application/vnd.comicbook+zip")
        self.assertEqual(h["Content-Disposition"], "attachment; filename*=UTF-8''Big%20one.cbz")
        self.assertIn("immutable", h["Cache-Control"])
        status, h, body = self.get(f"/api/library/file?book={self.bid}", {"Range": "bytes=0-9"})
        self.assertEqual((status, body), (206, self.book.read_bytes()[:10]))
        self.assertEqual(self.get("/api/library/file?book=999")[0], 404)
        self.book.unlink()
        self.lib.scan()
        self.lib.wait_scan(30)
        self.assertEqual(self.get(f"/api/library/file?book={self.bid}")[0], 410)  # missing file

    def test_feed_over_http(self):
        status, _, body = self.get("/api/library/opds")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"<?xml"))
        ET.fromstring(body)


if __name__ == "__main__":
    unittest.main()

"""Tests for EPUB books: parsing, image vs text, chapters and resources, progress, migration."""

import hashlib
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from helpers import CONTAINER, make_cbz, make_epub

from cbzshelf import epub as E
from cbzshelf.library import Library


def url_for(name, frag):
    return f"/r/{name}" + (f"#{frag}" if frag else "")


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def parse(self, path):
        with zipfile.ZipFile(path) as zf:
            return E.parse(zf)

    def test_text_book(self):
        meta = (
            "<dc:creator>Ann Author</dc:creator><dc:creator>Bo Two</dc:creator><dc:subject>fantasy, sea</dc:subject>"
            "<dc:language>en</dc:language><dc:date>2019-04-01</dc:date><dc:description>&lt;p&gt;A tale.&lt;/p&gt;</dc:description>"
            '<meta property="belongs-to-collection" id="c1">Tides</meta><meta refines="#c1" property="group-position">2</meta>'
        )
        b = self.parse(make_epub(self.dir / "t.epub", meta_extra=meta))
        self.assertEqual(b.kind, "epub-text")
        self.assertEqual(b.pages, 3)
        self.assertEqual(b.spine[0], "OEBPS/Text/ch1.xhtml")
        self.assertEqual(b.cover, "OEBPS/Images/cover.png")
        self.assertEqual(
            {
                k: b.meta[k]
                for k in ("title", "authors", "tags", "language", "year", "summary", "series", "number")
            },
            {
                "title": "A Book",
                "authors": ["Ann Author", "Bo Two"],
                "tags": ["fantasy", "sea"],
                "language": "en",
                "year": 2019,
                "summary": "A tale.",
                "series": "Tides",
                "number": 2,
            },
        )
        self.assertEqual([(t["title"], t["n"]) for t in b.toc], [("Part 1", 0), ("Part 2", 1), ("Part 3", 2)])

    def test_image_book_and_rtl(self):
        b = self.parse(make_epub(self.dir / "m.epub", kind="images", chapters=4, rtl=True))
        self.assertEqual(b.kind, "epub-image")
        self.assertEqual(b.images, [f"OEBPS/Images/p{i:03}.png" for i in range(1, 5)])
        self.assertTrue(b.meta["rtl"])
        self.assertEqual(b.cover, "OEBPS/Images/p001.png")

    def test_calibre_series_and_ncx_toc(self):
        meta = '<meta name="calibre:series" content="Saga"/><meta name="calibre:series_index" content="3.5"/>'
        b = self.parse(make_epub(self.dir / "c.epub", meta_extra=meta, nav=False))
        self.assertEqual((b.meta["series"], b.meta["number"]), ("Saga", 3.5))
        self.assertEqual(b.toc, [])  # no nav and no NCX

    def test_text_first_then_pictures_stops_reading_early(self):
        def chapter(i):
            if i == 0:
                return "<p>" + "Words of the story. " * 60 + "</p>"
            return f'<div><img src="../Images/{"cover" if i == 1 else "bg"}.png" alt=""/></div>'

        path = make_epub(self.dir / "mixed.epub", chapters=5, chapter_html=chapter)
        with zipfile.ZipFile(path) as z:
            entries = {n: z.read(n) for n in z.namelist()}
        opf = entries["OEBPS/content.opf"].decode().replace(' properties="cover-image"', "")
        entries["OEBPS/content.opf"] = opf.encode()
        with zipfile.ZipFile(path, "w") as z:
            for n, d in entries.items():
                z.writestr(n, d)
        fed = []
        real_feed = E._DocScan.feed
        with mock.patch.object(E._DocScan, "feed", lambda s, d: fed.append(d) or real_feed(s, d)):
            b = self.parse(path)
        self.assertEqual(len(fed), 2)  # a text page and two documents are enough to decide
        self.assertEqual((b.kind, b.pages, b.images), ("epub-text", 5, []))
        self.assertEqual(b.cover, "OEBPS/Images/cover.png")  # the fallback: first picture near the start

    def test_entities_and_bad_files_refused(self):
        bomb = self.dir / "bomb.epub"
        with zipfile.ZipFile(bomb, "w") as z:
            z.writestr("META-INF/container.xml", CONTAINER)
            z.writestr(
                "OEBPS/content.opf",
                '<?xml version="1.0"?><!DOCTYPE p [<!ENTITY a "aaaa">]><package>&a;</package>',
            )
        with self.assertRaises(E.EpubError):
            self.parse(bomb)
        make_cbz(self.dir / "x.cbz")
        with self.assertRaises(E.EpubError):
            self.parse(self.dir / "x.cbz")

    def test_resolve_and_safe_name(self):
        self.assertEqual(E.resolve("OEBPS/Text", "../Images/a%20b.png#x"), ("OEBPS/Images/a b.png", "x"))
        self.assertIsNone(E.resolve("OEBPS", "../../etc/passwd"))
        self.assertIsNone(E.resolve("OEBPS", "https://example.com/x.png"))
        self.assertIsNone(E.resolve("OEBPS", "/abs.png"))
        names = {"OEBPS/a.png"}
        self.assertEqual(E.safe_name("OEBPS/./a.png", names), "OEBPS/a.png")
        for bad in ("", "../x", "/etc/passwd", "OEBPS/../../x", "..", "a\\b"):
            with self.assertRaises(ValueError, msg=bad):
                E.safe_name(bad, names)
        with self.assertRaises(KeyError):
            E.safe_name("OEBPS/b.png", names)

    def test_rewrite_chapter_removes_active_content(self):
        text = (
            '<html><head><script>alert(1)</script><base href="http://x/"><meta http-equiv="refresh" content="0;url=http://x">'
            '<link href="../Styles/a.css" rel="stylesheet"/></head><body onload="evil()">'
            '<img src="../Images/i.png" onerror="evil()"/><a href="ch2.xhtml#s">n</a><a href="javascript:evil()">j</a>'
            '<a href="#top">t</a><iframe src="http://x"></iframe><div style="background:url(../Images/b.png)"></div></body></html>'
        )
        out = E.rewrite_chapter(text, "OEBPS/Text/ch1.xhtml", url_for)
        for bad in ("<script", "alert", "<base", "http-equiv", "onload", "onerror", "javascript:", "<iframe"):
            self.assertNotIn(bad, out)
        for good in (
            'href="/r/OEBPS/Styles/a.css"',
            'src="/r/OEBPS/Images/i.png"',
            'href="/r/OEBPS/Text/ch2.xhtml#s"',
            'href="#top"',
            'url("/r/OEBPS/Images/b.png")',
        ):
            self.assertIn(good, out)


class LibraryEpubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.root = self.dir / "Books"
        make_epub(
            self.root / "Novels" / "Tale.epub", title="The Tale", meta_extra="<dc:creator>Ann</dc:creator>"
        )
        make_epub(
            self.root / "Manga" / "Manga v01.epub", kind="images", chapters=4, rtl=True, title="Manga 1"
        )
        (self.root / "Broken").mkdir(parents=True)
        (self.root / "Broken" / "bad.epub").write_bytes(b"not a zip")
        self.lib = Library(self.dir / "lib.db", self.dir / "thumbs")
        self.lib.add_root(str(self.root))
        self.lib.wait_scan(30)

    def tearDown(self):
        self.lib.close()
        self.tmp.cleanup()

    def book(self, title):
        return next(b for b in self.lib.search(limit=50)["items"] if b["title"] == title)

    def test_scan_kinds_pages_and_errors(self):
        tale, manga = self.book("The Tale"), self.book("Manga 1")
        self.assertEqual((tale["kind"], tale["pages"]), ("epub-text", 3))
        self.assertEqual((manga["kind"], manga["pages"]), ("epub-image", 4))
        bad = self.book("bad")
        self.assertEqual(bad["pages"], 0)
        self.assertIn("cannot read", bad["error"])
        detail = self.lib.book_detail(tale["id"])
        self.assertEqual(detail["meta"]["authors"], ["Ann"])
        self.assertEqual(len(detail["toc"]), 3)
        sid = manga["series_id"]
        self.assertTrue(self.lib.series_detail(sid)["reader"]["rtl"])

    def test_image_epub_pages_and_covers(self):
        manga, tale = self.book("Manga 1"), self.book("The Tale")
        data, ctype = self.lib.page(manga["id"], 3)
        self.assertEqual((ctype, data[:4]), ("image/png", b"\x89PNG"))
        with self.assertRaises(ValueError):
            self.lib.page(tale["id"], 0)  # text books have chapters
        for b in (manga, tale):
            self.assertTrue(self.lib.thumbnail(b["id"]).startswith(b"RIFF"))

    def test_text_without_cover_gets_placeholder(self):
        path = make_epub(self.root / "Plain" / "Plain.epub", title="No Picture Here")
        with zipfile.ZipFile(path) as z:
            entries = {n: z.read(n) for n in z.namelist()}
        opf = entries["OEBPS/content.opf"].decode().replace(' properties="cover-image"', "")
        entries["OEBPS/content.opf"] = opf.encode()
        with zipfile.ZipFile(path, "w") as z:
            for n, d in entries.items():
                z.writestr(n, d)
        self.lib.scan_file(path)
        self.assertTrue(self.lib.thumbnail(self.book("No Picture Here")["id"]).startswith(b"RIFF"))

    def test_chapter_and_resources(self):
        tale = self.book("The Tale")
        data, ctype = self.lib.chapter(tale["id"], 0, url_for)
        self.assertEqual(ctype, "application/xhtml+xml; charset=utf-8")
        self.assertIn(b'href="/r/OEBPS/Styles/book.css"', data)
        css, ctype = self.lib.resource(tale["id"], "OEBPS/Styles/book.css", url_for)
        self.assertEqual(ctype, "text/css; charset=utf-8")
        self.assertIn(b'url("/r/OEBPS/Images/bg.png")', css)
        _, ctype = self.lib.resource(tale["id"], "OEBPS/Images/bg.png", url_for)
        self.assertEqual(ctype, "image/png")
        with self.assertRaises(ValueError):
            self.lib.resource(tale["id"], "OEBPS/Text/ch1.xhtml", url_for)  # documents only via chapter
        with self.assertRaises(ValueError):
            self.lib.resource(tale["id"], "../../../etc/passwd", url_for)
        with self.assertRaises(KeyError):
            self.lib.resource(tale["id"], "OEBPS/nope.png", url_for)
        with self.assertRaises(ValueError):
            self.lib.chapter(tale["id"], 3, url_for)
        with self.assertRaises(ValueError):
            self.lib.chapter(self.book("Manga 1")["id"], 0, url_for)

    def test_html_chapter_served_as_html(self):
        make_epub(
            self.root / "Loose" / "Loose.epub", title="Loose", chapter_html=lambda i: "<p>a&nbsp;b<br></p>"
        )
        self.lib.scan_file(self.root / "Loose" / "Loose.epub")
        _, ctype = self.lib.chapter(self.book("Loose")["id"], 0, url_for)
        self.assertEqual(ctype, "text/html; charset=utf-8")

    def test_text_progress_with_position(self):
        tale = self.book("The Tale")
        self.assertEqual(
            self.lib.set_progress(tale["id"], 2, pos=0.5), {"page": 2, "pos": 0.5, "finished": False}
        )
        self.assertTrue(self.lib.set_progress(tale["id"], 2, pos=0.99)["finished"])
        self.assertEqual(self.lib.set_progress(tale["id"], 1, pos=7)["pos"], 1.0)  # clamped
        p = self.lib.book_detail(tale["id"])["progress"]
        self.assertEqual((p["page"], p["pos"]), (1, 1.0))

    def test_epubs_are_never_written(self):
        files = sorted(self.root.rglob("*.epub"))
        before = {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
        tale, manga = self.book("The Tale"), self.book("Manga 1")
        self.lib.edit_book(tale["id"], {"title": "Renamed"})
        self.lib.chapter(tale["id"], 1, url_for)
        self.lib.thumbnail(manga["id"])
        self.lib.scan()
        self.lib.wait_scan(30)
        self.assertEqual({f: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}, before)


class MigrationTests(unittest.TestCase):
    def test_0_1_database_is_upgraded_and_keeps_data(self):
        from cbzshelf import library as L

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db = tmp / "old.db"
            make_cbz(tmp / "C" / "S" / "a.cbz", n_pages=4)
            # A 0.1.x database: the base schema, no migrations run, with a book, an edit and progress.
            saved, L.MIGRATIONS = L.MIGRATIONS, []
            try:
                Library(db, tmp / "thumbs").close()
            finally:
                L.MIGRATIONS = saved
            con = sqlite3.connect(db)
            book = tmp / "C" / "S" / "a.cbz"
            con.executescript(
                f"""INSERT INTO roots(id, path, added_at) VALUES (1, '{tmp / "C"}', 1);
                    INSERT INTO series(id, root_id, key, folder, file_name) VALUES (1, 1, 'dir:x', '{book.parent}', 'S');
                    INSERT INTO books(id, root_id, series_id, path, size, mtime, pages, user_json, meta_json, added_at)
                        VALUES (7, 1, 1, '{book}', 1, 1, 4, '{{"title": "Mine"}}', '{{"title": "Mine"}}', 1);
                    INSERT INTO progress(book_id, page, finished, updated_at) VALUES (7, 2, 0, 1);"""
            )
            con.commit()
            con.close()
            bid = 7
            self.assertEqual(sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0], 0)

            lib = Library(db, tmp / "thumbs")
            self.addCleanup(lib.close)
            self.assertIn("kind", {r[1] for r in lib._q("PRAGMA table_info(books)")})
            self.assertIn("user_id", {r[1] for r in lib._q("PRAGMA table_info(progress)")})
            b = lib.book_detail(bid)
            self.assertEqual((b["title"], b["kind"]), ("Mine", "cbz"))
            self.assertEqual((b["progress"]["page"], b["progress"]["pos"]), (2, 0))  # kept, as user 0's
            self.assertEqual(lib._q("PRAGMA user_version")[0][0], len(L.MIGRATIONS))


if __name__ == "__main__":
    unittest.main()

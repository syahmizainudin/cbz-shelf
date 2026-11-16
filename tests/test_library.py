"""Tests for metadata parsing and the library (scan, series, edits, missing files, search, pages)."""

import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from helpers import comicinfo, make_cbz

from cbzshelf import meta as M
from cbzshelf.library import Library


class MetaTests(unittest.TestCase):
    def test_filenames(self):
        cases = {
            "Naruto v01": {"volume": 1},
            "Naruto Vol. 3": {"volume": 3},
            "Berserk Volume 41": {"volume": 41},
            "One Piece c012": {"number": 12},
            "One Piece - Chapter 12.5": {"number": 12.5},
            "Batman #12": {"number": 12},
            "Vagabond ch 7": {"number": 7},
            "Chainsaw Man v02 c010": {"volume": 2, "number": 10},
            "Saga 054 (2018) (Digital)": {"number": 54},
            "Spider-Man (2021) 001": {"number": 1},
            "The Witcher [v3]": {"volume": 3},
            "Akira": {},
        }
        for stem, want in cases.items():
            with self.subTest(stem=stem):
                self.assertEqual(M.parse_filename(stem), want)

    def test_comicinfo(self):
        xml = comicinfo(
            Title="Romance Dawn",
            Series="One Piece",
            Number="1",
            Volume="1",
            Year="1997",
            Writer="Eiichiro Oda",
            Penciller="Eiichiro Oda, Someone Else",
            Tags="pirates, adventure",
            Genre="Shonen",
            Manga="YesAndRightToLeft",
            LanguageISO="ja",
            pages_xml='<Pages><Page Image="0"/><Page Image="2" Type="FrontCover"/></Pages>',
        )
        meta, cover = M.parse_comicinfo(xml)
        self.assertEqual(meta["authors"], ["Eiichiro Oda", "Someone Else"])
        self.assertEqual(meta["tags"], ["pirates", "adventure"])
        self.assertEqual(
            (meta["series"], meta["number"], meta["year"], meta["rtl"]), ("One Piece", 1, 1997, True)
        )
        self.assertEqual(cover, 2)

    def test_unsafe_or_bad_xml_ignored(self):
        bomb = (
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><ComicInfo><Title>&a;</Title></ComicInfo>'
        )
        self.assertEqual(M.parse_comicinfo(bomb), ({}, None))
        self.assertEqual(M.parse_comicinfo(b"<not xml"), ({}, None))
        self.assertEqual(M.parse_comicinfo(b"x" * (M.COMICINFO_MAX + 1)), ({}, None))

    def test_edit_layers(self):
        user = M.apply_changes({}, {"title": "Mine", "tags": "a, b", "year": "2001", "rtl": True})
        self.assertEqual(user, {"title": "Mine", "tags": ["a", "b"], "year": 2001, "rtl": True})
        self.assertEqual(M.apply_changes(user, {"title": None, "tags": ""}), {"year": 2001, "rtl": True})
        self.assertEqual(
            M.effective({"title": "File", "authors": ["X"]}, {"title": "Mine"}),
            {"title": "Mine", "authors": ["X"]},
        )
        for bad in ({"nope": 1}, {"year": "abc"}, {"rtl": "yes"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                M.clean_user_changes(bad)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.root = self.dir / "Comics"
        make_cbz(self.root / "Naruto" / "Naruto v02.cbz")
        make_cbz(
            self.root / "Naruto" / "Naruto v01.cbz",
            info=comicinfo(Writer="Masashi Kishimoto", Tags="ninja, action", Manga="YesAndRightToLeft"),
        )
        make_cbz(
            self.root / "Misc" / "Saga 001.cbz",
            info=comicinfo(Series="Saga", Writer="Brian K. Vaughan", Number="1"),
        )
        make_cbz(self.root / "Misc" / "Saga 002.cbz", info=comicinfo(Series="Saga", Number="2"))
        make_cbz(self.root / "Misc" / "deep" / "Loose One.cbz", names=["10.webp", "2.webp", "1.webp"])
        make_cbz(self.root / "Misc" / "junk.cbz", extra={"__MACOSX/._x.webp": b"x"}, n_pages=1)
        (self.root / "Misc" / "broken.cbz").write_bytes(b"not a zip")
        (self.root / ".hidden").mkdir()
        make_cbz(self.root / ".hidden" / "secret.cbz")
        self.lib = Library(self.dir / "db" / "lib.db", self.dir / "thumbs")
        self.lib.add_root(str(self.root))
        self.lib.wait_scan(30)

    def tearDown(self):
        self.lib.close()
        self.tmp.cleanup()

    def series(self, name):
        return next(s for s in self.lib.series_list(limit=200)["items"] if s["name"] == name)

    def book(self, title):
        return next(b for b in self.lib.search(limit=200)["items"] if b["title"] == title)

    def test_scan_builds_series(self):
        names = sorted((s["name"] for s in self.lib.series_list()["items"]), key=str.lower)
        self.assertEqual(names, ["deep", "Misc", "Naruto", "Saga"])  # ComicInfo Series wins over the folder
        self.assertEqual(self.lib.summary()["books"], 7)  # .hidden skipped; broken.cbz kept with an error
        broken = self.book("broken")
        self.assertEqual(broken["pages"], 0)
        self.assertIn("cannot read", broken["error"])
        self.assertEqual(self.lib.scan_state["last"]["errors"], 1)

    def test_series_detail_order_and_reader_defaults(self):
        s = self.lib.series_detail(self.series("Naruto")["id"])
        self.assertEqual([b["title"] for b in s["books"]], ["Naruto v01", "Naruto v02"])
        self.assertEqual(s["reader"], {"rtl": True, "mode": "single", "fit": "height"})
        self.assertEqual(s["authors"], ["Masashi Kishimoto"])

    def test_pages_in_natural_order_and_junk_skipped(self):
        loose = self.book("Loose One")
        self.assertEqual(loose["pages"], 3)
        data0, ctype = self.lib.page(loose["id"], 0)
        with zipfile.ZipFile(self.root / "Misc" / "deep" / "Loose One.cbz") as z:
            self.assertEqual(data0, z.read("1.webp"))
            self.assertEqual(self.lib.page(loose["id"], 2)[0], z.read("10.webp"))
        self.assertEqual(ctype, "image/webp")
        self.assertEqual(self.book("junk")["pages"], 1)
        with self.assertRaises(ValueError):
            self.lib.page(loose["id"], 3)

    def test_thumbnail_cached(self):
        b = self.book("Naruto v01")
        first = self.lib.thumbnail(b["id"])
        self.assertTrue(first.startswith(b"RIFF"))
        files = list((self.dir / "thumbs").glob(f"{b['id']}-*.webp"))
        self.assertEqual(len(files), 1)
        mtime = files[0].stat().st_mtime
        time.sleep(0.01)
        self.assertEqual(self.lib.thumbnail(b["id"]), first)
        self.assertEqual(files[0].stat().st_mtime, mtime)

    def test_search_and_facets(self):
        self.assertEqual([b["title"] for b in self.lib.search("kishi")["items"]], ["Naruto v01"])
        self.assertEqual([b["title"] for b in self.lib.search("vaughan saga")["items"]], ["Saga 001"])
        self.assertEqual([b["title"] for b in self.lib.search(tag="ninja")["items"]], ["Naruto v01"])
        self.assertEqual([s["name"] for s in self.lib.series_list(q="ninja")["items"]], ["Naruto"])
        self.assertEqual(
            [s["name"] for s in self.lib.series_list(author="Brian K. Vaughan")["items"]], ["Saga"]
        )
        facets = self.lib.facets()
        self.assertIn({"value": "ninja", "n": 1}, facets["tags"])
        with self.assertRaises(ValueError):
            self.lib.search("!!!")

    def test_edits_survive_rescan_of_a_changed_file(self):
        b = self.book("Naruto v02")
        self.lib.edit_book(b["id"], {"title": "My Title", "tags": "favourite", "authors": "Me"})
        self.assertEqual([x["title"] for x in self.lib.search("favourite")["items"]], ["My Title"])
        # the file changes: new ComicInfo
        path = self.root / "Naruto" / "Naruto v02.cbz"
        make_cbz(path, n_pages=5, info=comicinfo(Title="File Title", Writer="Someone"))
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.lib.scan()
        self.lib.wait_scan(30)
        d = self.lib.book_detail(b["id"])
        self.assertEqual((d["title"], d["pages"]), ("My Title", 5))
        self.assertEqual(d["file_meta"]["title"], "File Title")
        self.assertEqual(d["meta"]["authors"], ["Me"])
        # clearing the override shows the file's value again
        d = self.lib.edit_book(b["id"], {"title": None, "authors": None})
        self.assertEqual((d["title"], d["meta"]["authors"]), ("File Title", ["Someone"]))

    def test_missing_keeps_edits_and_progress(self):
        b = self.book("Naruto v01")
        self.lib.edit_book(b["id"], {"tags": "keep me"})
        self.lib.set_progress(b["id"], 1)
        path = self.root / "Naruto" / "Naruto v01.cbz"
        moved = self.dir / "elsewhere.cbz"
        path.rename(moved)
        self.lib.scan()
        self.lib.wait_scan(30)
        d = self.lib.book_detail(b["id"])
        self.assertTrue(d["missing"])
        self.assertEqual(self.lib.summary()["missing"], 1)
        with self.assertRaises(FileNotFoundError):
            self.lib.page(b["id"], 0)
        moved.rename(path)  # it comes back
        self.lib.scan()
        self.lib.wait_scan(30)
        d = self.lib.book_detail(b["id"])
        self.assertFalse(d["missing"])
        self.assertEqual((d["meta"]["tags"], d["progress"]["page"]), (["keep me"], 1))
        path.unlink()
        self.lib.scan()
        self.lib.wait_scan(30)
        self.assertEqual(self.lib.remove_missing(), 1)
        with self.assertRaises(KeyError):
            self.lib.book_detail(b["id"])

    def test_progress(self):
        b = self.book("Naruto v01")
        self.assertEqual(self.lib.set_progress(b["id"], 1), {"page": 1, "pos": 0.0, "finished": False})
        self.assertEqual([x["id"] for x in self.lib.continue_reading()], [b["id"]])
        # clamped; last page
        self.assertEqual(self.lib.set_progress(b["id"], 99), {"page": 2, "pos": 0.0, "finished": True})
        self.assertEqual(self.lib.continue_reading(), [])
        self.lib.clear_progress(b["id"])
        self.assertIsNone(self.lib.book_detail(b["id"])["progress"])

    def test_series_edits_and_reader_settings(self):
        sid = self.series("Naruto")["id"]
        s = self.lib.edit_series(sid, {"name": "NARUTO", "mode": "double", "rtl": False, "tags": "classic"})
        self.assertEqual(
            (s["name"], s["reader"]["mode"], s["reader"]["rtl"], s["tags"]),
            ("NARUTO", "double", False, ["classic"]),
        )
        self.assertEqual([x["name"] for x in self.lib.series_list(q="NARUTO")["items"]], ["NARUTO"])
        for bad in ({"mode": "spiral"}, {"rtl": "yes"}, {"colour": "red"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.lib.edit_series(sid, bad)
        with self.assertRaises(ValueError):
            self.lib.edit_book(self.book("Naruto v01")["id"], {"series": "Other"})

    def test_roots(self):
        with self.assertRaisesRegex(ValueError, "already"):
            self.lib.add_root(str(self.root))
        with self.assertRaisesRegex(ValueError, "overlaps"):
            self.lib.add_root(str(self.root / "Naruto"))
        with self.assertRaisesRegex(ValueError, "not a folder"):
            self.lib.add_root(str(self.dir / "nope"))
        rid = self.lib.roots()[0]["id"]
        self.assertTrue(self.lib.remove_root(rid))
        self.assertEqual(self.lib.summary()["books"], 0)
        self.assertEqual(self.lib.search(limit=10)["items"], [])
        self.assertTrue((self.root / "Naruto" / "Naruto v01.cbz").exists())  # files untouched

    def test_scan_file_for_one_new_book(self):
        new = make_cbz(self.root / "Naruto" / "Naruto v03.cbz")
        self.assertTrue(self.lib.scan_file(new))
        self.assertEqual(len(self.lib.series_detail(self.series("Naruto")["id"])["books"]), 3)
        self.assertFalse(self.lib.scan_file(self.dir / "outside.cbz"))

    def test_archives_are_never_written(self):
        before = {p: (p.stat().st_mtime, p.read_bytes()) for p in self.root.rglob("*.cbz")}
        b = self.book("Naruto v01")
        self.lib.edit_book(b["id"], {"title": "x"})
        self.lib.set_progress(b["id"], 1)
        self.lib.thumbnail(b["id"])
        self.lib.page(b["id"], 0)
        self.assertEqual(before, {p: (p.stat().st_mtime, p.read_bytes()) for p in self.root.rglob("*.cbz")})

    def test_persists_across_restart(self):
        b = self.book("Naruto v01")
        self.lib.edit_book(b["id"], {"title": "Kept"})
        self.lib.close()
        self.lib = Library(self.dir / "db" / "lib.db", self.dir / "thumbs")
        self.assertEqual(self.lib.book_detail(b["id"])["title"], "Kept")
        self.assertEqual([x["title"] for x in self.lib.search("kept")["items"]], ["Kept"])


if __name__ == "__main__":
    unittest.main()

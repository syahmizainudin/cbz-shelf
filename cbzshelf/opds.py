"""An OPDS 1.2 catalog of the library, for reader apps (Panels, Chunky, KOReader…).

Served by ``LibraryAPI.get("opds", ...)`` at ``/api/library/opds?feed=...``. URLs are flat (query
parameters, no path segments) because the console server only routes ``<prefix><action>``.

  (no feed)            navigation: All, each library, search
  feed=series          navigation: series (``library=`` for one library), 50 a page
  feed=books&series=   acquisition: a series' books
  feed=search&q=       acquisition: matching books
  feed=opensearch      the OpenSearch description reader apps use for the search box

Each book links its file (``file?book=``) for download, its cover, and for comics and picture
EPUBs an OPDS Page Streaming Extension link to ``page?book=&n={pageNumber}``, so apps can read
without downloading. Everything is limited to the libraries the request may see (the caller
passes ``libraries``); a hidden series answers 404 like a missing one. Built with
``xml.etree.ElementTree``: every text and attribute is escaped by it.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

ATOM = "http://www.w3.org/2005/Atom"
OPDS = "http://opds-spec.org/2010/catalog"
PSE = "http://vaemendis.net/opds-pse/ns"
OS = "http://a9.com/-/spec/opensearch/1.1/"
DC = "http://purl.org/dc/terms/"
for prefix, uri in (("", ATOM), ("opds", OPDS), ("pse", PSE), ("opensearch", OS), ("dc", DC)):
    ET.register_namespace(prefix, uri)

NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"
SEARCH_TYPE = "application/opensearchdescription+xml"
PAGE = 50
BOOK_MIME = {
    "cbz": "application/vnd.comicbook+zip",
    "epub-image": "application/epub+zip",
    "epub-text": "application/epub+zip",
}


def _a(tag: str) -> str:
    return f"{{{ATOM}}}{tag}"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Catalog:
    def __init__(self, library, prefix: str = "/api/library/", title: str = "Library"):
        self.lib, self.prefix, self.title = library, prefix, title

    def url(self, action: str = "opds", **params) -> str:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        return self.prefix + action + ("?" + urlencode(params) if params else "")

    # ------------------------------------------------------------ building blocks
    def _feed(self, fid: str, title: str, kind: str, self_url: str) -> ET.Element:
        feed = ET.Element(_a("feed"))
        ET.SubElement(feed, _a("id")).text = f"urn:cbz-shelf:{fid}"
        ET.SubElement(feed, _a("title")).text = title
        ET.SubElement(feed, _a("updated")).text = _now()
        author = ET.SubElement(feed, _a("author"))
        ET.SubElement(author, _a("name")).text = "cbz-shelf"
        self._link(feed, "self", self_url, kind)
        self._link(feed, "start", self.url(), NAV)
        self._link(feed, "search", self.url(feed="opensearch"), SEARCH_TYPE)
        return feed

    @staticmethod
    def _link(parent: ET.Element, rel: str, href: str, typ: str, **attrs) -> ET.Element:
        return ET.SubElement(parent, _a("link"), {"rel": rel, "href": href, "type": typ, **attrs})

    def _nav_entry(self, feed, eid: str, title: str, href: str, text: str = "", kind: str = NAV):
        e = ET.SubElement(feed, _a("entry"))
        ET.SubElement(e, _a("id")).text = f"urn:cbz-shelf:{eid}"
        ET.SubElement(e, _a("title")).text = title
        ET.SubElement(e, _a("updated")).text = _now()
        if text:
            ET.SubElement(e, _a("content"), {"type": "text"}).text = text
        self._link(e, "subsection", href, kind)
        return e

    def _book_entry(self, feed, b: dict) -> None:
        e = ET.SubElement(feed, _a("entry"))
        ET.SubElement(e, _a("id")).text = f"urn:cbz-shelf:book:{b['id']}"
        ET.SubElement(e, _a("title")).text = b["title"]
        ET.SubElement(e, _a("updated")).text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(b["v"]))
        for name in b.get("authors") or []:
            ET.SubElement(ET.SubElement(e, _a("author")), _a("name")).text = name
        for tag in b.get("tags") or []:
            ET.SubElement(e, _a("category"), {"term": tag, "label": tag})
        bits = [
            x
            for x in (
                b.get("volume") is not None and f"Vol. {b['volume']}",
                b.get("number") is not None and f"#{b['number']}",
            )
            if x
        ]
        unit = "chapters" if b["kind"] == "epub-text" else "pages"
        ET.SubElement(e, _a("content"), {"type": "text"}).text = " ".join([*bits, f"{b['pages']} {unit}"])
        v = b["v"]
        thumb = self.url("thumb", book=b["id"], v=v)
        self._link(e, "http://opds-spec.org/image", thumb, "image/webp")
        self._link(e, "http://opds-spec.org/image/thumbnail", thumb, "image/webp")
        self._link(
            e,
            "http://opds-spec.org/acquisition",
            self.url("file", book=b["id"], v=v),
            BOOK_MIME.get(b["kind"], "application/zip"),
        )
        if b["kind"] != "epub-text" and b["pages"]:
            # {pageNumber} is filled in by the app, so it can't go through urlencode
            href = f"{self.prefix}page?book={b['id']}&v={v}&n={{pageNumber}}"
            self._link(
                e,
                "http://vaemendis.net/opds-pse/stream",
                href,
                "image/jpeg",
                **{f"{{{PSE}}}count": str(b["pages"])},
            )

    def _paging(self, el, total: int, offset: int, **params) -> None:
        if offset + PAGE < total:
            self._link(el, "next", self.url(**params, offset=offset + PAGE), el.find(_a("link")).get("type"))
        if offset > 0:
            self._link(
                el,
                "previous",
                self.url(**params, offset=max(offset - PAGE, 0) or None),
                el.find(_a("link")).get("type"),
            )
        ET.SubElement(el, f"{{{OS}}}totalResults").text = str(total)

    # ------------------------------------------------------------ feeds
    def root(self, libraries) -> ET.Element:
        feed = self._feed("root", self.title, NAV, self.url())
        libs = self.lib.libraries(libraries)
        self._nav_entry(feed, "all", "All series", self.url(feed="series"), "Every series, by name")
        if len(libs) > 1:
            for x in libs:
                self._nav_entry(
                    feed,
                    f"library:{x['id']}",
                    x["name"],
                    self.url(feed="series", library=x["id"]),
                    f"{x['books']} books",
                )
        return feed

    def series(self, libraries, library=None, offset: int = 0) -> ET.Element:
        scope = libraries
        title = "All series"
        if library not in (None, ""):
            lid = int(library)
            scope = frozenset({lid}) if libraries is None or lid in libraries else frozenset()
            named = [x["name"] for x in self.lib.libraries(scope)]
            if not named:
                raise KeyError(f"no library {lid}")
            title = named[0]
        res = self.lib.series_list(limit=PAGE, offset=offset, libraries=scope)
        feed = self._feed(
            f"series:{library or 'all'}:{offset}",
            title,
            NAV,
            self.url(feed="series", library=library, offset=offset or None),
        )
        self._link(feed, "up", self.url(), NAV)
        for s in res["items"]:
            e = self._nav_entry(
                feed,
                f"series:{s['id']}",
                s["name"],
                self.url(feed="books", series=s["id"]),
                f"{s['books']} book{'s' if s['books'] != 1 else ''}",
                ACQ,
            )
            if s.get("cover_book"):
                self._link(
                    e,
                    "http://opds-spec.org/image/thumbnail",
                    self.url("thumb", book=s["cover_book"], v=s["cover_v"]),
                    "image/webp",
                )
        self._paging(feed, res["total"], offset, feed="series", library=library)
        return feed

    def books(self, libraries, series_id: int) -> ET.Element:
        self.lib.check_visible("series", series_id, libraries)
        s = self.lib.series_detail(series_id)
        feed = self._feed(f"books:{series_id}", s["name"], ACQ, self.url(feed="books", series=series_id))
        self._link(feed, "up", self.url(feed="series"), NAV)
        for b in s["books"]:
            if not b["missing"] and not b["error"]:
                self._book_entry(feed, b)
        return feed

    def search(self, libraries, q: str, offset: int = 0) -> ET.Element:
        res = self.lib.search(q=q, limit=PAGE, offset=offset, libraries=libraries)
        feed = self._feed(
            f"search:{q}:{offset}", f"Search: {q}", ACQ, self.url(feed="search", q=q, offset=offset or None)
        )
        self._link(feed, "up", self.url(), NAV)
        for b in res["items"]:
            if not b["missing"] and not b["error"]:
                self._book_entry(feed, b)
        self._paging(feed, res["total"], offset, feed="search", q=q)
        return feed

    def opensearch(self) -> bytes:
        root = ET.Element(f"{{{OS}}}OpenSearchDescription")
        ET.SubElement(root, f"{{{OS}}}ShortName").text = self.title
        ET.SubElement(root, f"{{{OS}}}Description").text = "Search titles, series, authors and tags"
        ET.SubElement(
            root,
            f"{{{OS}}}Url",
            {"type": ACQ, "template": f"{self.prefix}opds?feed=search&q={{searchTerms}}"},
        )
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def answer(self, query: dict, libraries) -> tuple[bytes, str]:
        """(body, content type) for an ``opds`` request."""
        feed_name = query.get("feed", "")
        offset = max(int(query.get("offset", 0) or 0), 0)
        if feed_name == "opensearch":
            return self.opensearch(), SEARCH_TYPE
        if feed_name == "":
            feed, kind = self.root(libraries), NAV
        elif feed_name == "series":
            feed, kind = self.series(libraries, query.get("library"), offset), NAV
        elif feed_name == "books":
            feed, kind = self.books(libraries, int(query["series"])), ACQ
        elif feed_name == "search":
            feed, kind = self.search(libraries, str(query.get("q", "")), offset), ACQ
        else:
            raise KeyError(f"no feed {feed_name}")
        return ET.tostring(feed, encoding="utf-8", xml_declaration=True), kind + ";charset=utf-8"

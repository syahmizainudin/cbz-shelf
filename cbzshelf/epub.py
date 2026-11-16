"""Reading EPUB files: metadata, spine, cover, table of contents, and image or text kind.

An EPUB is a zip. ``META-INF/container.xml`` names the package (OPF) file, which lists the
metadata, every file (the manifest) and the reading order (the spine).

Two kinds of book are told apart from the spine:

* ``epub-image``: every spine document is a picture with (almost) no text, as in manga and comic
  EPUBs. Its pages are those pictures, read in the comic reader.
* ``epub-text``: anything else (novels). Its "pages" are the spine documents (chapters), shown in a
  sandboxed frame by the text reader.

Everything here only reads. The XML files (container, OPF, NCX) are size-capped, and a DOCTYPE with
an internal subset or any ENTITY is refused before parsing. XHTML documents are never parsed as
XML here: they go through ``html.parser``, which doesn't expand entities.
"""

from __future__ import annotations

import html
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

XML_MAX = 4 * 1024 * 1024  # container, OPF and NCX; real ones are tens of KB
DOC_MAX = 8 * 1024 * 1024  # one chapter
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")
TEXTY = 200  # characters of text that make a spine document a text page, not a picture page


class EpubError(ValueError):
    pass


@dataclass
class EpubBook:
    kind: str  # "epub-image" or "epub-text"
    meta: dict
    spine: list[str]  # zip names, in reading order
    images: list[str] = field(default_factory=list)  # epub-image: the pages
    cover: str | None = None  # zip name of the cover image
    toc: list[dict] = field(default_factory=list)  # {"title", "n" (spine index), "frag", "level"}

    @property
    def pages(self) -> int:
        return len(self.images) if self.kind == "epub-image" else len(self.spine)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def safe_xml(data: bytes) -> ET.Element:
    """Parse a small XML file, refusing entity declarations (no billion laughs, no XXE)."""
    if len(data) > XML_MAX:
        raise EpubError("XML file too large")
    if b"<!ENTITY" in data or re.search(rb"<!DOCTYPE[^>]*\[", data):
        raise EpubError("XML with entity declarations refused")
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise EpubError(f"bad XML: {e}") from None


def resolve(base_dir: str, href: str) -> tuple[str, str] | None:
    """A relative link inside the EPUB -> (zip name, fragment). None if it leaves the EPUB."""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        return None
    path = unquote(parts.path)
    if not path:
        return None
    if path.startswith("/"):
        return None
    name = posixpath.normpath(posixpath.join(base_dir, path))
    if name.startswith("../") or name == ".." or name.startswith("/"):
        return None
    return name, parts.fragment


def safe_name(raw: str, names: set[str]) -> str:
    """A zip entry name from a request: normalized, inside the EPUB, and present in it."""
    name = posixpath.normpath(unquote(raw or ""))
    if not raw or name.startswith(("../", "/")) or name in ("..", ".") or "\\" in name or "\0" in name:
        raise ValueError("bad resource name")
    if name not in names:
        raise KeyError("no such resource in this book")
    return name


class _DocScan(HTMLParser):
    """Pictures and amount of text in one spine document."""

    SKIP = frozenset({"script", "style", "title", "head"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images: list[str] = []
        self.text = 0
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "img" and a.get("src"):
            self.images.append(a["src"])
        elif tag == "image":  # <svg><image xlink:href=...>
            href = a.get("xlink:href") or a.get("href")
            if href:
                self.images.append(href)

    def handle_startendtag(self, tag, attrs):
        if tag not in self.SKIP:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.text += len("".join(data.split()))


class _NavScan(HTMLParser):
    """Links in the EPUB 3 navigation document's table of contents."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str, int]] = []  # (href, title, level)
        self._nav_depth = 0  # >0 inside <nav epub:type="toc">
        self._ol = 0
        self._href: str | None = None
        self._text: list[str] = []
        self._any_nav = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "nav":
            self._any_nav += 1
            if self._nav_depth or "toc" in (a.get("epub:type") or "").split():
                self._nav_depth += 1
        elif self._nav_depth and tag == "ol":
            self._ol += 1
        elif self._nav_depth and tag == "a":
            self._href, self._text = a.get("href"), []

    def handle_endtag(self, tag):
        if tag == "nav" and self._nav_depth:
            self._nav_depth -= 1
        elif tag == "ol" and self._nav_depth and self._ol:
            self._ol -= 1
        elif tag == "a" and self._href is not None:
            title = " ".join("".join(self._text).split())
            self.items.append((self._href, title, max(self._ol - 1, 0)))
            self._href = None

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)


def _read(zf: zipfile.ZipFile, name: str, cap: int) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > cap:
        raise EpubError(f"{name} is too large")
    return zf.read(info)


def decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _num(v: str | None):
    try:
        n = float((v or "").strip())
    except ValueError:
        return None
    return int(n) if n.is_integer() else n


def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def _metadata(pkg: ET.Element) -> dict:
    md = next((e for e in pkg if _local(e.tag) == "metadata"), None)
    meta: dict = {}
    if md is None:
        return meta
    dc: dict[str, list[str]] = {}
    named: dict[str, str] = {}  # <meta name= content=> (EPUB 2, calibre)
    props: list[ET.Element] = []  # <meta property=> (EPUB 3)
    for e in md.iter():
        tag = _local(e.tag)
        if tag == "meta":
            if e.get("name"):
                named[e.get("name")] = e.get("content") or ""
            elif e.get("property"):
                props.append(e)
        elif e.text and e.text.strip():
            dc.setdefault(tag, []).append(e.text.strip())
    if dc.get("title"):
        meta["title"] = dc["title"][0]
    authors: list[str] = []
    for a in dc.get("creator", []):
        if a.lower() not in (x.lower() for x in authors):
            authors.append(a)
    if authors:
        meta["authors"] = authors
    tags = []
    for s in dc.get("subject", []):
        tags += [t.strip() for t in re.split(r"[,;]", s) if t.strip() and t.strip() not in tags]
    if tags:
        meta["tags"] = tags
    for key, tag in (("publisher", "publisher"), ("language", "language")):
        if dc.get(tag):
            meta[key] = dc[tag][0]
    if dc.get("description"):
        meta["summary"] = _strip_tags(dc["description"][0])[:10000]
    for d in dc.get("date", []):
        if m := re.match(r"\s*(\d{4})", d):
            meta["year"] = int(m.group(1))
            break
    # Series: calibre's meta, or EPUB 3 belongs-to-collection with group-position.
    if named.get("calibre:series"):
        meta["series"] = named["calibre:series"].strip()
        if (n := _num(named.get("calibre:series_index"))) is not None:
            meta["number"] = n
    else:
        for p in props:
            if p.get("property") == "belongs-to-collection" and (p.text or "").strip():
                meta["series"] = p.text.strip()
                pid = p.get("id")
                for r in props:
                    if pid and r.get("refines") == "#" + pid and r.get("property") == "group-position":
                        if (n := _num(r.text)) is not None:
                            meta["number"] = n
                break
    return meta


def parse(zf: zipfile.ZipFile) -> EpubBook:
    """Read an EPUB's structure. Raises EpubError (a ValueError) for anything that isn't one."""
    names = set(zf.namelist())
    if "META-INF/container.xml" not in names:
        raise EpubError("not an EPUB: no META-INF/container.xml")
    container = safe_xml(_read(zf, "META-INF/container.xml", XML_MAX))
    rootfile = next((e.get("full-path") for e in container.iter() if _local(e.tag) == "rootfile"), None)
    if not rootfile or rootfile not in names:
        raise EpubError("not an EPUB: the package file is missing")
    pkg = safe_xml(_read(zf, rootfile, XML_MAX))
    opf_dir = posixpath.dirname(rootfile)

    manifest: dict[str, dict] = {}
    spine_el = None
    for e in pkg:
        if _local(e.tag) == "manifest":
            for item in e:
                if _local(item.tag) == "item" and item.get("id") and item.get("href"):
                    r = resolve(opf_dir, item.get("href"))
                    if r:
                        manifest[item.get("id")] = {
                            "name": r[0],
                            "type": item.get("media-type") or "",
                            "props": (item.get("properties") or "").split(),
                        }
        elif _local(e.tag) == "spine":
            spine_el = e
    if spine_el is None:
        raise EpubError("not an EPUB: no spine")
    spine = [
        manifest[i.get("idref")]["name"]
        for i in spine_el
        if _local(i.tag) == "itemref"
        and i.get("idref") in manifest
        and manifest[i.get("idref")]["name"] in names
    ]
    if not spine:
        raise EpubError("this EPUB's reading order is empty")

    meta = _metadata(pkg)
    if spine_el.get("page-progression-direction") == "rtl":
        meta["rtl"] = True

    # Classify: picture pages or text.
    pictures: list[list[str]] = []
    texty = 0
    for name in spine:
        try:
            doc = decode(_read(zf, name, DOC_MAX))
        except EpubError:
            texty += 1
            pictures.append([])
            continue
        scan = _DocScan()
        scan.feed(doc)
        scan.close()
        base = posixpath.dirname(name)
        imgs = []
        for src in scan.images:
            r = resolve(base, src)
            if r and r[0] in names and r[0].lower().endswith(IMAGE_EXTS):
                imgs.append(r[0])
        pictures.append(imgs)
        if scan.text > TEXTY:
            texty += 1
    images = [i for imgs in pictures for i in imgs]
    kind = "epub-image" if images and not texty else "epub-text"

    # Cover: EPUB 3 cover-image, EPUB 2 <meta name="cover">, else the first picture near the start.
    cover = next((m["name"] for m in manifest.values() if "cover-image" in m["props"]), None)
    if cover is None:
        md = next((e for e in pkg if _local(e.tag) == "metadata"), None)
        cid = next(
            (
                e.get("content")
                for e in (md.iter() if md is not None else [])
                if _local(e.tag) == "meta" and e.get("name") == "cover"
            ),
            None,
        )
        if cid in manifest:
            cover = manifest[cid]["name"]
    if cover is not None and (cover not in names or not cover.lower().endswith(IMAGE_EXTS)):
        cover = None
    if cover is None:
        cover = next((imgs[0] for imgs in pictures[:2] if imgs), None)

    book = EpubBook(
        kind=kind, meta=meta, spine=spine, images=images if kind == "epub-image" else [], cover=cover
    )
    book.toc = _toc(zf, names, manifest, spine_el, spine)
    return book


def _toc(zf, names, manifest, spine_el, spine) -> list[dict]:
    index = {name: n for n, name in enumerate(spine)}
    out: list[dict] = []

    def add(base: str, href: str, title: str, level: int):
        r = resolve(base, href)
        if r and r[0] in index and title:
            out.append({"title": title[:300], "n": index[r[0]], "frag": r[1], "level": min(level, 6)})

    nav = next((m for m in manifest.values() if "nav" in m["props"] and m["name"] in names), None)
    if nav:
        try:
            scan = _NavScan()
            scan.feed(decode(_read(zf, nav["name"], DOC_MAX)))
            scan.close()
            for href, title, level in scan.items:
                add(posixpath.dirname(nav["name"]), href, title, level)
        except EpubError:
            pass
    if out:
        return out
    ncx = manifest.get(spine_el.get("toc") or "") or next(
        (m for m in manifest.values() if m["type"] == "application/x-dtbncx+xml"), None
    )
    if ncx and ncx["name"] in names:
        try:
            root = safe_xml(_read(zf, ncx["name"], XML_MAX))
        except EpubError:
            return out

        def walk(el, level):
            for p in el:
                if _local(p.tag) != "navPoint":
                    continue
                label = next((t for t in p.iter() if _local(t.tag) == "text"), None)
                content = next((c for c in p if _local(c.tag) == "content"), None)
                if content is not None and content.get("src"):
                    title = " ".join((label.text or "").split()) if label is not None else ""
                    add(posixpath.dirname(ncx["name"]), content.get("src"), title, level)
                walk(p, level + 1)

        nav_map = next((e for e in root if _local(e.tag) == "navMap"), None)
        if nav_map is not None:
            walk(nav_map, 0)
    return out


# ---------------------------------------------------------------- serving chapters

_ATTR = re.compile(r"""(\s(?:src|href|xlink:href|poster)\s*=\s*)(["'])(.*?)\2""", re.I | re.S)
_CSS_URL = re.compile(r"""url\(\s*(["']?)([^"')]*)\1\s*\)""", re.I)
_CSS_IMPORT = re.compile(r"""@import\s+(["'])([^"']*)\1""", re.I)
_DROP = [
    re.compile(r"<script\b.*?</script\s*>", re.I | re.S),
    re.compile(r"<script\b[^>]*/>", re.I),
    re.compile(r"<(?:base|meta\s[^>]*http-equiv)[^>]*>", re.I),
    re.compile(r"<(iframe|object|embed|form)\b.*?</\1\s*>", re.I | re.S),
    re.compile(r"<(?:iframe|object|embed)\b[^>]*/?>", re.I),
]
_EVENT_ATTR = re.compile(r"""\son[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)


def rewrite_css(css: str, base_dir: str, url_for) -> str:
    def fix(m, quote_group=1, url_group=2, fmt="url({q}{u}{q})"):
        u = m.group(url_group)
        r = resolve(base_dir, u) if not u.startswith(("data:", "#")) else None
        q = m.group(quote_group)
        if r is None:
            return m.group(0) if u.startswith(("data:", "#")) else fmt.format(q=q or '"', u="")
        return fmt.format(q=q or '"', u=url_for(r[0], r[1]))

    css = _CSS_URL.sub(fix, css)
    return _CSS_IMPORT.sub(lambda m: fix(m, 1, 2, "@import {q}{u}{q}"), css)


def rewrite_chapter(text: str, name: str, url_for) -> str:
    """A chapter with its links pointing at the library's resource URLs, and active content removed.

    ``url_for(zip_name, fragment)`` builds the URL. Links that leave the EPUB are kept as they are
    (the page opens them in a new tab); the chapter's Content-Security-Policy stops it loading them.
    """
    base = posixpath.dirname(name)
    for pat in _DROP:
        text = pat.sub("", text)
    text = _EVENT_ATTR.sub("", text)
    text = _XML_DECL.sub('<?xml version="1.0" encoding="utf-8"?>', text, count=1)

    def fix(m):
        value = html.unescape(m.group(3))
        if value.startswith("#") or urlsplit(value).scheme or value.startswith("//"):
            if value.lower().startswith(("javascript:", "vbscript:", "data:text")):
                return f"{m.group(1)}{m.group(2)}#{m.group(2)}"
            return m.group(0)
        r = resolve(base, value)
        if r is None:
            return f"{m.group(1)}{m.group(2)}#{m.group(2)}"
        return f"{m.group(1)}{m.group(2)}{html.escape(url_for(r[0], r[1]), quote=True)}{m.group(2)}"

    text = _ATTR.sub(fix, text)
    return rewrite_css(text, base, url_for) if "url(" in text or "@import" in text else text


def well_formed_xml(text: str) -> bool:
    """Whether a chapter can be served as XHTML. Named HTML entities like &nbsp; make it plain HTML."""
    if "<!ENTITY" in text:
        return False
    try:
        ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return False
    return True

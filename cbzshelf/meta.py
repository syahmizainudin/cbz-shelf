"""Book metadata: ComicInfo.xml inside the archive, and numbers parsed from file names.

Metadata is a dict with these keys (all optional):

    title, series, number, volume, year, publisher, summary, language  (text or number)
    authors, tags, genres                                               (lists of text)
    rtl                                                                 (bool: read right to left)

Two layers are stored per book: ``file`` (rebuilt from the archive on every scan) and ``user``
(edits, never touched by a scan). ``effective`` merges them, the user's value winning.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

TEXT_FIELDS = ("title", "series", "publisher", "summary", "language")
NUMBER_FIELDS = ("number", "volume", "year")
LIST_FIELDS = ("authors", "tags", "genres")
BOOL_FIELDS = ("rtl",)
FIELDS = TEXT_FIELDS + NUMBER_FIELDS + LIST_FIELDS + BOOL_FIELDS

COMICINFO_MAX = 512 * 1024  # bytes; real files are a few KB
AUTHOR_ROLES = ("Writer", "Penciller", "Inker", "Colorist", "Letterer", "CoverArtist", "Editor")


# ---------------------------------------------------------------- ComicInfo.xml


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    out: list[str] = []
    for part in re.split(r"[,;]", value):
        part = part.strip()
        if part and part.lower() not in (p.lower() for p in out):
            out.append(part)
    return out


def _num(value: str | None) -> float | None:
    try:
        n = float((value or "").strip())
    except ValueError:
        return None
    return int(n) if n.is_integer() else n


def parse_comicinfo(data: bytes) -> tuple[dict, int | None]:
    """Metadata and the front-cover page index from a ComicInfo.xml. Bad or unsafe XML gives ({}, None)."""
    if len(data) > COMICINFO_MAX or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return {}, None  # entity tricks have no place in ComicInfo; refuse rather than expand
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return {}, None

    def text(tag: str) -> str | None:
        el = root.find(tag)
        return el.text.strip() if el is not None and el.text and el.text.strip() else None

    meta: dict[str, Any] = {}
    for field, tag in (
        ("title", "Title"),
        ("series", "Series"),
        ("publisher", "Publisher"),
        ("summary", "Summary"),
        ("language", "LanguageISO"),
    ):
        if (v := text(tag)) is not None:
            meta[field] = v
    for field, tag in (("number", "Number"), ("volume", "Volume"), ("year", "Year")):
        if (v := _num(text(tag))) is not None:
            meta[field] = v
    authors: list[str] = []
    for role in AUTHOR_ROLES:
        for name in _split(text(role)):
            if name.lower() not in (a.lower() for a in authors):
                authors.append(name)
    if authors:
        meta["authors"] = authors
    if tags := _split(text("Tags")):
        meta["tags"] = tags
    if genres := _split(text("Genre")):
        meta["genres"] = genres
    if (manga := text("Manga")) is not None:
        meta["rtl"] = manga.lower() == "yesandrighttoleft"

    cover = None
    for page in root.iter("Page"):
        if page.get("Type", "").lower() == "frontcover" and (idx := _num(page.get("Image"))) is not None:
            cover = int(idx)
            break
    return meta, cover


# ---------------------------------------------------------------- file names

_NUM = r"(\d+(?:\.\d+)?)"
_VOLUME = re.compile(r"(?<![A-Za-z])(?:volume|vol\.?|v)\s*" + _NUM, re.I)
_CHAPTER = re.compile(r"(?:(?<![A-Za-z])(?:chapter|chap\.?|ch\.?|c)\s*|#\s*)" + _NUM, re.I)
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}")


def parse_filename(stem: str) -> dict:
    """Volume and chapter/issue number from a file name (without extension).

    ``Naruto v01`` -> volume 1; ``One Piece c012`` / ``Chapter 12`` / ``#12`` -> number 12;
    with neither marker, the last number outside brackets is the number (``Saga 054 (2018)`` -> 54).
    """
    meta: dict[str, Any] = {}
    if m := _VOLUME.search(stem):
        meta["volume"] = _num(m.group(1))
    if m := _CHAPTER.search(stem):
        meta["number"] = _num(m.group(1))
    if "volume" not in meta and "number" not in meta:
        nums = re.findall(_NUM, _BRACKETS.sub(" ", stem))
        if nums:
            meta["number"] = _num(nums[-1])
    return meta


_SERIES_TAIL = re.compile(r"[\s\-_#.,:;~]+$")


def guess_series(stem: str) -> str:
    """A series name from a file name, for grouping a compilation folder's books.

    Bracketed parts, the volume and chapter markers and the last bare number are dropped:
    ``Batman v01 (2016)`` and ``Batman - Chapter 12`` -> ``Batman``. A name with nothing left
    after that (``001``) is kept whole.
    """
    name = _BRACKETS.sub(" ", stem)
    name = _VOLUME.sub(" ", name)
    name = _CHAPTER.sub(" ", name)
    name = re.sub(r"[\s_]" + _NUM + r"\s*$", " ", " " + name.rstrip())
    name = _SERIES_TAIL.sub("", " ".join(name.split()))
    return name or stem.strip() or stem


# ---------------------------------------------------------------- layers


def clean_user_changes(changes: dict) -> dict:
    """Validate an edit: known fields, right types. ``None`` or ``""`` means "use the file's value"."""
    out: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in FIELDS:
            raise ValueError(f"unknown field: {key}")
        if value is None or value == "" or value == []:
            out[key] = None
        elif key in LIST_FIELDS:
            items = value if isinstance(value, list) else _split(str(value))
            out[key] = [str(i).strip() for i in items if str(i).strip()][:100]
        elif key in NUMBER_FIELDS:
            n = _num(str(value))
            if n is None:
                raise ValueError(f"{key} must be a number")
            out[key] = n
        elif key in BOOL_FIELDS:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
            out[key] = value
        else:
            out[key] = str(value).strip()[:10000]
    return out


def apply_changes(user: dict, changes: dict) -> dict:
    """The user layer after an edit; ``None`` removes an override."""
    merged = dict(user)
    for key, value in clean_user_changes(changes).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def effective(file_meta: dict, user_meta: dict) -> dict:
    return {**file_meta, **user_meta}

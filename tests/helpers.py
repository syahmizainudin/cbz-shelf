"""Fixtures: build sample .cbz files and library folder trees."""

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image


def page(color, size=(60, 90), fmt="WEBP"):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, fmt)
    return b.getvalue()


def comicinfo(**fields):
    pages = fields.pop("pages_xml", "")
    body = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    return f'<?xml version="1.0"?><ComicInfo>{body}{pages}</ComicInfo>'.encode()


def make_cbz(path, n_pages=3, info=None, names=None, extra=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    colors = ["red", "green", "blue", "white", "black", "yellow", "purple", "orange", "gray", "pink", "cyan"]
    names = names or [f"{i + 1:03}.webp" for i in range(n_pages)]
    with zipfile.ZipFile(path, "w") as z:
        for i, name in enumerate(names):
            z.writestr(name, page(colors[i % len(colors)]))
        if info is not None:
            z.writestr("ComicInfo.xml", info)
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return path

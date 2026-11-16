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


CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
    "</rootfiles></container>"
)


def make_epub(
    path, kind="text", chapters=3, title="A Book", meta_extra="", rtl=False, nav=True, chapter_html=None
):
    """An EPUB 3 file. ``kind="images"``: one picture per spine page; ``"text"``: chapters of prose."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    items, spine, files = [], [], {}
    if kind == "images":
        for i in range(chapters):
            files[f"OEBPS/Images/p{i + 1:03}.png"] = page(["red", "green", "blue", "white"][i % 4], fmt="PNG")
            items.append(
                f'<item id="img{i}" href="Images/p{i + 1:03}.png" media-type="image/png"'
                + (' properties="cover-image"' if i == 0 else "")
                + "/>"
            )
            files[f"OEBPS/Text/p{i + 1:03}.xhtml"] = (
                '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>p</title>'
                f'</head><body><div><img src="../Images/p{i + 1:03}.png" alt="page"/></div></body></html>'
            )
            items.append(
                f'<item id="p{i}" href="Text/p{i + 1:03}.xhtml" media-type="application/xhtml+xml"/>'
            )
            spine.append(f'<itemref idref="p{i}"/>')
    else:
        files["OEBPS/Styles/book.css"] = (
            'body { font-family: serif } .x { background: url("../Images/bg.png") }'
        )
        files["OEBPS/Images/bg.png"] = page("gray", fmt="PNG")
        files["OEBPS/Images/cover.png"] = page("purple", fmt="PNG")
        items += [
            '<item id="css" href="Styles/book.css" media-type="text/css"/>',
            '<item id="bg" href="Images/bg.png" media-type="image/png"/>',
            '<item id="cover" href="Images/cover.png" media-type="image/png" properties="cover-image"/>',
        ]
        for i in range(chapters):
            body = (
                chapter_html(i)
                if chapter_html
                else (
                    f'<h1 id="c{i}">Chapter {i + 1}</h1>'
                    + "<p>"
                    + ("Words of the story. " * 60)
                    + "</p>"
                    + (
                        f'<p><a href="ch{i + 2}.xhtml#c{i + 1}">next</a> <a href="https://example.com/">site</a></p>'
                    )
                )
            )
            files[f"OEBPS/Text/ch{i + 1}.xhtml"] = (
                '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head>'
                '<title>c</title><link rel="stylesheet" href="../Styles/book.css"/></head>'
                f"<body>{body}</body></html>"
            )
            items.append(f'<item id="ch{i}" href="Text/ch{i + 1}.xhtml" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="ch{i}"/>')
    if nav:
        links = "".join(
            f'<li><a href="{f"p{i + 1:03}" if kind == "images" else f"ch{i + 1}"}.xhtml">Part {i + 1}</a></li>'
            for i in range(chapters)
        )
        files["OEBPS/Text/nav.xhtml"] = (
            '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>nav</title></head><body>'
            f'<nav epub:type="toc"><ol>{links}</ol></nav></body></html>'
        )
        items.append(
            '<item id="nav" href="Text/nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title>{meta_extra}</metadata>"
        f"<manifest>{''.join(items)}</manifest>"
        f"<spine{' page-progression-direction=' + chr(34) + 'rtl' + chr(34) if rtl else ''}>{''.join(spine)}</spine>"
        "</package>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", opf)
        for k, v in files.items():
            z.writestr(k, v)
    return path

# cbz-shelf

[![CI](https://github.com/syahmizainudin/cbz-shelf/actions/workflows/ci.yml/badge.svg)](https://github.com/syahmizainudin/cbz-shelf/actions/workflows/ci.yml)

`cbz-shelf` is a library and reader for `.cbz` comics and manga, and for EPUB books, that runs on your own computer. Add a top-level folder, and it scans every book inside, groups the archives into series, and indexes their metadata for search. You then browse and read in the browser.

## Install

Needs Python 3.10 or later.

```bash
pipx install git+https://github.com/syahmizainudin/cbz-shelf
```

This also installs [cbz-webp](https://github.com/syahmizainudin/cbz-webp), whose web server it uses, and Pillow. Each [release](https://github.com/syahmizainudin/cbz-shelf/releases) also has a wheel.

## Use

```bash
cbz-shelf add /mnt/d/Comics      # add a folder and scan it
cbz-shelf ui --open              # http://127.0.0.1:8767/
```

| Command | What it does |
|---|---|
| `cbz-shelf ui [-p 8767] [--open] [--no-scan]` | Starts the library and reader. Rescans all folders in the background when it starts, unless you pass `--no-scan` |
| `cbz-shelf add <folder>` | Adds a folder and scans it |
| `cbz-shelf scan [<folder>]` | Rescans every library folder, or one, for new, changed and missing books |
| `cbz-shelf roots` | Lists the library's folders |
| `cbz-shelf search <words>` | Searches titles, series, authors, tags and summaries |

`--db PATH` (or `$CBZ_SHELF_DB`) picks the database. The default is `~/.local/share/cbz-shelf/library.db`. Covers are cached in `~/.cache/cbz-shelf/thumbs` and can be deleted at any time; they are rebuilt as needed. Windows paths such as `D:\Comics` work in WSL.

## How the library is built

- **Books:** every `.cbz`, `.zip` and `.epub` under a library folder, including subfolders. Hidden folders are skipped. `.cbr` (RAR) files aren't supported; convert them to `.cbz` first.
- **Series:** each folder of archives is a series named after the folder. If an archive's `ComicInfo.xml` has a `Series`, that wins, so two files in one folder can belong to different series. Books are ordered by volume, then number, then file name (with `2` before `10`).
- **Numbers:** taken from `ComicInfo.xml`, or else from the file name:

  | File name | Read as |
  |---|---|
  | `v01`, `Vol. 3`, `Volume 41` | volume |
  | `c012`, `ch 7`, `Chapter 12.5`, `#12` | number |
  | `Saga 054 (2018)` | number 54 (bracketed parts are ignored) |

- **Metadata:** read from `ComicInfo.xml`:
  - title, series, number, volume, year, publisher, summary, language;
  - authors (Writer, Penciller, Inker, Colorist, Letterer, CoverArtist and Editor, merged);
  - tags and genres;
  - `Manga=YesAndRightToLeft`, which sets the reading direction to right to left;
  - the front-cover page.
- **Pages:** the archive's images in natural order. `__MACOSX`, `.DS_Store` and other junk are skipped.
- **Rescans** only reopen archives whose size or modification time changed.

### EPUB books

- **Picture EPUBs** (manga and comics, where every page is one picture) open in the comic reader, with all its layouts. `page-progression-direction="rtl"` makes them read right to left.
- **Text EPUBs** (novels) open in the text reader, one chapter at a time.
- **Metadata** comes from the book's package file: title, authors, subjects (as tags), publisher, language, date (the year), description, and the series and number from calibre's `calibre:series` or EPUB 3's `belongs-to-collection`.
- **Covers:** the book's cover image, or else a plain cover with the title.

### Your edits and your files

- Edit any book's title, numbers, year, authors, tags, genres, publisher, language, summary or reading direction from **details**. Each field shows the file's value in grey. Type to override it, or empty the field to go back to the file's value.
- Series can be renamed and given authors, tags and a summary. The reader remembers its layout, fit and direction per series.
- Edits and reading progress are stored only in the database. **cbz-shelf never writes to your archives.** A rescan updates the file's metadata but never your edits.
- If a file disappears, its book is marked *missing* and keeps your edits and progress. Moving it back restores it. **Folders → Forget missing books** removes them for good.
- **Folders → Remove** forgets a folder and everything recorded about it. The files stay on disk.

## The page

- **Home** shows *Continue reading*, your *Favourites* and your series, with filters for tag and author and sorting by name, recently read or recently added. Search covers titles, series names, authors, tags, summaries and file names, with each word matched as a prefix.
- **Favourites:** click the ☆ on any series or book cover (or **☆ Favourite** on a series page or in a book's details). Starred series and books get their own row on Home, and **★ Favourites** next to the filters shows only them.
- **Continue reading:** the × on a card takes it out of the row, and **Clear all** empties the row. Your page is kept, and a book comes back as soon as you read it again.
- **A series** shows its books, with progress bars and a Read or Continue button.
- **Folders** adds library folders with a folder browser, and rescans or removes them.

## The reader

| | |
|---|---|
| Layouts | Single page · Two pages (the cover alone, then pairs) · Vertical scroll (webtoon) |
| Direction | Left to right, or right to left. In right-to-left books, the left side and the left arrow go forward, and spreads show the higher page on the left |
| Fit | Height · Width · Original size |
| Keys | ← → turn pages (following the direction) · Space / Shift+Space · Page Up / Down · Home / End · F full screen · Esc back |
| Mouse and touch | Click or tap the left or right third to turn pages, and the middle to hide the bars. Swipe to turn pages |

Your page is saved as you read. At the end of a book, the reader offers the next one in the series.

### The text reader

| | |
|---|---|
| Contents | The book's table of contents, to jump to a chapter |
| End of a chapter | A **Next** button with the next chapter's title, or **Finish** on the last one |
| Text | A− / A+ text size · Narrow, Medium or Wide lines · the book's colours, Light, Sepia or Dark |
| Turning | Click or tap the right third of the page, or ›, → , Space or Page Down, for the next screen; the left third, ‹, ←, Shift+Space or Page Up to go back. At the end of a chapter this goes on to the next one. Swipe left or right too. Click the middle to hide the bars |
| Keys | ↑ ↓ scroll a little · Home / End first or last chapter · F full screen · Esc back |
| Links | Links to other chapters open them here. Links to websites open in a new tab |

Your chapter and how far down it you are are saved as you read. Text settings are remembered in this browser.

Chapters are shown in a frame that runs no scripts, and the server sends them with a Content-Security-Policy that blocks scripts, forms and anything from outside, so a book can't run code or contact other sites.

## Using it inside another tool

`cbzshelf.web.LibraryAPI(library)` is the library as an API for [cbz-webp's console server](https://github.com/syahmizainudin/cbz-webp#using-the-console-in-another-tool), mounted at `/api/library/`, and `cbzshelf.web.UI_HTML` is the page. [harbor](https://github.com/syahmizainudin/harbor) uses them for its Library tab.

When the page runs in a frame, the reader posts `{type: "shelf-reader", open: true}` to the parent page (same origin only) when a book opens, and `open: false` when it closes, so the host can give the reader the whole window.

## Development

```bash
pip install --target .deps .                         # cbz-webp (pinned) and Pillow
PYTHONPATH=.deps python3 -m unittest discover -s tests
ruff check . && ruff format --check .
```

GitHub Actions runs lint, the tests on Python 3.10 to 3.14, and a package smoke test on every push to `main` and every pull request. Pushing a `v<version>` tag publishes a release. [AGENTS.md](AGENTS.md) has the rules for changing the code.

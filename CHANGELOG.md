# Changelog

Each release has a `## <version>` section. The release pipeline publishes that section as the GitHub release notes, and refuses to release a version without one.

## Unreleased

## 0.2.1

- Text reader: click or tap the left or right third of the page to go back or forward a screen, as in the comic reader, and the middle to hide the bars. ‹ ›, ← → and swipes also turn a screen, going on to the next or previous chapter at the ends. Contents and the slider still jump between chapters.

## 0.2.0

- EPUB books:
  - Picture EPUBs (manga, comics) open in the comic reader, right to left when the book says so.
  - Text EPUBs open in a new text reader: contents, text size, line width, the book's colours or Light, Sepia or Dark, and progress saved by chapter and position.
  - Title, authors, subjects, publisher, language, year, description and series are read from the book. A book without a cover picture gets a plain cover with its title.
  - Chapters can't run scripts or contact other sites: they are sent with a strict Content-Security-Policy and shown in a sandboxed frame.
- The database gets versioned migrations. Opening an older library adds the new columns and keeps everything in it.
- In a frame, the reader tells the parent page when a book opens and closes (`postMessage` `{type: "shelf-reader", open}`, same origin), so harbor can give it the whole window.
- A series with one book no longer shows its cover twice: the series page's top cover is left out when the book's card already shows it.

## 0.1.1

- The Folders window keeps one size: the folder browser fills it and scrolls inside, and the list of library folders scrolls in its own area, instead of the window growing and shrinking with every folder.
- Scrollbars and form controls follow the dark theme.

## 0.1.0

First release.

- **Library:** add folders and every `.cbz`/`.zip` inside them is indexed into SQLite. Each folder is a series, unless `ComicInfo.xml` names one. Volume and chapter numbers come from `ComicInfo.xml` or the file name. Rescans are incremental (size and modification time); files that disappear are marked missing and keep their edits and progress.
- **Metadata:** title, authors, tags, genres, volume, number, year, publisher, language, summary and reading direction, read from `ComicInfo.xml` and editable per book and per series. Edits live in the database; archives are never written.
- **Search:** full-text search (SQLite FTS5, prefix matching) over titles, series, authors, tags and summaries, plus tag and author filters.
- **Reader:** single page, two-page spread (cover alone; right-to-left swaps the pair), and vertical scroll. Right-to-left reading, fit height/width/original, keyboard, click zones, swipe, full screen, preloading. Reading progress and per-series reader settings are saved; "Continue reading" on the home page.
- `cbz-shelf ui`, `add`, `scan`, `roots`, `search`. Built on cbz-webp 0.3.0's console server.

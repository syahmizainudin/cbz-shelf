# Changelog

Each release has a `## <version>` section. The release pipeline publishes that section as the GitHub release notes, and refuses to release a version without one.

## Unreleased

- **OPDS catalog** at `/api/library/opds` for reader apps (Panels, Chunky, KOReader): browse all series or one library, search, stream comic and picture-EPUB pages (OPDS Page Streaming), and download any book whole. New action `file?book=` streams the file, with resume (`Range`). Both obey `visible`.
- **Folder paths only for managers:** `series` and `series-detail` leave out the series' folder unless the user has `library.manage`, so readers don't see where books are kept on the server.

## 0.5.1

- Pins cbz-webp 0.5.0 (the console can be reached from other devices when a host such as harbor asks; nothing changes standalone).

## 0.5.0

- **Libraries as tabs:** split the library into named libraries (Manga, Novels…), each holding one or more folders. They show as tabs above the filters, next to **All**, and a tab limits series, search, filters, Continue reading and Favourites to that library. **Libraries…** adds, renames, reorders and removes them, and **Folders** moves a folder to another library (keeping its books, edits and progress) and adds new folders to the library you pick. Migration 6 puts every existing folder in one library called "Library", so nothing changes until you add another. `cbz-shelf add --library NAME`, `cbz-shelf libraries`.
- **For hosts:** `LibraryAPI(visible=)` limits a request to some libraries. Books and series in the others are left out of every list and count and answer 404 by id. New actions `libraries` (GET), `add-library`, `rename-library`, `order-libraries`, `remove-library` and `move-root` (`library.manage`); `add-root` takes `library_id`; `series`, `search`, `continue`, `facets` and `clear-continue` take `library`. `/api/state` has `library_list`, and leaves out the path of the folder being scanned unless the user has `library.manage`.

## 0.4.3

- Pins cbz-webp 0.4.2.

## 0.4.2

- Faster scans: books are written 100 at a time, each batch in one transaction, instead of three or four commits per book. Files are still read with no lock held.
- Migration 5 adds two indexes (`books(missing, root_id)`, `books(series_id, missing, sort_key)`) for the counts `/api/state` reports and for series covers. It adds nothing else.
- A text EPUB stops being read for classification once it is known to be a text book.
- A changed book's old cached cover is deleted when the scan sees the change, instead of the cache folder being searched on every new cover.
- Pages, chapters and EPUB files with `&v=` are sent with `Cache-Control: private, max-age=31536000, immutable`. Covers keep one day, since a placeholder cover follows the title.
- The page asks `/api/state` every 5 s (1.5 s during a scan), one request at a time, and not at all while the tab is hidden, the reader is open, or harbor shows another tab.
- Starring a book or series, or removing one from Continue reading, redraws only that row, keeping "Show more" pages and the scroll position.
- Whether a chapter is well-formed XHTML is remembered per chapter.

## 0.4.1

- The text reader's size, line width and colours are saved per user in the library database (endpoint `prefs`, migration 4), so they follow you to another browser. This browser's copy is used until they load.

## 0.4.0

- **Permissions for hosts with accounts:** `LibraryAPI(..., can=...)`. Editing book and series details needs `library.edit`; folders, scans and forgetting missing books need `library.manage`; everything else needs `library.read`. Refused actions get 403, and the page hides **Folders**, **Edit series** and the details form for users without them. Standalone, everything is allowed as before.
- **Reader settings per user:** layout, fit and direction chosen in the reader are saved for you only (new endpoint `reader-settings`, migration 3). The shared series settings stay the default.
- Depends on cbz-webp 0.4.1.

## 0.3.2

- Depends on cbz-webp 0.4.0 (was 0.3.0), so it installs alongside harbor, which needs 0.4.0's sign-in support. No other change.

## 0.3.1

- `Library.give_user_data(from_user, to_user)` moves progress and favourites to another user (harbor gives the one user's data to its first account).
- Inside a host with accounts, an expired session sends the page to the host's sign-in page.

## 0.3.0

- **Favourites:** star series and books. Home gets a Favourites row, and a **★ Favourites** filter shows only them. Endpoint `favourite` (`kind`, `id`, `on`); `series` and `search` take `favourite=1`.
- **Continue reading:** × on a card hides it and **Clear all** empties the row, keeping your pages; reading a book again brings it back. Endpoints `hide-continue` and `clear-continue`.
- Progress, Continue reading and favourites are kept per user (migration 2). Without accounts everything is user 0, as before. `LibraryAPI(library, user_of)` lets a host with accounts say who is signed in.

## 0.2.1

- Text reader: click or tap the left or right third of the page to go back or forward a screen, as in the comic reader, and the middle to hide the bars. ‹ ›, ← → and swipes also turn a screen, going on to the next or previous chapter at the ends. Contents and the slider still jump between chapters.
- Text reader: each chapter ends with the reader's own footer and a **Next: <chapter>** button (or **Finish** on the last one), so moving on doesn't depend on links in the book.

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

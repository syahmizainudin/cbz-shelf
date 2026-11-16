# Changelog

Each release has a `## <version>` section. The release pipeline publishes that section as the GitHub release notes, and refuses to release a version without one.

## Unreleased

## 0.1.0

First release.

- **Library:** add folders and every `.cbz`/`.zip` inside them is indexed into SQLite. Each folder is a series, unless `ComicInfo.xml` names one. Volume and chapter numbers come from `ComicInfo.xml` or the file name. Rescans are incremental (size and modification time); files that disappear are marked missing and keep their edits and progress.
- **Metadata:** title, authors, tags, genres, volume, number, year, publisher, language, summary and reading direction, read from `ComicInfo.xml` and editable per book and per series. Edits live in the database; archives are never written.
- **Search:** full-text search (SQLite FTS5, prefix matching) over titles, series, authors, tags and summaries, plus tag and author filters.
- **Reader:** single page, two-page spread (cover alone; right-to-left swaps the pair), and vertical scroll. Right-to-left reading, fit height/width/original, keyboard, click zones, swipe, full screen, preloading. Reading progress and per-series reader settings are saved; "Continue reading" on the home page.
- `cbz-shelf ui`, `add`, `scan`, `roots`, `search`. Built on cbz-webp 0.3.0's console server.

"""Command line: ``cbz-shelf ui``, ``add``, ``scan``, ``roots`` and ``search``."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import webbrowser

from . import __version__
from .library import Library, default_db


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cbz-shelf", description="A library and reader for .cbz comics and EPUB books.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument(
        "--db",
        default=os.environ.get("CBZ_SHELF_DB") or str(default_db()),
        help="library database (default $CBZ_SHELF_DB or ~/.local/share/cbz-shelf/library.db)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("ui", help="start the library and reader in your browser")
    u.add_argument("-p", "--port", type=int, default=8767, help="port on 127.0.0.1 (default 8767)")
    u.add_argument("--open", action="store_true", help="open it in a browser")
    u.add_argument("--no-scan", action="store_true", help="don't rescan the folders at start-up")

    a = sub.add_parser("add", help="add a folder to the library and scan it")
    a.add_argument("folder")
    s = sub.add_parser("scan", help="rescan every folder (or one) for new, changed and missing books")
    s.add_argument("folder", nargs="?", help="only this library folder")
    sub.add_parser("roots", help="list the library's folders")
    q = sub.add_parser("search", help="search titles, series, authors and tags")
    q.add_argument("words", nargs="+")
    return ap


def _print_scan(lib: Library) -> None:
    last = lib.scan_state.get("last") or {}
    print(
        f"{last.get('added', 0)} added, {last.get('updated', 0)} updated, {last.get('missing', 0)} missing"
        + (f", {last['errors']} unreadable" if last.get("errors") else "")
        + f". Library: {lib.summary()['series']} series, {lib.summary()['books']} books."
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lib = Library(args.db)
    try:
        if args.cmd == "ui":
            return cmd_ui(lib, args)
        if args.cmd == "add":
            root = lib.add_root(args.folder)
            print(f"Added {root['path']}; scanning…", flush=True)
            lib.wait_scan()
            _print_scan(lib)
        elif args.cmd == "scan":
            target = None
            if args.folder:
                from cbzwebp.core import normalize_path

                path = str(normalize_path(args.folder))
                match = [r for r in lib.roots() if r["path"] == path]
                if not match:
                    raise ValueError(f"not a library folder: {path} (see `cbz-shelf roots`)")
                target = match[0]["id"]
            lib.scan(target)
            lib.wait_scan()
            _print_scan(lib)
        elif args.cmd == "roots":
            roots = lib.roots()
            for r in roots:
                missing = f", {r['missing']} missing" if r["missing"] else ""
                print(f"{r['path']}  ({r['books']} books{missing})")
            if not roots:
                print("No folders yet: cbz-shelf add <folder>")
        elif args.cmd == "search":
            res = lib.search(" ".join(args.words), limit=50)
            for b in res["items"]:
                print(f"{b['title']}  [{', '.join(b['authors'])}]  {b['file']}")
            print(f"{res['total']} match(es).")
    except (ValueError, OSError) as e:
        print(f"cbz-shelf: {e}", file=sys.stderr)
        return 2
    finally:
        lib.close()
    return 0


def cmd_ui(lib: Library, args) -> int:
    from .web import serve_shelf

    try:
        server = serve_shelf(args.port, lib)
    except OSError as e:
        print(f"cbz-shelf: cannot listen on 127.0.0.1:{args.port}: {e}", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{args.port}/"
    print(f"cbz-shelf: {url}  (Ctrl+C to stop)", flush=True)
    if not args.no_scan and lib.roots():
        lib.scan()  # pick up what changed while it was stopped; runs in the background

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

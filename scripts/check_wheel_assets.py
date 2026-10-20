#!/usr/bin/env python3
"""Fail when a built talk2view wheel or sdist does not carry the chat page correctly.

    python scripts/check_wheel_assets.py dist/*.whl dist/*.tar.gz

The page (``talk2view/ui/static/``) is git-ignored and reaches the archives only through
hatch ``artifacts``, so a build from a tree without it, or a packaging change, can ship a
UI server with no page. Checked for every archive:

* no entry appears twice (a hatch ``force-include`` of an in-package directory double-adds
  every file, which breaks installs);
* ``index.html`` is there, it is the page ``embed/`` builds (its
  ``<meta name="talk2view-page">`` marker, which the Python tests' stub page lacks), it
  references at least one local asset, and every local asset it references
  (``src=``/``href=``) is there too, inside the bundle;
* a wheel lists every static file in its ``RECORD``.

The sdist matters as much as the wheel: ``uv build`` and ``pip install <sdist>`` build the
wheel from it. Standard library only; exit status 1 on any problem.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
import tarfile
import zipfile
from collections import Counter
from html.parser import HTMLParser
from typing import Callable, List, Optional, Sequence, Tuple

WHEEL_STATIC = "talk2view/ui/static/"
SDIST_STATIC = "src/talk2view/ui/static/"
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
#: The built page's marker (embed/index.html; check-dist.mjs demands it too).
PAGE_MARKER = re.compile(r"<meta\s[^>]*name=[\"']talk2view-page[\"']", re.IGNORECASE)


class _References(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        for name, value in attrs:
            if name in ("src", "href") and value:
                self.found.append(value)


def local_references(html: str) -> List[str]:
    """Bundle-relative paths of the local files ``html`` references (``..`` kept, to flag)."""
    parser = _References()
    parser.feed(html)
    out: List[str] = []
    for ref in parser.found:
        if _SCHEME.match(ref) or ref.startswith(("//", "#")):
            continue  # another origin, data:, or a fragment
        path = ref.split("#", 1)[0].split("?", 1)[0].lstrip("/")
        if path:
            out.append(posixpath.normpath(path))
    return out


def check_entries(names: Sequence[str], static: str, read: Callable[[str], bytes]) -> List[str]:
    problems: List[str] = []
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        problems.append("duplicate entries: " + ", ".join(duplicates))
    present = set(names)
    index = static + "index.html"
    if index not in present:
        problems.append("missing {} (build the chat page before packaging)".format(index))
        return problems
    html = read(index).decode("utf-8", "replace")
    if not PAGE_MARKER.search(html):
        problems.append(
            "{} is not the page embed/ builds (no talk2view-page marker): "
            "the test stub, or an old build".format(index)
        )
    references = local_references(html)
    if not references:
        problems.append("{} references no local asset: not a built page".format(index))
    for ref in references:
        if ref == ".." or ref.startswith("../"):
            problems.append("{} references {} outside the bundle".format(index, ref))
        elif static + ref not in present:
            problems.append("missing {} (referenced by index.html)".format(static + ref))
    return problems


def check_wheel(path: str) -> List[str]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        problems = check_entries(names, WHEEL_STATIC, archive.read)
        records = [n for n in names if n.endswith(".dist-info/RECORD")]
        if len(records) != 1:
            problems.append("expected exactly one .dist-info/RECORD, found {}".format(len(records)))
            return problems
        listed = {
            line.split(",", 1)[0]
            for line in archive.read(records[0]).decode("utf-8").splitlines()
            if line
        }
        for name in sorted(set(names)):
            if name.startswith(WHEEL_STATIC) and name not in listed:
                problems.append("{} is not listed in RECORD".format(name))
    return problems


def check_sdist(path: str) -> List[str]:
    with tarfile.open(path, "r:gz") as archive:
        members = {}
        names: List[str] = []
        for member in archive.getmembers():
            if not member.isfile():
                continue
            name = member.name.split("/", 1)[1] if "/" in member.name else member.name
            names.append(name)
            members[name] = member

        def read(name: str) -> bytes:
            handle = archive.extractfile(members[name])
            return handle.read() if handle is not None else b""

        return check_entries(names, SDIST_STATIC, read)


def check_archive(path: str) -> List[str]:
    if path.endswith(".whl"):
        return check_wheel(path)
    if path.endswith(".tar.gz"):
        return check_sdist(path)
    return ["not a wheel or an sdist: {}".format(path)]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archives", nargs="+", help="built .whl / .tar.gz files")
    args = parser.parse_args(argv)
    failed = False
    for path in args.archives:
        problems = check_archive(path)
        if problems:
            failed = True
            for problem in problems:
                print("FAIL {}: {}".format(path, problem))
        else:
            print("OK   {}".format(path))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

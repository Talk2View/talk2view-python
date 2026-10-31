"""Smoke-test an installed talk2view: it imports, says its version, serves its chat page, and
opens an on-device memory store (``talk2view.memory``) on whichever backend runs there.

CI runs it against the built wheel in a clean virtual environment, from outside the
checkout, on Linux, macOS and Windows. Stdlib only (plus talk2view and its dependencies).

    python scripts/smoke_installed.py
"""

from __future__ import annotations

import importlib.metadata
import sys
import tempfile
import urllib.request
from pathlib import Path

import talk2view
from talk2view.client import SDK_VERSION
from talk2view.memory import MemoryStore
from talk2view.ui import GenericHost, UIServer
from talk2view.ui.static import require_static_dir
from talk2view.ui.testing import FakeController

PAGE_MARKER = b'name="talk2view-page"'


def check(ok: bool, what: object) -> None:
    if not ok:
        raise SystemExit("smoke test failed: {}".format(what))


def main() -> int:
    installed = importlib.metadata.version("talk2view")
    versions = (talk2view.__version__, installed, SDK_VERSION)
    check(len(set(versions)) == 1, "versions disagree: {}".format(versions))
    check("site-packages" in talk2view.__file__, "imported from " + talk2view.__file__)

    static = require_static_dir()
    check(PAGE_MARKER in (static / "index.html").read_bytes(), "not the built chat page")

    server = UIServer(FakeController(), GenericHost())
    server.start()
    try:
        with urllib.request.urlopen(  # noqa: S310 - a loopback URL this script started
            "http://127.0.0.1:{}/".format(server.port), timeout=10
        ) as response:
            check(response.status == 200, "GET / answered {}".format(response.status))
            check(PAGE_MARKER in response.read(), "the server did not serve the chat page")
    finally:
        server.stop()

    # The wheel carries the memory subpackage, and a store opens and closes on a backend here.
    with tempfile.TemporaryDirectory() as directory:
        store = MemoryStore(Path(directory) / "smoke.sqlite3")
        try:
            backend = store.backend
            check(store.entries() == [], "a new memory store is not empty")
        finally:
            store.close()

    print(
        "talk2view {} on Python {}: imports, has its page, serves it, opens a memory store"
        " ({} backend)".format(installed, sys.version.split()[0], backend)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

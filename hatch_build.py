"""Refuse to build a wheel or sdist without the chat page.

``talk2view.ui`` serves a page built from ``embed/`` into ``src/talk2view/ui/static/``.
That directory is git-ignored and carried into archives by hatch ``artifacts``, so a
build from a fresh checkout would otherwise succeed and ship a UI that cannot start.
Editable installs (``uv sync``, ``pip install -e .``) are not archives and are left alone:
``UIServer`` says how to build the page when it is missing there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

PAGE = Path("src") / "talk2view" / "ui" / "static" / "index.html"


class ChatPageHook(BuildHookInterface):  # type: ignore[type-arg]
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            return
        if not (Path(self.root) / PAGE).is_file():
            raise RuntimeError(
                f"{PAGE} is missing, so this {self.target_name} would ship without the chat "
                "page. Build it first: cd embed && npm ci && npm run build"
            )

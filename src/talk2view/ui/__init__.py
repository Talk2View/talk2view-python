"""talk2view.ui — the localhost chat UI server (standard library only).

A web view (3D Slicer's ``qSlicerWebWidget``, a ``QWebEngineView``, a system browser)
loads the bundled chat page from ``http://127.0.0.1:<port>/#t=<token>``; the page reads the
state as SSE op batches and sends commands back. Python owns every piece of state.
Nothing in ``talk2view`` imports this package: it is opt-in.
"""

from __future__ import annotations

from .app_state import AppState
from .host import GenericHost, HostAdapter, Theme
from .options import ChatFeatures, ChatOptions, ChatWelcome, ToolPresentation
from .server import UIServer
from .static import StaticAssetsMissingError

__all__ = [
    "AppState",
    "ChatFeatures",
    "ChatOptions",
    "ChatWelcome",
    "ToolPresentation",
    "GenericHost",
    "HostAdapter",
    "StaticAssetsMissingError",
    "Theme",
    "UIServer",
]

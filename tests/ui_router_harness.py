"""A CommandRouter over a never-started Talk2View, for skills command tests."""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from talk2view import Talk2View
from talk2view.skill_store import FolderSkillStore
from talk2view.storage import MemoryStorage
from talk2view.ui.app_state import AppState
from talk2view.ui.host import GenericHost
from talk2view.ui.router import CommandRouter, UploadRegistry
from talk2view.ui.testing import FakeController


class RecordingHost(GenericHost):
    supports_clipboard = False

    def __init__(self) -> None:
        super().__init__()
        self.opened: List[str] = []

    def post(self, fn: Callable[[], None]) -> None:
        fn()

    def open_external(self, url: str) -> Any:
        self.opened.append(url)
        return True


def make_router(
    *,
    skills_dir: Optional[Path],
    bundled: Sequence[str] = (),
    pick: Optional[Callable[[], Optional[str]]] = None,
    host: Optional[GenericHost] = None,
):
    t2v = Talk2View(partner_key="pk_test", storage=MemoryStorage(), base_url="http://127.0.0.1:9")
    if skills_dir is not None:
        t2v.skills.use_store(FolderSkillStore(skills_dir), bundled=bundled)
    host = host if host is not None else RecordingHost()
    router = CommandRouter(
        FakeController(), AppState(t2v, host), host, UploadRegistry(), on_pick_skill_import=pick
    )
    router.host = host  # type: ignore[attr-defined]

    def run(command: str, args: Dict[str, Any]) -> Any:
        return router._handlers[command](args)  # validated args, as the loop passes them

    router.run = run  # type: ignore[attr-defined]
    return router, t2v

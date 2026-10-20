"""The wheel packaging guard: hatch ``artifacts`` config and scripts/check_wheel_assets.py."""

from __future__ import annotations

import importlib.util
import io
import tarfile
import warnings
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Dict, Iterable, Tuple

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
SCRIPT = PACKAGE / "scripts" / "check_wheel_assets.py"
STUB = Path(__file__).parent / "fixtures" / "ui_static"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_wheel_assets", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load()


def stub_files(prefix: str) -> Dict[str, bytes]:
    """The test stub page, as it is: what the checker must refuse."""
    return {
        prefix + str(path.relative_to(STUB)).replace("\\", "/"): path.read_bytes()
        for path in sorted(STUB.rglob("*"))
        if path.is_file()
    }


def page_files(prefix: str) -> Dict[str, bytes]:
    """The stub with the built page's marker added: the shape of a real build."""
    files = stub_files(prefix)
    index = prefix + "index.html"
    files[index] = files[index].replace(
        b"<head>", b'<head>\n<meta name="talk2view-page" content="sdk-embed">', 1
    )
    return files


def make_wheel(
    tmp_path: Path, files: Dict[str, bytes], extra: Iterable[Tuple[str, bytes]] = (),
    unlisted: Iterable[str] = (),
) -> str:
    entries = dict({"talk2view/__init__.py": b""}, **files)
    record = "".join(
        "{},sha256=x,1\n".format(name) for name in entries if name not in set(unlisted)
    )
    path = tmp_path / "talk2view-0.3.0-py3-none-any.whl"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about the duplicates we add on purpose
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
            for name, data in extra:
                archive.writestr(name, data)
            archive.writestr("talk2view-0.3.0.dist-info/RECORD", record)
    return str(path)


def make_sdist(tmp_path: Path, files: Dict[str, bytes]) -> str:
    path = tmp_path / "talk2view-0.3.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in dict({"pyproject.toml": b""}, **files).items():
            info = tarfile.TarInfo("talk2view-0.3.0/" + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return str(path)


def test_pyproject_carries_the_git_ignored_page_through_build_level_artifacts() -> None:
    text = (PACKAGE / "pyproject.toml").read_text(encoding="utf-8")
    assert '[tool.hatch.build]\nartifacts = ["src/talk2view/ui/static/**"]' in text
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    assert "force-include" not in code
    ignore = (PACKAGE / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/src/talk2view/ui/static/" in ignore


def test_a_wheel_with_the_page_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    wheel = make_wheel(tmp_path, page_files(guard.WHEEL_STATIC))
    assert guard.check_wheel(wheel) == []
    assert guard.main([wheel]) == 0
    assert capsys.readouterr().out.startswith("OK")


def test_an_sdist_with_the_page_passes(tmp_path: Path) -> None:
    assert guard.check_sdist(make_sdist(tmp_path, page_files(guard.SDIST_STATIC))) == []


def test_the_test_stub_page_fails_in_a_wheel_and_an_sdist(tmp_path: Path) -> None:
    """A build over a static dir still holding the tests' stub must not pass."""
    assert guard.check_wheel(make_wheel(tmp_path, stub_files(guard.WHEEL_STATIC))) == [
        "talk2view/ui/static/index.html is not the page embed/ builds "
        "(no talk2view-page marker): the test stub, or an old build"
    ]
    assert guard.check_sdist(make_sdist(tmp_path, stub_files(guard.SDIST_STATIC))) == [
        "src/talk2view/ui/static/index.html is not the page embed/ builds "
        "(no talk2view-page marker): the test stub, or an old build"
    ]


def test_a_wheel_without_the_page_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    wheel = make_wheel(tmp_path, {})
    assert guard.check_wheel(wheel) == [
        "missing talk2view/ui/static/index.html (build the chat page before packaging)"
    ]
    assert guard.main([wheel]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_an_sdist_without_the_page_fails(tmp_path: Path) -> None:
    problems = guard.check_sdist(make_sdist(tmp_path, {}))
    assert problems == [
        "missing src/talk2view/ui/static/index.html (build the chat page before packaging)"
    ]


def test_duplicate_entries_fail(tmp_path: Path) -> None:
    files = page_files(guard.WHEEL_STATIC)
    wheel = make_wheel(tmp_path, files, extra=[("talk2view/ui/static/assets/app.js", b"again")])
    assert guard.check_wheel(wheel) == [
        "duplicate entries: talk2view/ui/static/assets/app.js"
    ]


def test_a_referenced_asset_that_is_missing_fails(tmp_path: Path) -> None:
    files = page_files(guard.WHEEL_STATIC)
    del files["talk2view/ui/static/assets/app.css"]
    assert guard.check_wheel(make_wheel(tmp_path, files)) == [
        "missing talk2view/ui/static/assets/app.css (referenced by index.html)"
    ]


def test_a_static_file_missing_from_record_fails(tmp_path: Path) -> None:
    wheel = make_wheel(
        tmp_path, page_files(guard.WHEEL_STATIC), unlisted=["talk2view/ui/static/assets/app.js"]
    )
    assert guard.check_wheel(wheel) == [
        "talk2view/ui/static/assets/app.js is not listed in RECORD"
    ]


def test_an_index_that_is_not_a_built_page_fails(tmp_path: Path) -> None:
    index = b'<meta name="talk2view-page" content="sdk-embed"><p>placeholder</p>'
    wheel = make_wheel(tmp_path, {"talk2view/ui/static/index.html": index})
    assert guard.check_wheel(wheel) == [
        "talk2view/ui/static/index.html references no local asset: not a built page"
    ]


def test_references_are_resolved_like_the_browser_does() -> None:
    html = (
        '<link href="./assets/a.css"><script src="/assets/b.js?v=2"></script>'
        '<a href="https://example.com/x"></a><img src="data:image/png;base64,AA">'
        '<a href="#top"></a><script src="//cdn.example/x.js"></script>'
        '<script src="../escape.js"></script>'
    )
    assert guard.local_references(html) == ["assets/a.css", "assets/b.js", "../escape.js"]


def test_a_reference_outside_the_bundle_fails(tmp_path: Path) -> None:
    index = b'<meta name="talk2view-page" content="sdk-embed"><script src="../../evil.js"></script>'
    files = {"talk2view/ui/static/index.html": index}
    assert guard.check_wheel(make_wheel(tmp_path, files)) == [
        "talk2view/ui/static/index.html references ../../evil.js outside the bundle"
    ]


def test_an_unknown_archive_type_fails(tmp_path: Path) -> None:
    assert guard.check_archive(str(tmp_path / "x.zip")) == [
        "not a wheel or an sdist: {}".format(tmp_path / "x.zip")
    ]

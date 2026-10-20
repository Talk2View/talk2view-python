"""talk2view.ui.static: traversal-proof mapping, content types, the missing-bundle error."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from talk2view.ui.static import (
    BUILD_INSTRUCTIONS,
    StaticAssetsMissingError,
    content_type_for,
    default_static_dir,
    require_static_dir,
    resolve_static,
)

STUB = Path(__file__).parent / "fixtures" / "ui_static"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    for rel in ("index.html", "assets/app.js"):
        (dist / rel).write_text("x", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("s", encoding="utf-8")
    return dist


def test_root_maps_to_index_and_queries_are_ignored(root: Path) -> None:
    assert resolve_static(root, "/") == os.path.realpath(root / "index.html")
    assert (resolve_static(root, "/assets/app.js?v=1") or "").endswith("app.js")
    assert resolve_static(root, "/nope.js") is None
    assert resolve_static(root, "/assets") is None  # a directory is not a file


@pytest.mark.parametrize(
    "bad",
    [
        "/../secret.txt",
        "/assets/../../secret.txt",
        "/%2e%2e/secret.txt",
        "/..%2fsecret.txt",
        "/assets/..\\..\\secret.txt",
        "/assets/%5c..%5csecret.txt",
        "//etc/passwd",
        "/index.html%00.js",
        "../secret.txt",  # relative (no leading slash); BaseHTTPRequestHandler accepts it
    ],
)
def test_traversal_is_refused(root: Path, bad: str) -> None:
    assert resolve_static(root, bad) is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlink_out_of_the_root_is_refused(root: Path) -> None:
    (root / "leak.txt").symlink_to(root.parent / "secret.txt")
    assert resolve_static(root, "/leak.txt") is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_directory_symlink_out_of_the_root_is_refused(root: Path) -> None:
    (root / "escape").symlink_to(root.parent)
    assert resolve_static(root, "/escape/secret.txt") is None


def test_dotfiles_and_reserved_device_names_are_refused(root: Path) -> None:
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    (root / "CON.txt").write_text("x", encoding="utf-8")
    assert resolve_static(root, "/.env") is None
    assert resolve_static(root, "/CON.txt") is None
    assert resolve_static(root, "/con.txt") is None  # case-insensitive
    assert resolve_static(root, "/assets/CON") is None  # reserved, no extension


@pytest.mark.skipif(os.name == "nt", reason="a trailing space/dot is stripped by Windows only")
def test_reserved_name_check_normalises_trailing_space_and_dot(root: Path) -> None:
    # Windows strips trailing spaces/dots from a component before resolving it, so these
    # spellings are all still the reserved name "CON" as far as the filesystem cares.
    (root / "CON .txt").write_text("x", encoding="utf-8")
    assert resolve_static(root, "/CON .txt") is None


def test_console_io_and_superscript_device_names_are_refused(root: Path) -> None:
    (root / "CONIN$").write_text("x", encoding="utf-8")
    (root / "CONOUT$").write_text("x", encoding="utf-8")
    (root / "COM¹").write_text("x", encoding="utf-8")
    assert resolve_static(root, "/CONIN$") is None
    assert resolve_static(root, "/CONOUT$") is None
    assert resolve_static(root, "/COM¹") is None


@pytest.mark.skipif(os.name == "nt", reason="colon is forbidden in a Windows filename")
def test_a_colon_in_a_path_segment_is_refused(root: Path) -> None:
    (root / "a:b.js").write_text("x", encoding="utf-8")
    assert resolve_static(root, "/a:b.js") is None


def test_a_malformed_target_is_refused_not_raised(root: Path) -> None:
    # urlsplit raises ValueError on these; resolve_static must not propagate it.
    assert resolve_static(root, "http://[evil/events") is None
    assert resolve_static(root, "//[x/events") is None


def test_content_types_never_come_from_the_registry() -> None:
    assert content_type_for("/x/app.js") == "text/javascript; charset=utf-8"
    assert content_type_for("INDEX.HTML") == "text/html; charset=utf-8"
    assert content_type_for("a.woff2") == "font/woff2"
    assert content_type_for("a.bin") == "application/octet-stream"


def test_missing_bundle_raises_with_build_instructions(tmp_path: Path) -> None:
    with pytest.raises(StaticAssetsMissingError) as info:
        require_static_dir(tmp_path)
    message = str(info.value)
    assert str(tmp_path / "index.html") in message
    assert "npm run build" in message and "static_dir" in message
    assert BUILD_INSTRUCTIONS in message
    assert info.value.static_dir == tmp_path
    assert isinstance(info.value, RuntimeError)


def test_require_static_dir_resolves_a_built_bundle() -> None:
    assert require_static_dir(STUB) == STUB.resolve()
    assert require_static_dir(str(STUB)) == STUB.resolve()


def test_the_default_directory_is_inside_the_package() -> None:
    assert default_static_dir().parts[-3:] == ("talk2view", "ui", "static")

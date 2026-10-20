"""talk2view.ui.multipart: the one-file form the page sends, and nothing else."""

from __future__ import annotations

import httpx
import pytest

from talk2view.ui.multipart import MultipartError, parse_upload, safe_filename

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"\r\n--not-the-boundary\r\n" + b"\x00" * 16
CT = "multipart/form-data; boundary=----WebKitFormBoundaryAbc123"


def chromium_body(
    data: bytes = PNG,
    filename: str = "scan.png",
    part_type: str = "image/png",
    name: str = "file",
    boundary: str = "----WebKitFormBoundaryAbc123",
) -> bytes:
    """The bytes Chromium sends for ``FormData`` with one file (raw UTF-8 filename)."""
    return (
        b"--" + boundary.encode() + b"\r\n"
        + 'Content-Disposition: form-data; name="{}"; filename="{}"\r\n'.format(
            name, filename
        ).encode("utf-8")
        + ("Content-Type: " + part_type + "\r\n" if part_type else "").encode()
        + b"\r\n" + data + b"\r\n--" + boundary.encode() + b"--\r\n"
    )


def test_chromium_shaped_body_round_trips_binary_exactly() -> None:
    upload = parse_upload(chromium_body(), CT)
    assert upload.filename == "scan.png"
    assert upload.content_type == "image/png"
    assert upload.data == PNG


def test_httpx_encoded_body_is_accepted() -> None:
    files = {"file": ("r\u00e9sum\u00e9.pdf", PNG, "application/pdf")}
    request = httpx.Request("POST", "http://127.0.0.1/upload", files=files)
    body = request.read()
    upload = parse_upload(body, request.headers["Content-Type"])
    assert upload.data == PNG
    assert upload.content_type == "application/pdf"
    assert upload.filename == "r\u00e9sum\u00e9.pdf"


def test_unicode_filename_and_a_missing_part_type() -> None:
    upload = parse_upload(chromium_body(filename="\u65e5\u672c.png", part_type=""), CT)
    assert upload.filename == "\u65e5\u672c.png"
    assert upload.content_type is None


def test_the_close_delimiter_may_omit_the_final_crlf() -> None:
    body = chromium_body()[: -2]
    assert parse_upload(body, CT).data == PNG


@pytest.mark.parametrize(
    "body, content_type",
    [
        (chromium_body(), "application/json"),
        (chromium_body(), "multipart/form-data"),
        (chromium_body(), "multipart/form-data; boundary=" + "x" * 71),
        (b"preamble\r\n" + chromium_body(), CT),
        (chromium_body(name="other"), CT),
        (chromium_body().replace(b'; filename="scan.png"', b""), CT),
        (chromium_body().replace(b"form-data;", b"attachment;"), CT),
        (chromium_body()[:-40], CT),
        (chromium_body() + b"trailing", CT),
        (
            chromium_body()[:-4] + b"\r\n"
            + b'Content-Disposition: form-data; name="x"\r\n\r\nsecond\r\n'
            + b"------WebKitFormBoundaryAbc123--\r\n",
            CT,
        ),
        (chromium_body().replace(b"\r\n\r\n", b"\r\n", 1), CT),
        (chromium_body().replace(b"Content-Type: image/png\r\n", b" folded\r\n"), CT),
        (chromium_body().replace(b"Content-Type", b"Content-Disposition"), CT),
        (chromium_body().replace(b"scan.png", b"\xff\xfe.png"), CT),
    ],
    ids=[
        "not-multipart",
        "no-boundary",
        "boundary-too-long",
        "preamble",
        "wrong-field",
        "not-a-file",
        "not-form-data",
        "unterminated",
        "epilogue",
        "two-parts",
        "no-header-end",
        "folded-header",
        "repeated-header",
        "non-utf8-header",
    ],
)
def test_anything_else_is_refused(body: bytes, content_type: str) -> None:
    with pytest.raises(MultipartError):
        parse_upload(body, content_type)


def test_oversized_part_headers_are_refused() -> None:
    body = chromium_body().replace(
        b"Content-Type: image/png", b"Content-Type: image/png\r\nX-Pad: " + b"a" * 9000
    )
    with pytest.raises(MultipartError):
        parse_upload(body, CT)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("C:\\Users\\me\\scan.png", "scan.png"),
        ("../../etc/passwd", "passwd"),
        ("a\x00b\x1fc.png", "abc.png"),
        ("", "file"),
        ("   ", "file"),
        (".", "file"),
        ("..", "file"),
        ("a\u0085b\u009bc.png", "abc.png"),  # NEL and a C1 control stripped
        ("scan\u202egnp.exe", "scangnp.exe"),  # RLO (bidi override) stripped
        ("a\u2028b.png", "ab.png"),  # LINE SEPARATOR stripped
        ("a\u200eb\u200fc.png", "abc.png"),  # LRM/RLM (the rest of Bidi_Control) stripped
        ("a\u061cb.png", "ab.png"),  # ARABIC LETTER MARK stripped
        # The stem is truncated, not the tail, so a long name keeps its extension.
        ("x" * 300 + ".png", "x" * 251 + ".png"),
    ],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


@pytest.mark.parametrize(
    "charset",
    ["idna", "punycode", "undefined", "utf-16", "bogus-charset", "rot13", "base64"],
)
def test_unsupported_filename_charsets_are_refused(charset: str) -> None:
    # collapse_rfc2231_value only catches LookupError; idna/punycode/undefined raise
    # UnicodeError instead, so only utf-8/us-ascii are attempted at all.
    body = chromium_body().replace(
        b'filename="scan.png"', ("filename*=%s''scan.png" % charset).encode()
    )
    with pytest.raises(MultipartError):
        parse_upload(body, CT)


@pytest.mark.parametrize("charset", ["utf-8", "UTF-8", "us-ascii"])
def test_utf8_and_us_ascii_filename_charsets_are_accepted(charset: str) -> None:
    body = chromium_body().replace(
        b'filename="scan.png"', ("filename*=%s''scan.png" % charset).encode()
    )
    assert parse_upload(body, CT).filename == "scan.png"


@pytest.mark.parametrize(
    "value",
    ["", "image", "x/y/z", "image/png, text/html", "image/png\x00", "ima\x01ge/png"],
)
def test_an_invalid_part_content_type_is_treated_as_absent(value: str) -> None:
    body = (
        b"--" b"----WebKitFormBoundaryAbc123\r\n"
        b'Content-Disposition: form-data; name="file"; filename="scan.png"\r\n'
        b"Content-Type: " + value.encode("utf-8") + b"\r\n"
        b"\r\n" + PNG + b"\r\n--" b"----WebKitFormBoundaryAbc123--\r\n"
    )
    assert parse_upload(body, CT).content_type is None

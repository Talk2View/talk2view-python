"""Error-sanitization tests (SEC-20 / VULN-12, issue #106).

The SDK must surface only the structured, user-safe ``error.type`` +
``error.message`` (plus HTTP status) in a thrown error's user-facing message.
The server's raw ``detail`` (stack traces, DB errors, file paths) and any opaque
non-JSON body must NOT appear in the default message — a *structured* detail is
kept on a separate attribute for debugging only, and FastAPI 422/404 responses
stay debuggable rather than collapsing to a bare status.
"""

from __future__ import annotations

import json

import pytest

from talk2view.client import T2VClient
from talk2view.errors import T2VError
from talk2view.storage import MemoryStorage

FAKE_STACK = (
    "Traceback (most recent call last):\n"
    '  File "/srv/app/db.py", line 42, in query\n'
    "    raise OperationalError(\"FATAL: password authentication failed for user 't2v'\")\n"
    "psycopg2.OperationalError: connection to 10.0.0.5:5432 failed"
)


def _make_client(base_url: str) -> T2VClient:
    storage = MemoryStorage()
    storage.set("access_token", "tok")
    return T2VClient("pk_test", storage, base_url)


def test_structured_error_surfaces_type_and_message_not_detail(httpserver) -> None:  # type: ignore[no-untyped-def]
    """A structured {error:{type,message,detail}} surfaces type+message but the
    raw detail (stack trace, DB error, file paths) never enters the message."""
    httpserver.expect_request("/v1/thing").respond_with_json(
        {
            "error": {
                "type": "internal_error",
                "message": "Something went wrong on our end.",
                "detail": FAKE_STACK,
            }
        },
        status=500,
    )

    c = _make_client(httpserver.url_for(""))

    with pytest.raises(T2VError) as excinfo:
        c.request("/v1/thing")

    err = excinfo.value
    assert err.type == "internal_error"
    assert err.status_code == 500
    assert str(err) == "Something went wrong on our end."
    # Raw detail must NOT leak into the user-facing message.
    assert "Traceback" not in str(err)
    assert "OperationalError" not in str(err)
    assert "/srv/app/db.py" not in str(err)
    assert "password authentication failed" not in str(err)
    assert "10.0.0.5" not in str(err)
    # ...but stays available for debugging.
    assert err.detail == FAKE_STACK


def test_opaque_non_json_error_yields_generic_message_with_status(httpserver) -> None:  # type: ignore[no-untyped-def]
    """A non-JSON / opaque body (e.g. an HTML 502) must yield a generic message +
    status, never echoing response.text back to the caller."""
    html = "<html><body>502 Bad Gateway - nginx/1.25.3 at db-internal.svc</body></html>"
    httpserver.expect_request("/v1/thing").respond_with_data(
        html, status=502, content_type="text/html"
    )

    c = _make_client(httpserver.url_for(""))

    with pytest.raises(T2VError) as excinfo:
        c.request("/v1/thing")

    err = excinfo.value
    assert err.status_code == 502
    assert str(err) == "Request failed (HTTP 502)"
    # The raw HTML / internal hostnames must NOT appear in the message.
    assert "nginx" not in str(err)
    assert "db-internal" not in str(err)
    assert "<html>" not in str(err)
    assert err.detail is None


def test_fastapi_422_validation_stays_debuggable(httpserver) -> None:  # type: ignore[no-untyped-def]
    """FastAPI's default 422 shape is a top-level ``detail`` LIST. It must stay
    debuggable — not collapse to a bare "Request failed (HTTP 422)" — with the
    structured list preserved (JSON-serialized) on ``.detail``."""
    validation = [
        {"loc": ["body", "model"], "msg": "field required", "type": "value_error.missing"}
    ]
    httpserver.expect_request("/v1/thing").respond_with_json(
        {"detail": validation}, status=422
    )

    c = _make_client(httpserver.url_for(""))

    with pytest.raises(T2VError) as excinfo:
        c.request("/v1/thing")

    err = excinfo.value
    assert err.status_code == 422
    # Not flattened to a bare status — the validation detail is visible.
    assert str(err) != "Request failed (HTTP 422)"
    assert "field required" in str(err)
    # The structured list is preserved (JSON-serialized) for programmatic use.
    assert err.detail is not None
    assert json.loads(err.detail) == validation


def test_fastapi_404_string_detail_is_surfaced(httpserver) -> None:  # type: ignore[no-untyped-def]
    """A ``{"detail": "..."}`` HTTPException message (e.g. a 404) is a safe,
    human-readable string and must be surfaced instead of a bare status."""
    httpserver.expect_request("/v1/thing").respond_with_json(
        {"detail": "Session not found"}, status=404
    )

    c = _make_client(httpserver.url_for(""))

    with pytest.raises(T2VError) as excinfo:
        c.request("/v1/thing")

    err = excinfo.value
    assert err.status_code == 404
    assert str(err) == "Session not found"


def test_parse_error_does_not_echo_response_text() -> None:
    """Directly exercise _parse_error: an unparseable body must fall back to a
    generic message and never include response.text."""
    from unittest.mock import MagicMock

    response = MagicMock()
    response.json.side_effect = Exception("not json")
    response.status_code = 500
    response.text = "psycopg2.OperationalError: FATAL secret-internal-detail"

    result = T2VClient._parse_error(response)

    assert result["message"] == "Request failed (HTTP 500)"
    assert "secret-internal-detail" not in result["message"]
    assert "psycopg2" not in result["message"]
    assert result["detail"] is None

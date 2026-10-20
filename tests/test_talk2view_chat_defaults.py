"""Talk2View.set_chat_defaults — how ChatController's model/capabilities reach the session."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

from talk2view import Talk2View
from talk2view.types import Capabilities


def _t2v() -> Talk2View:
    t2v = Talk2View("pk_test_123", base_url="http://localhost:9999")
    t2v._client.close()
    client = MagicMock()

    def request(endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        if endpoint == "/v1/sessions":
            return {"session_id": "s1", "thread_id": "th1", "model": "m"}
        return {}

    client.request.side_effect = request
    t2v._client = client
    t2v.tools._client = client
    return t2v


def test_defaults_apply_to_the_next_session() -> None:
    t2v = _t2v()
    capabilities = Capabilities(approval=True, tool_events=True)

    t2v.set_chat_defaults(model="gpt-4o", capabilities=capabilities)
    session = t2v.create_session()

    assert session._default_model == "gpt-4o"
    assert session._capabilities == capabilities
    body = t2v._client.request.call_args_list[0][1]["body"]  # type: ignore[attr-defined]
    assert body == {"model": "gpt-4o"}


def test_a_change_keeps_the_current_session_object_and_deletes_nothing() -> None:
    """The next chat() builds a session with the new settings; until then an approval
    pending on this one can still be answered (respond_to_approval)."""
    t2v = _t2v()
    session = t2v.create_session()

    t2v.set_chat_defaults(model="gpt-4o")

    assert t2v.get_session() is session
    deletes = [
        c
        for c in t2v._client.request.call_args_list  # type: ignore[attr-defined]
        if c[1].get("method") == "DELETE"
    ]
    assert deletes == []


def test_no_change_keeps_the_session() -> None:
    t2v = _t2v()
    t2v.set_chat_defaults(model="gpt-4o", capabilities=Capabilities(reasoning=True))
    session = t2v.create_session()

    t2v.set_chat_defaults(model="gpt-4o", capabilities=Capabilities(reasoning=True))
    t2v.set_chat_defaults()

    assert t2v.get_session() is session

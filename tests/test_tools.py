"""Tests for the @tool decorator and T2VTools."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from talk2view.tools import (
    T2VTools,
    _generate_schema,
    _parse_google_docstring,
    _schema_from_pydantic,
    strip_null_args,
    tool,
)
from talk2view.types import ClientToolSchema, PermissionCheck, PermissionResult


class TestParseGoogleDocstring:
    def test_empty(self) -> None:
        desc, params = _parse_google_docstring(None)
        assert desc == ""
        assert params == {}

    def test_description_only(self) -> None:
        desc, params = _parse_google_docstring("Does something useful.")
        assert desc == "Does something useful."
        assert params == {}

    def test_multiline_description(self) -> None:
        doc = """Does something useful.
        It really does."""
        desc, params = _parse_google_docstring(doc)
        assert desc == "Does something useful. It really does."

    def test_with_args(self) -> None:
        doc = """Create a shape.

        Args:
            shape: The shape type.
            size: The size in units.
        """
        desc, params = _parse_google_docstring(doc)
        assert desc == "Create a shape."
        assert params == {
            "shape": "The shape type.",
            "size": "The size in units.",
        }

    def test_with_typed_args(self) -> None:
        doc = """Create something.

        Args:
            name (str): The name to use.
            count (int): How many times.

        Returns:
            The result string.
        """
        desc, params = _parse_google_docstring(doc)
        assert desc == "Create something."
        assert params == {
            "name": "The name to use.",
            "count": "How many times.",
        }

    def test_multiline_param_description(self) -> None:
        doc = """Do thing.

        Args:
            data: A very long description that
                spans multiple lines.
        """
        desc, params = _parse_google_docstring(doc)
        assert params["data"] == "A very long description that spans multiple lines."


class TestToolDecorator:
    def test_basic_function(self) -> None:
        @tool
        def my_tool(name: str, count: int) -> str:
            """Does something useful.

            Args:
                name: The name to use.
                count: How many times.
            """
            return "ok"

        assert hasattr(my_tool, "_t2v_tool_schema")
        schema = my_tool._t2v_tool_schema

        assert schema.name == "my_tool"
        assert schema.description == "Does something useful."
        assert schema.parameters["properties"]["name"] == {
            "type": "string",
            "description": "The name to use.",
        }
        assert schema.parameters["properties"]["count"] == {
            "type": "integer",
            "description": "How many times.",
        }
        assert schema.parameters["required"] == ["name", "count"]

    def test_optional_param(self) -> None:
        @tool
        def my_tool(name: str, color: str = "red") -> str:
            """A tool.

            Args:
                name: The name.
                color: The color.
            """
            return name

        schema = my_tool._t2v_tool_schema
        assert "name" in schema.parameters["required"]
        assert "color" not in schema.parameters["required"]

    def test_bool_and_float(self) -> None:
        @tool
        def my_tool(visible: bool, opacity: float) -> str:
            """Toggle visibility."""
            return "ok"

        schema = my_tool._t2v_tool_schema
        assert schema.parameters["properties"]["visible"]["type"] == "boolean"
        assert schema.parameters["properties"]["opacity"]["type"] == "number"

    def test_list_type(self) -> None:
        @tool
        def my_tool(names: List[str]) -> str:
            """Process names."""
            return "ok"

        schema = my_tool._t2v_tool_schema
        prop = schema.parameters["properties"]["names"]
        assert prop["type"] == "array"
        assert prop["items"] == {"type": "string"}

    def test_still_callable(self) -> None:
        @tool
        def my_tool(x: int) -> str:
            """Add one."""
            return str(x + 1)

        assert my_tool(5) == "6"

    def test_bound_method_pattern(self) -> None:
        """Test the tool(self.method) pattern used by SlicerTools."""

        class MyTools:
            def get_context(self, query: str) -> str:
                """Get context.

                Args:
                    query: The search query.
                """
                return "context for " + query

        obj = MyTools()
        decorated = tool(obj.get_context)

        schema = decorated._t2v_tool_schema
        assert schema.name == "get_context"
        assert "query" in schema.parameters["properties"]
        # self should NOT be in parameters
        assert "self" not in schema.parameters["properties"]

        # Should still be callable
        assert decorated("test") == "context for test"

    def test_no_docstring(self) -> None:
        @tool
        def my_tool(x: str) -> str:
            return x

        schema = my_tool._t2v_tool_schema
        assert schema.name == "my_tool"
        assert schema.description == "my_tool"  # Falls back to function name

    def test_langchain_compat_attrs_function(self) -> None:
        """@tool on a function exposes .name, .description, .args."""

        @tool
        def create_shape(shape: str, size: int) -> str:
            """Create a 3D shape.

            Args:
                shape: The shape type.
                size: The size in units.
            """
            return "ok"

        assert create_shape.name == "create_shape"
        assert create_shape.description == "Create a 3D shape."
        assert "shape" in create_shape.args
        assert create_shape.args["shape"]["type"] == "string"

    def test_langchain_compat_attrs_bound_method(self) -> None:
        """tool(self.method) exposes .name, .description, .args."""

        class MyTools:
            def get_context(self, query: str) -> str:
                """Get context data.

                Args:
                    query: Search query.
                """
                return query

        obj = MyTools()
        decorated = tool(obj.get_context)

        assert decorated.name == "get_context"
        assert decorated.description == "Get context data."
        assert "query" in decorated.args
        assert "self" not in decorated.args


class TestGenerateSchema:
    def test_optional_annotation(self) -> None:
        def my_func(name: str, label: Optional[str] = None) -> str:
            """Do thing.

            Args:
                name: The name.
                label: Optional label.
            """
            return name

        schema = _generate_schema(my_func)
        assert schema.parameters["properties"]["label"]["type"] == "string"
        assert "label" not in schema.parameters["required"]


class TestT2VTools:
    def test_handle_and_execute(self) -> None:
        client = MagicMock()
        tools = T2VTools(client)

        def my_handler(name: str) -> str:
            return "hello " + name

        tools.handle("greet", my_handler)
        assert tools.has_handler("greet")

        result, is_error = tools.execute_tool_call("greet", {"name": "world"})
        assert result == "hello world"
        assert is_error is False

    def test_execute_unknown_tool(self) -> None:
        client = MagicMock()
        tools = T2VTools(client)

        result, is_error = tools.execute_tool_call("unknown", {})
        assert is_error is True
        data = json.loads(result)
        assert "Unknown tool" in data["error"]

    def test_execute_handler_error(self) -> None:
        client = MagicMock()
        tools = T2VTools(client)

        def bad_handler() -> str:
            raise ValueError("something broke")

        tools.handle("bad", bad_handler)
        result, is_error = tools.execute_tool_call("bad", {})
        assert is_error is True
        data = json.loads(result)
        assert "something broke" in data["error"]

    def test_register_from_functions(self) -> None:
        client = MagicMock()
        client.request.return_value = {"registered": ["my_tool"], "count": 1}
        tools = T2VTools(client)

        @tool
        def my_tool(x: str) -> str:
            """Do thing."""
            return x

        response = tools.register_from_functions([my_tool])
        assert response.registered == ["my_tool"]
        assert response.count == 1

        # Handler should be registered
        assert tools.has_handler("my_tool")

        # Schema should have been sent to server
        call_args = client.request.call_args
        assert call_args[1]["body"]["tools"][0]["name"] == "my_tool"

    def test_execute_non_string_result(self) -> None:
        client = MagicMock()
        tools = T2VTools(client)

        def dict_handler() -> dict:  # type: ignore[type-arg]
            return {"key": "value"}

        tools.handle("dict_tool", dict_handler)
        result, is_error = tools.execute_tool_call("dict_tool", {})
        assert is_error is False
        assert json.loads(result) == {"key": "value"}


class TestPydanticSchema:
    """Tests for _schema_from_pydantic and @tool(args_schema=...)."""

    def test_basic_pydantic_model(self) -> None:
        """Simple pydantic model extracts types and descriptions."""
        from pydantic import BaseModel, Field

        class MyInput(BaseModel):
            name: str = Field(description="The name")
            count: int = Field(description="The count")

        def my_func(name: str, count: int) -> str:
            """Do something useful."""
            return "ok"

        schema = _schema_from_pydantic(my_func, MyInput)
        assert schema.name == "my_func"
        assert schema.description == "Do something useful."
        assert schema.parameters["properties"]["name"]["type"] == "string"
        assert schema.parameters["properties"]["name"]["description"] == "The name"
        assert schema.parameters["properties"]["count"]["type"] == "integer"
        assert "name" in schema.parameters["required"]
        assert "count" in schema.parameters["required"]

    def test_pydantic_with_defaults(self) -> None:
        """Fields with defaults should not be in required."""
        from pydantic import BaseModel, Field

        class MyInput(BaseModel):
            name: str = Field(description="The name")
            color: str = Field(default="red", description="The color")

        def my_func(name: str, color: str = "red") -> str:
            """A tool."""
            return name

        schema = _schema_from_pydantic(my_func, MyInput)
        assert "name" in schema.parameters["required"]
        assert "color" not in schema.parameters["required"]
        assert schema.parameters["properties"]["color"]["default"] == "red"

    def test_pydantic_list_field(self) -> None:
        """List[str] fields produce array schema."""
        from pydantic import BaseModel, Field

        class MyInput(BaseModel):
            names: List[str] = Field(description="List of names")

        def my_func(names: List[str]) -> str:
            """Process names."""
            return "ok"

        schema = _schema_from_pydantic(my_func, MyInput)
        prop = schema.parameters["properties"]["names"]
        assert prop["type"] == "array"
        assert prop["items"] == {"type": "string"}

    def test_pydantic_optional_field(self) -> None:
        """Optional fields (anyOf pattern in pydantic v2) resolve correctly."""
        from pydantic import BaseModel, Field

        class MyInput(BaseModel):
            name: str = Field(description="The name")
            label: Optional[str] = Field(default=None, description="Optional label")

        def my_func(name: str, label: Optional[str] = None) -> str:
            """A tool."""
            return name

        schema = _schema_from_pydantic(my_func, MyInput)
        label_prop = schema.parameters["properties"]["label"]
        assert label_prop["type"] == "string"
        assert "label" not in schema.parameters["required"]

    def test_tool_with_args_schema(self) -> None:
        """@tool(args_schema=Model) uses pydantic schema extraction."""
        from pydantic import BaseModel, Field

        class MyInput(BaseModel):
            query: str = Field(description="Search query")

        @tool(args_schema=MyInput)
        def search(query: str) -> str:
            """Search for things."""
            return query

        schema = search._t2v_tool_schema
        assert schema.name == "search"
        assert schema.description == "Search for things."
        assert schema.parameters["properties"]["query"]["type"] == "string"
        assert schema.parameters["properties"]["query"]["description"] == "Search query"

    def test_tool_with_return_direct(self) -> None:
        """@tool(args_schema=Model, return_direct=True) sets return_direct."""
        from pydantic import BaseModel, Field

        class MyInput(BaseModel):
            msg: str = Field(description="Message")

        @tool(args_schema=MyInput, return_direct=True)
        def echo(msg: str) -> str:
            """Echo message."""
            return msg

        schema = echo._t2v_tool_schema
        assert schema.return_direct is True


def _align_schema() -> ClientToolSchema:
    return ClientToolSchema(
        name="align_text",
        description="Align the selected text.",
        parameters={
            "type": "object",
            "properties": {
                "alignment": {"type": "string", "enum": ["left", "center", "right"]},
                "section": {"type": "string"},
                "level": {"type": "integer", "enum": [1, 2, 3]},
            },
            "required": ["alignment"],
        },
    )


def _count_schema() -> ClientToolSchema:
    return ClientToolSchema(
        name="repeat",
        description="Repeat something.",
        parameters={
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "scale": {"type": "number"},
            },
            "required": ["count"],
        },
    )


def _tools_with(schema: ClientToolSchema) -> tuple:  # type: ignore[type-arg]
    client = MagicMock()
    client.request.return_value = {"registered": [schema.name], "count": 1}
    tools = T2VTools(client)
    tools.register([schema])
    return tools, client


class TestArgCleaningAndValidation:
    def test_strip_null_and_empty_object_args(self) -> None:
        cleaned = strip_null_args(
            {"a": None, "b": {}, "c": 0, "d": "", "e": False, "f": [], "g": {"k": 1}}
        )
        assert cleaned == {"c": 0, "d": "", "e": False, "f": [], "g": {"k": 1}}

        tools, _ = _tools_with(_align_schema())
        received: Dict[str, Any] = {}

        def handler(alignment: str, section: str = "body") -> str:
            received.update(alignment=alignment, section=section)
            return "ok"

        tools.handle("align_text", handler)
        result, is_error = tools.execute_tool_call(
            "align_text", {"alignment": "left", "section": {}, "level": None}
        )

        assert (result, is_error) == ("ok", False)
        assert received == {"alignment": "left", "section": "body"}

    def test_enum_case_insensitive_normalised(self) -> None:
        tools, _ = _tools_with(_align_schema())
        received: Dict[str, Any] = {}

        def handler(alignment: str) -> str:
            received["alignment"] = alignment
            return "ok"

        tools.handle("align_text", handler)
        result, is_error = tools.execute_tool_call("align_text", {"alignment": "Center"})

        assert (result, is_error) == ("ok", False)
        assert received == {"alignment": "center"}  # schema casing, not the model's

    def test_enum_value_outside_the_list_is_error_result(self) -> None:
        tools, _ = _tools_with(_align_schema())
        tools.handle("align_text", lambda alignment: "ok")

        result, is_error = tools.execute_tool_call("align_text", {"alignment": "justify"})

        assert is_error is True
        assert json.loads(result) == {
            "error": 'Argument "alignment" must be one of: left, center, right'
        }

    def test_non_string_enum_compared_as_text(self) -> None:
        tools, _ = _tools_with(_align_schema())
        tools.handle("align_text", lambda alignment, level=1: "ok")

        assert tools.execute_tool_call("align_text", {"alignment": "left", "level": 2}) == (
            "ok",
            False,
        )
        result, is_error = tools.execute_tool_call(
            "align_text", {"alignment": "left", "level": 9}
        )
        assert is_error is True
        assert json.loads(result) == {"error": 'Argument "level" must be one of: 1, 2, 3'}

    def test_missing_required_is_error_result(self) -> None:
        tools, _ = _tools_with(_align_schema())
        calls: List[str] = []
        tools.handle("align_text", lambda **kwargs: calls.append("ran") or "ok")

        result, is_error = tools.execute_tool_call("align_text", {"alignment": None})

        assert is_error is True
        assert json.loads(result) == {"error": "Missing required argument: alignment"}
        assert calls == []  # the handler never ran

    def test_wrong_type_is_error_result(self) -> None:
        tools, _ = _tools_with(_align_schema())
        tools.handle("align_text", lambda **kwargs: "ok")

        result, is_error = tools.execute_tool_call(
            "align_text", {"alignment": "left", "level": True}
        )

        assert is_error is True
        assert json.loads(result) == {
            "error": 'Argument "level" expected type "integer", got bool'
        }

    def test_integer_rejects_a_non_integral_float(self) -> None:
        """A model that sends 1.5 for an integer gets told, rather than having
        it passed to a handler that indexes or slices with it."""
        tools, _ = _tools_with(_count_schema())
        calls: List[Any] = []
        tools.handle("repeat", lambda count: calls.append(count) or "ok")

        result, is_error = tools.execute_tool_call("repeat", {"count": 1.5})

        assert is_error is True
        assert json.loads(result) == {
            "error": 'Argument "count" expected type "integer", got float'
        }
        assert calls == []

    @pytest.mark.parametrize("value", [3, 3.0, -0.0])
    def test_integer_accepts_ints_and_integral_floats(self, value: Any) -> None:
        """JSON has one number type, so 3.0 on the wire is the integer 3."""
        tools, _ = _tools_with(_count_schema())
        tools.handle("repeat", lambda count: "ok")

        assert tools.execute_tool_call("repeat", {"count": value}) == ("ok", False)

    @pytest.mark.parametrize("value", [float("nan"), float("inf")])
    def test_integer_rejects_nan_and_infinity(self, value: float) -> None:
        tools, _ = _tools_with(_count_schema())
        tools.handle("repeat", lambda count: "ok")

        _, is_error = tools.execute_tool_call("repeat", {"count": value})

        assert is_error is True

    def test_number_still_accepts_a_fractional_value(self) -> None:
        tools, _ = _tools_with(_count_schema())
        tools.handle("repeat", lambda count=1, scale=1.0: "ok")

        assert tools.execute_tool_call("repeat", {"count": 1, "scale": 1.5}) == ("ok", False)

    @pytest.mark.parametrize("args", [None, "left", ["left"], 3])
    def test_non_object_arguments_are_an_error_result(self, args: Any) -> None:
        """A model (or a permission callback's updated_input) can hand over a
        non-object: report it to the model, never crash on args.items()."""
        tools, _ = _tools_with(_align_schema())
        calls: List[str] = []
        tools.handle("align_text", lambda **kwargs: calls.append("ran") or "ok")

        result, is_error = tools.execute_tool_call("align_text", args)

        assert is_error is True
        assert json.loads(result) == {"error": "Tool arguments must be an object"}
        assert calls == []  # the handler never ran

    def test_no_schema_means_no_validation(self) -> None:
        tools = T2VTools(MagicMock())
        tools.handle("free_form", lambda **kwargs: json.dumps(kwargs))

        result, is_error = tools.execute_tool_call("free_form", {"anything": 1})

        assert (json.loads(result), is_error) == ({"anything": 1}, False)


class TestPermissions:
    def test_no_permission_allows(self) -> None:
        tools = T2VTools(MagicMock())
        tools.handle("open_file", lambda path: "ok")

        assert tools.check_permission("open_file", {"path": "a"}) == PermissionCheck(
            action="allow"
        )
        assert tools.check_permission("never_registered", {}) == PermissionCheck(action="allow")

    def test_permission_true_requires_approval(self) -> None:
        client = MagicMock()
        client.request.return_value = {"registered": ["delete_volume"], "count": 1}
        tools = T2VTools(client)

        @tool(permission=True)
        def delete_volume(name: str) -> str:
            """Delete a volume."""
            return "deleted " + name

        tools.register_from_functions([delete_volume])

        assert tools.check_permission("delete_volume", {"name": "CT"}) == PermissionCheck(
            action="require_approval"
        )
        # `permission` is local policy — it never travels to the engine.
        sent = client.request.call_args[1]["body"]["tools"][0]
        assert "permission" not in sent

    def test_permission_via_handle_and_bound_method(self) -> None:
        tools = T2VTools(MagicMock())
        tools.handle("save", lambda: "ok", permission=True)
        assert tools.check_permission("save", {}).action == "require_approval"

        tools.handle("save", lambda: "ok")  # re-registering without it clears it
        assert tools.check_permission("save", {}).action == "allow"

    def test_re_registering_without_a_permission_warns_that_it_is_dropped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tools = T2VTools(MagicMock())
        tools.handle("save", lambda: "ok", permission=True)

        with caplog.at_level(logging.WARNING, logger="talk2view.tools"):
            tools.handle("save", lambda: "ok")

        assert "save" in caplog.text
        assert "permission" in caplog.text
        assert tools.check_permission("save", {}).action == "allow"

    def test_re_registering_a_tool_that_never_had_a_permission_is_quiet(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tools = T2VTools(MagicMock())
        tools.handle("save", lambda: "ok")

        with caplog.at_level(logging.WARNING, logger="talk2view.tools"):
            tools.handle("save", lambda: "ok")

        assert caplog.text == ""

    @pytest.mark.parametrize("bad", ["always", 1, {"ask": True}, object()])
    def test_a_permission_that_is_not_a_bool_or_callable_is_rejected(self, bad: Any) -> None:
        """Caught at registration: a truthy string would otherwise be stored
        and then called, raising inside the turn instead of here."""
        tools = T2VTools(MagicMock())

        with pytest.raises(TypeError, match="permission must be a bool or a callable"):
            tools.handle("save", lambda: "ok", permission=bad)

        assert tools.has_handler("save") is False  # nothing half-registered

    def test_a_bad_permission_on_the_decorator_is_rejected_at_registration(self) -> None:
        client = MagicMock()
        client.request.return_value = {"registered": ["wipe"], "count": 1}
        tools = T2VTools(client)

        @tool(permission="always")  # type: ignore[arg-type]
        def wipe() -> str:
            """Wipe the scene."""
            return "wiped"

        with pytest.raises(TypeError, match="permission must be a bool or a callable"):
            tools.register_from_functions([wipe])

        class Host:
            def wipe(self) -> str:
                """Wipe the scene."""
                return "wiped"

        wrapped = tool(Host().wipe, permission=True)
        assert wrapped._t2v_permission is True

    def test_permission_callback_deny_and_updated_input(self) -> None:
        seen: List[tuple] = []  # type: ignore[type-arg]

        def policy(tool_name: str, args: Dict[str, Any]) -> PermissionResult:
            seen.append((tool_name, dict(args)))
            if args.get("path", "").startswith("/etc"):
                return PermissionResult.deny("system files are off limits")
            return PermissionResult.allow({"path": args["path"], "read_only": True})

        tools = T2VTools(MagicMock())
        tools.handle("open_file", lambda path, read_only=False: "ok", permission=policy)

        denied = tools.check_permission("open_file", {"path": "/etc/passwd"})
        allowed = tools.check_permission("open_file", {"path": "scan.nrrd"})

        assert denied == PermissionCheck(action="deny", message="system files are off limits")
        assert allowed == PermissionCheck(
            action="allow", updated_input={"path": "scan.nrrd", "read_only": True}
        )
        assert seen[0] == ("open_file", {"path": "/etc/passwd"})

    def test_permission_callback_plain_allow_and_bare_deny(self) -> None:
        tools = T2VTools(MagicMock())
        tools.handle("a", lambda: "ok", permission=lambda n, a: PermissionResult.allow())
        tools.handle("b", lambda: "ok", permission=lambda n, a: PermissionResult.deny())

        assert tools.check_permission("a", {}) == PermissionCheck(action="allow")
        assert tools.check_permission("b", {}) == PermissionCheck(action="deny")

    def test_permission_callback_that_raises_is_a_denial(self) -> None:
        def broken(name: str, args: Dict[str, Any]) -> PermissionResult:
            raise RuntimeError("policy service unreachable")

        tools = T2VTools(MagicMock())
        tools.handle("delete_volume", lambda: "ok", permission=broken)

        assert tools.check_permission("delete_volume", {}) == PermissionCheck(
            action="deny",
            message="Permission check failed (RuntimeError); the call was denied.",
        )

    @pytest.mark.parametrize(
        "returned", [None, True, "deny", {"type": "allow"}, PermissionCheck(action="allow")]
    )
    def test_permission_callback_returning_junk_is_a_denial(self, returned: Any) -> None:
        """A partner callback that returns the wrong shape must deny, not
        crash the turn with an AttributeError out of chat()."""
        tools = T2VTools(MagicMock())
        tools.handle("delete_volume", lambda: "ok", permission=lambda n, a: returned)

        assert tools.check_permission("delete_volume", {}) == PermissionCheck(
            action="deny",
            message="Permission check failed (invalid result); the call was denied.",
        )


class TestDescriptionAndReRegister:
    def test_get_description(self) -> None:
        tools, _ = _tools_with(_align_schema())
        assert tools.get_description("align_text") == "Align the selected text."
        assert tools.get_description("unknown") == ""

    def test_re_register_none_when_empty(self) -> None:
        client = MagicMock()
        tools = T2VTools(client)

        assert tools.re_register() is None
        client.request.assert_not_called()

    def test_re_register_resends_the_same_schemas(self) -> None:
        tools, client = _tools_with(_align_schema())
        first_body = client.request.call_args[1]["body"]
        client.request.reset_mock()
        client.request.return_value = {"registered": ["align_text"], "count": 1}

        response = tools.re_register()

        assert response is not None
        assert response.registered == ["align_text"]
        client.request.assert_called_once_with(
            "/v1/tools/register", method="POST", body=first_body
        )

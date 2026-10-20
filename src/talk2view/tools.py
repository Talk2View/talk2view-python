"""Tools — @tool decorator, schema generation, handler registry."""

from __future__ import annotations

import inspect
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .cancel import Cancelled, CancelToken
from .client import T2VClient
from .dispatch import Dispatcher, InlineDispatcher, run_on_main
from .types import (
    ClientToolSchema,
    PermissionCheck,
    PermissionResult,
    RegisterToolsResponse,
)

logger = logging.getLogger("talk2view.tools")

# Type alias for tool handlers
ToolHandler = Callable[..., Any]

# Programmatic per-call permission decision: (tool_name, args) -> PermissionResult.
ToolPermissionCallback = Callable[[str, Dict[str, Any]], PermissionResult]

# `True` = always ask the end-user; a callback = decide in code; False/None = run.
ToolPermission = Union[bool, ToolPermissionCallback]


def strip_null_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Drop ``None`` values and empty dicts from tool args.

    Models often send null, or an empty object, for optional parameters they do
    not mean to set. Dropping both keeps such placeholders from tripping type
    validation (e.g. ``section: {}`` for an optional string).
    """
    clean: Dict[str, Any] = {}
    for key, value in args.items():
        if value is None:
            continue
        if isinstance(value, dict) and not value:
            continue
        clean[key] = value
    return clean


def _check_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    # bool is an int subclass in Python; JSON keeps them apart.
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        # JSON has a single number type, so 3.0 on the wire IS the integer 3 —
        # but 1.5 is not, and nor are NaN/Infinity. The TypeScript SDK's
        # checkType() accepts any `number` for "integer", so it takes 1.5;
        # this rejects it rather than hand a handler a value it cannot index
        # or slice with.
        return isinstance(value, float) and value.is_integer()
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _enum_text(value: Any) -> str:
    """How a JSON scalar reads as text (``true``, not Python's ``True``)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# ── Python type → JSON Schema type mapping ──

_TYPE_MAP: Dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _python_type_to_json_schema(annotation: Any) -> Dict[str, Any]:
    """Convert a Python type annotation to a JSON Schema type descriptor."""
    if annotation is inspect.Parameter.empty or annotation is None:
        return {"type": "string"}

    # Direct type mapping
    if annotation in _TYPE_MAP:
        return {"type": _TYPE_MAP[annotation]}

    # Handle string annotations (from __future__ annotations)
    if isinstance(annotation, str):
        simple_map = {
            "str": "string",
            "int": "integer",
            "float": "number",
            "bool": "boolean",
        }
        if annotation in simple_map:
            return {"type": simple_map[annotation]}

        # Handle list[X] pattern
        list_match = re.match(r"(?:list|List)\[(.+)\]", annotation)
        if list_match:
            inner = list_match.group(1).strip()
            inner_schema = _python_type_to_json_schema(inner)
            return {"type": "array", "items": inner_schema}

        # Handle Optional[X]
        optional_match = re.match(r"Optional\[(.+)\]", annotation)
        if optional_match:
            inner = optional_match.group(1).strip()
            return _python_type_to_json_schema(inner)

        # Handle dict[X, Y]
        dict_match = re.match(r"(?:dict|Dict)\[(.+),\s*(.+)\]", annotation)
        if dict_match:
            return {"type": "object"}

        return {"type": "string"}

    # Handle typing generics at runtime (Python 3.9+)
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())

    if origin is list:
        if args:
            inner_schema = _python_type_to_json_schema(args[0])
            return {"type": "array", "items": inner_schema}
        return {"type": "array"}

    if origin is dict:
        return {"type": "object"}

    # Optional[X] = Union[X, None]
    if origin is Union:
        # Filter out NoneType
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _python_type_to_json_schema(non_none[0])

    return {"type": "string"}


# ── Docstring parsing ──

_SECTION_HEADERS = frozenset({
    "Returns:", "Return:", "Raises:", "Yields:",
    "Note:", "Notes:", "Example:", "Examples:",
})

_ARG_HEADERS = frozenset({"Args:", "Arguments:", "Parameters:", "Params:"})


def _parse_google_docstring(doc: Optional[str]) -> Tuple[str, Dict[str, str]]:
    """Parse a Google-style docstring into description + param descriptions.

    Returns:
        (description, {param_name: param_description})
    """
    if not doc:
        return ("", {})

    lines = doc.strip().split("\n")
    description_lines: List[str] = []
    param_docs: Dict[str, str] = {}
    in_args = False
    description_done = False
    current_param: Optional[str] = None

    for line in lines:
        stripped = line.strip()

        if stripped in _ARG_HEADERS:
            in_args = True
            description_done = True
            continue

        if stripped in _SECTION_HEADERS:
            in_args = False
            description_done = True
            continue

        if in_args:
            # Try to match "param_name (type): description" or "param_name: description"
            param_match = re.match(
                r"(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)",
                stripped,
            )
            if param_match:
                current_param = param_match.group(1)
                param_docs[current_param] = param_match.group(2).strip()
            elif current_param and stripped:
                # Continuation line for current param
                param_docs[current_param] += " " + stripped
        elif not description_done:
            if not stripped:
                if description_lines:
                    description_done = True
            else:
                description_lines.append(stripped)

    description = " ".join(description_lines)
    return (description, param_docs)


# ── Schema generation ──


def _generate_schema(func: Callable[..., Any]) -> ClientToolSchema:
    """Generate a ClientToolSchema from a function's signature and docstring."""
    sig = inspect.signature(func)
    description, param_docs = _parse_google_docstring(func.__doc__)

    properties: Dict[str, Any] = {}
    required: List[str] = []

    for name, param in sig.parameters.items():
        # Skip self/cls
        if name in ("self", "cls"):
            continue

        type_schema = _python_type_to_json_schema(param.annotation)
        prop: Dict[str, Any] = dict(type_schema)

        # Add description from docstring
        if name in param_docs:
            prop["description"] = param_docs[name]

        properties[name] = prop

        # Required if no default value
        if param.default is inspect.Parameter.empty:
            required.append(name)

    func_name = getattr(func, "__name__", str(func))

    return ClientToolSchema(
        name=func_name,
        description=description or func_name,
        parameters={
            "type": "object",
            "properties": properties,
            "required": required,
        },
    )


# ── Pydantic schema extraction ──


def _resolve_ref(ref: str, defs: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a JSON Schema ``$ref`` against ``$defs``."""
    # $ref looks like "#/$defs/MyModel"
    name = ref.rsplit("/", 1)[-1]
    return dict(defs.get(name, {}))


def _simplify_prop(prop_schema: Dict[str, Any], defs: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a single property schema into a simple type descriptor.

    Handles pydantic v2 patterns:
    - ``$ref`` → resolved from ``$defs``
    - ``anyOf`` (used for Optional[X]) → unwrap to the non-null type
    - ``allOf`` with single ``$ref`` → resolve
    - nested ``items`` with ``$ref`` → resolve
    """
    # Resolve $ref at top level
    if "$ref" in prop_schema:
        prop_schema = _resolve_ref(prop_schema["$ref"], defs)

    # Handle allOf with single $ref (pydantic v2 pattern for required nested models)
    if "allOf" in prop_schema and len(prop_schema["allOf"]) == 1:
        inner = prop_schema["allOf"][0]
        if "$ref" in inner:
            resolved = _resolve_ref(inner["$ref"], defs)
            # Preserve description from the outer schema
            if "description" in prop_schema:
                resolved = dict(resolved)
                resolved["description"] = prop_schema["description"]
            prop_schema = resolved

    # Handle anyOf (typically Optional[X] = anyOf: [{type: X}, {type: null}])
    if "anyOf" in prop_schema:
        non_null = [
            branch for branch in prop_schema["anyOf"]
            if branch.get("type") != "null"
        ]
        if len(non_null) == 1:
            inner = non_null[0]
            if "$ref" in inner:
                inner = _resolve_ref(inner["$ref"], defs)
            prop_schema = {**inner}
            # Preserve description/default from outer
            for key in ("description", "default"):
                if key in prop_schema:
                    continue
                # Check original anyOf parent (already overwritten, but we keep
                # description if it was on the outer level)

    prop: Dict[str, Any] = {}
    if "type" in prop_schema:
        prop["type"] = prop_schema["type"]
    if "description" in prop_schema:
        prop["description"] = prop_schema["description"]
    if "items" in prop_schema:
        items = prop_schema["items"]
        if "$ref" in items:
            items = _resolve_ref(items["$ref"], defs)
        prop["items"] = items
    if "enum" in prop_schema:
        prop["enum"] = prop_schema["enum"]
    if "default" in prop_schema:
        prop["default"] = prop_schema["default"]
    # Fall back to string if no type resolved
    if "type" not in prop:
        prop["type"] = "string"
    return prop


def _schema_from_pydantic(
    func: Callable[..., Any], model: Any, return_direct: bool = False
) -> ClientToolSchema:
    """Build a ClientToolSchema from a pydantic BaseModel class.

    Handles pydantic v2 JSON Schema output including ``$defs``, ``$ref``,
    and ``anyOf`` patterns for Optional fields.
    """
    func_name = getattr(func, "__name__", str(func))
    description, _ = _parse_google_docstring(func.__doc__)

    json_schema = model.model_json_schema()
    defs = json_schema.get("$defs", {})

    properties: Dict[str, Any] = {}
    required: List[str] = json_schema.get("required", [])

    for prop_name, prop_schema in json_schema.get("properties", {}).items():
        properties[prop_name] = _simplify_prop(prop_schema, defs)

    return ClientToolSchema(
        name=func_name,
        description=description or func_name,
        parameters={
            "type": "object",
            "properties": properties,
            "required": required,
        },
        return_direct=return_direct,
    )


# ── @tool decorator ──


class _ToolWrapper:
    """Wraps a callable with tool schema metadata.

    Exposes .name, .description, and .args for compatibility with code
    that may inspect tool objects (e.g., LangChain-style patterns).
    """

    def __init__(
        self,
        func: Callable[..., Any],
        schema: ClientToolSchema,
        permission: Optional[ToolPermission] = None,
    ) -> None:
        self._t2v_tool_schema = schema
        self._t2v_handler = func
        self._t2v_permission = permission
        self._func = func
        # Copy function metadata
        self.__name__ = getattr(func, "__name__", str(func))
        self.__doc__ = func.__doc__
        self.__wrapped__ = func
        # LangChain-compatible properties
        self.name = schema.name
        self.description = schema.description
        self.args = schema.parameters.get("properties", {})

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._func(*args, **kwargs)


def tool(
    func: Optional[Callable[..., Any]] = None,
    *,
    args_schema: Any = None,
    return_direct: bool = False,
    permission: Optional[ToolPermission] = None,
) -> Any:
    """Decorator that attaches tool schema metadata to a function.

    Usage:
        @tool
        def my_tool(name: str, count: int) -> str:
            '''Does something useful.

            Args:
                name: The name to use.
                count: How many times.
            '''
            return "result"

    Also works as a plain function call for bound methods:
        self.tools = [tool(self.get_data_context), tool(self.set_visibility)]

    Accepts optional args_schema (pydantic BaseModel) for compatibility with
    LangChain-style tool definitions. If provided, the schema is extracted from
    the pydantic model instead of the function signature.

    Args:
        func: The function to wrap.
        args_schema: Optional pydantic BaseModel class defining input parameters.
        return_direct: If True, marks the tool to return output directly to the user.
        permission: ``True`` = every call needs the end-user's approval (the
            stream pauses with an ``approval_required`` event). A callable
            ``(tool_name, args) -> PermissionResult`` decides per call in code.
            ``False``/omitted = run without asking.
    """
    def _wrap(fn: Callable[..., Any]) -> Any:
        if args_schema is not None:
            schema = _schema_from_pydantic(fn, args_schema, return_direct)
        else:
            schema = _generate_schema(fn)
            if return_direct:
                schema.return_direct = True

        # Try setting attributes directly (works for regular functions)
        try:
            fn._t2v_tool_schema = schema  # type: ignore[attr-defined]
            fn._t2v_handler = fn  # type: ignore[attr-defined]
            fn._t2v_permission = permission  # type: ignore[attr-defined]
            fn.name = schema.name  # type: ignore[attr-defined]
            fn.description = schema.description  # type: ignore[attr-defined]
            fn.args = schema.parameters.get("properties", {})  # type: ignore[attr-defined]
            return fn
        except AttributeError:
            # Bound methods don't allow attribute setting — use a wrapper
            return _ToolWrapper(fn, schema, permission)

    # Support both @tool and tool(func, args_schema=...) call styles
    if func is not None:
        return _wrap(func)
    return _wrap


# ── T2VTools class ──


class T2VTools:
    """Tool management — handler registry, schema registration with the server."""

    def __init__(self, client: T2VClient, dispatcher: Optional[Dispatcher] = None) -> None:
        self._client = client
        self._dispatcher: Dispatcher = dispatcher if dispatcher is not None else InlineDispatcher()
        self._handlers: Dict[str, ToolHandler] = {}
        self._schemas: List[ClientToolSchema] = []
        self._permissions: Dict[str, ToolPermission] = {}

    @property
    def dispatcher(self) -> Dispatcher:
        """Where handlers and permission callbacks run (host main thread)."""
        return self._dispatcher

    def handle(
        self,
        tool_name: str,
        handler: ToolHandler,
        permission: Optional[ToolPermission] = None,
    ) -> None:
        """Register a handler for a specific tool name.

        Re-registering a tool WITHOUT ``permission=`` drops the permission it
        had; that is logged, because silently un-gating a tool is a change
        nobody asks for on purpose.

        Args:
            permission: ``True`` = ask the end-user before every call; a
                callable ``(tool_name, args) -> PermissionResult`` decides per
                call; ``False``/omitted = run without asking.

        Raises:
            TypeError: if ``permission`` is neither ``None``, a ``bool``, nor
                callable. Nothing is registered in that case.
        """
        self._set_permission(tool_name, permission)
        self._handlers[tool_name] = handler

    def _set_permission(self, tool_name: str, permission: Optional[ToolPermission]) -> None:
        if permission is not None and not isinstance(permission, bool) and not callable(
            permission
        ):
            # Caught here rather than at call time: a truthy non-callable would
            # be stored and then invoked, raising inside the turn instead.
            raise TypeError("permission must be a bool or a callable")
        if permission is None or permission is False:
            if permission is None and tool_name in self._permissions:
                logger.warning(
                    "Re-registering tool %r without permission= drops the permission it "
                    "had; pass permission= again to keep asking the end-user.",
                    tool_name,
                )
            self._permissions.pop(tool_name, None)
        else:
            self._permissions[tool_name] = permission

    @staticmethod
    def _payload(tools: List[ClientToolSchema]) -> List[Dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "return_direct": t.return_direct,
            }
            for t in tools
        ]

    def _post_schemas(self, tools: List[ClientToolSchema]) -> RegisterToolsResponse:
        data = self._client.request(
            "/v1/tools/register",
            method="POST",
            body={"tools": self._payload(tools)},
        )
        return RegisterToolsResponse(
            registered=data.get("registered", []),
            count=data.get("count", 0),
        )

    def register(self, tools: List[ClientToolSchema]) -> RegisterToolsResponse:
        """Register tool schemas with the server.

        Does NOT register handlers — use handle() or register_from_functions()
        for that.
        """
        self._schemas = list(tools)
        return self._post_schemas(self._schemas)

    def re_register(self) -> Optional[RegisterToolsResponse]:
        """Re-send the already-registered schemas to the server.

        Tools are session-scoped on the engine, so a new session (first chat,
        clear, or recovery after the engine lost the old one) needs them again.
        Returns None when nothing has been registered.
        """
        if not self._schemas:
            return None
        return self._post_schemas(self._schemas)

    def register_from_functions(
        self,
        functions: List[Callable[..., Any]],
    ) -> RegisterToolsResponse:
        """Extract schemas from @tool-decorated functions and register.

        Registers both handlers locally and schemas with the server.
        Functions without @tool get schemas generated on the fly.

        Raises:
            TypeError: if a ``@tool(permission=...)`` is neither ``None``, a
                ``bool``, nor callable.
        """
        schemas: List[ClientToolSchema] = []
        for func in functions:
            schema: ClientToolSchema = getattr(func, "_t2v_tool_schema", None)  # type: ignore[assignment]
            if schema is None:
                schema = _generate_schema(func)

            schemas.append(schema)

            handler: ToolHandler = getattr(func, "_t2v_handler", func)
            self._set_permission(schema.name, getattr(func, "_t2v_permission", None))
            self._handlers[schema.name] = handler

        return self.register(schemas)

    def validate_args(self, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
        """Validate ``args`` against the registered schema.

        Returns an error message, or None when valid (or when the tool has no
        registered schema). String enum values are matched case-insensitively
        and NORMALISED IN PLACE to the schema's casing, so a model echoing the
        UI label "Center" for the enum value "center" does not cost a
        reject-and-retry round trip.
        """
        schema = next((s for s in self._schemas if s.name == tool_name), None)
        if schema is None:
            return None

        properties: Dict[str, Any] = schema.parameters.get("properties") or {}
        required: List[str] = schema.parameters.get("required") or []

        for field_name in required:
            if field_name not in args:
                return "Missing required argument: " + field_name

        for key, value in list(args.items()):
            prop = properties.get(key)
            if not isinstance(prop, dict):
                continue  # allow extra properties

            expected = prop.get("type")
            if isinstance(expected, str) and not _check_type(value, expected):
                return 'Argument "{}" expected type "{}", got {}'.format(
                    key, expected, type(value).__name__
                )

            enum = prop.get("enum")
            if enum:
                allowed = ", ".join(_enum_text(e) for e in enum)
                if expected == "string" and isinstance(value, str):
                    match = next(
                        (e for e in enum if isinstance(e, str) and e.lower() == value.lower()),
                        None,
                    )
                    if match is None:
                        return 'Argument "{}" must be one of: {}'.format(key, allowed)
                    args[key] = match
                elif _enum_text(value) not in [_enum_text(e) for e in enum]:
                    return 'Argument "{}" must be one of: {}'.format(key, allowed)

        return None

    def execute_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        cancel: Optional[CancelToken] = None,
    ) -> Tuple[str, bool]:
        """Execute a tool call locally. Returns (result_string, is_error).

        Null / empty-object args are stripped and the rest validated against
        the registered schema first; a validation failure is returned to the
        model as an error result, and the handler does not run.

        The handler runs on the dispatcher's main thread. Raises ``Cancelled``
        if ``cancel`` fires while the handler is still queued.
        """
        handler = self._handlers.get(tool_name)

        if not handler:
            return (json.dumps({"error": "Unknown tool: " + tool_name}), True)

        if not isinstance(args, dict):
            # A model, or a permission callback's updated_input, can hand over
            # something that is not an object. Report it instead of raising an
            # AttributeError out of the caller's chat() loop.
            return (json.dumps({"error": "Tool arguments must be an object"}), True)

        clean_args = strip_null_args(args)

        validation_error = self.validate_args(tool_name, clean_args)
        if validation_error:
            return (json.dumps({"error": validation_error}), True)

        try:
            result = run_on_main(self._dispatcher, lambda: handler(**clean_args), cancel)
            if not isinstance(result, str):
                result = json.dumps(result)
            return (result, False)
        except Cancelled:
            raise  # a stop is not a tool failure — never report it to the model
        except Exception as exc:
            return (json.dumps({"error": str(exc)}), True)

    def check_permission(
        self,
        tool_name: str,
        args: Dict[str, Any],
        cancel: Optional[CancelToken] = None,
    ) -> PermissionCheck:
        """Decide whether a tool call may run.

        - no permission configured → allow
        - ``permission=True`` → require_approval (ask the end-user)
        - ``permission=callback`` → the callback decides (allow may rewrite the
          input through ``updated_input``; deny carries a ``message``)

        A callback runs on the dispatcher's main thread, like a handler.
        Permission checks fail closed: a callback that raises, or that returns
        anything other than a ``PermissionResult``, is a denial. ``Cancelled``
        still propagates.
        """
        permission = self._permissions.get(tool_name)
        if permission is None or permission is False:
            return PermissionCheck(action="allow")
        if permission is True:
            return PermissionCheck(action="require_approval")
        callback: ToolPermissionCallback = permission
        try:
            result = run_on_main(self._dispatcher, lambda: callback(tool_name, args), cancel)
        except Cancelled:
            raise
        except Exception as exc:
            return PermissionCheck(
                action="deny",
                message="Permission check failed ({}); the call was denied.".format(
                    type(exc).__name__
                ),
            )
        if not isinstance(result, PermissionResult):
            return PermissionCheck(
                action="deny",
                message="Permission check failed (invalid result); the call was denied.",
            )
        if result.type == "allow":
            return PermissionCheck(action="allow", updated_input=result.updated_input)
        return PermissionCheck(action="deny", message=result.message)

    def has_handler(self, tool_name: str) -> bool:
        """Check if a handler is registered for a tool."""
        return tool_name in self._handlers

    def get_description(self, tool_name: str) -> str:
        """Description of a registered tool ('' when unknown)."""
        schema = next((s for s in self._schemas if s.name == tool_name), None)
        return schema.description if schema is not None else ""

    def get_registered(self) -> List[ClientToolSchema]:
        """Get all registered tool schemas."""
        return list(self._schemas)

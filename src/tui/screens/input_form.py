"""Bounded JSON-Schema input form, preview and explicit confirmation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, TextArea

from ..state import JSON, Object, Schema, TuiContext, json_plain


def validate_input(
    value: JSON, schema: Schema, root: Schema | None = None, path: str = "$", depth: int = 0
) -> None:
    """Validate the supported schema vocabulary; unsupported assertions fail closed.

    Local references only. Providers must validate again at the mutation boundary.
    Unknown assertions are not silently treated as successful validation.
    """
    if depth > 64:
        raise ValueError(f"{path}: schema nesting exceeds 64")
    root = schema if root is None else root
    ref = schema.get("$ref")
    if ref is not None:
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise ValueError("Only local JSON Schema references are supported")
        target: JSON = root
        for part in ref[2:].split("/"):
            if not isinstance(target, Mapping):
                raise ValueError("Invalid local schema reference")
            target = target[part.replace("~1", "/").replace("~0", "~")]
        if not isinstance(target, Mapping):
            raise ValueError("Schema reference must resolve to an object")
        validate_input(value, target, root, path, depth + 1)
    supported = {
        "$schema",
        "$id",
        "$ref",
        "$defs",
        "definitions",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "type",
        "enum",
        "const",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "pattern",
        "anyOf",
        "oneOf",
        "allOf",
    }
    unknown = set(schema) - supported
    if unknown:
        raise ValueError(f"Unsupported schema assertions: {', '.join(sorted(unknown))}")
    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(keyword)
        if branches is None:
            continue
        if not isinstance(branches, list):
            raise ValueError(f"{keyword} must contain schemas")
        matches = 0
        for branch in branches:
            if not isinstance(branch, Mapping):
                raise ValueError("Schema branches must be objects")
            try:
                validate_input(value, branch, root, path, depth + 1)
            except ValueError:
                continue
            matches += 1
        if (
            (keyword == "anyOf" and matches == 0)
            or (keyword == "oneOf" and matches != 1)
            or (keyword == "allOf" and matches != len(branches))
        ):
            raise ValueError(f"{path}: does not satisfy {keyword}")
    types = schema.get("type")
    allowed = types if isinstance(types, list) else [types] if types is not None else []
    actual = (
        "null"
        if value is None
        else "boolean"
        if isinstance(value, bool)
        else "integer"
        if isinstance(value, int)
        else "number"
        if isinstance(value, float)
        else "string"
        if isinstance(value, str)
        else "array"
        if isinstance(value, list)
        else "object"
    )
    if allowed and actual not in allowed and not (actual == "integer" and "number" in allowed):
        raise ValueError(f"{path}: expected {types}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: select a declared enum value")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path}: value does not match const")
    if isinstance(value, Mapping):
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise ValueError("required must be an array")
        missing = set(required) - set(value)
        if missing:
            raise ValueError(f"{path}: missing {', '.join(sorted(missing))}")
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ValueError("properties must be an object")
        for name, item in value.items():
            subschema = properties.get(name, schema.get("additionalProperties", True))
            if subschema is False:
                raise ValueError(f"{path}.{name}: additional property not allowed")
            if isinstance(subschema, Mapping):
                validate_input(item, subschema, root, f"{path}.{name}", depth + 1)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get(
            "maxItems", 1000000
        ):
            raise ValueError(f"{path}: array size outside bounds")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                validate_input(item, item_schema, root, f"{path}[{index}]", depth + 1)
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get(
            "maxLength", 1000000
        ):
            raise ValueError(f"{path}: string length outside bounds")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise ValueError(f"{path}: string does not match pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, reject in (
            ("minimum", lambda n: value < n),
            ("maximum", lambda n: value > n),
            ("exclusiveMinimum", lambda n: value <= n),
            ("exclusiveMaximum", lambda n: value >= n),
        ):
            if key in schema and reject(schema[key]):
                raise ValueError(f"{path}: outside {key}")


class InputForm(ModalScreen):
    """Fields contain JSON values, so nested/nullable inputs retain their types."""

    DEFAULT_CSS = """
    InputForm { align: center middle; }
    InputForm > VerticalScroll {
        width: 85%; height: 85%; padding: 1 2;
        border: solid $accent; background: $surface;
    }
    InputForm Input { margin-bottom: 1; }
    InputForm TextArea { height: 8; }
    InputForm #form-preview { height: auto; max-height: 10; }
    """
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(
        self,
        schema: Schema,
        context: TuiContext,
        submit: Callable[[Object], None],
        title: str = "Start run",
    ) -> None:
        super().__init__()
        self.schema = schema
        self.context = context
        self.submit = submit
        self.title_text = title
        properties = schema.get("properties", {})
        self.properties = properties if isinstance(properties, Mapping) else {}
        self._payload: Object | None = None
        self._validated_token: tuple | None = None
        self._names: list[str] = []

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label(self.title_text)
            yield Static(
                "JSON values preserve types. Blank optional fields are omitted. "
                "Defaults shown below require preview and confirmation."
            )
            for index, (name, schema) in enumerate(self.properties.items()):
                self._names.append(name)
                if not isinstance(schema, Mapping):
                    continue
                required = name in self.schema.get("required", [])
                yield Label(
                    f"{name}{' *' if required else ''} · {schema.get('type', 'JSON')}"
                    + (f" · enum {schema['enum']}" if "enum" in schema else "")
                )
                value = (
                    json.dumps(json_plain(schema["default"]), allow_nan=False)
                    if "default" in schema
                    else ""
                )
                yield Input(value=value, id=f"field-{index}")
            yield Label("Raw JSON object (overrides fields when nonempty)")
            yield TextArea(id="raw-input")
            yield Static(id="form-preview", markup=False)
            yield Button("Preview validated input", id="form-validate")
            yield Button("Confirm and submit", id="form-submit", disabled=True)
            yield Button("Cancel", id="form-cancel")

    def on_mount(self) -> None:
        # The dashboard intentionally has no keyboard-focused editor. Establish
        # modal focus explicitly, including schemas with no property controls.
        inputs = list(self.query(Input))
        (inputs[0] if inputs else self.query_one("#raw-input", TextArea)).focus()

    def _input(self) -> Object:
        raw = self.query_one("#raw-input", TextArea).text
        payload = (
            json.loads(raw)
            if raw.strip()
            else {
                name: json.loads(self.query_one(f"#field-{i}", Input).value)
                for i, name in enumerate(self._names)
                if self.query_one(f"#field-{i}", Input).value.strip()
            }
        )
        if not isinstance(payload, dict):
            raise ValueError("Input must be a JSON object")
        if len(json.dumps(payload, allow_nan=False).encode("utf-8")) > 1024 * 1024:
            raise ValueError("Input exceeds 1 MiB")
        validate_input(payload, json_plain(self.schema))
        return payload

    def _input_token(self) -> tuple:
        return (
            self.query_one("#raw-input", TextArea).text,
            *(self.query_one(f"#field-{i}", Input).value for i in range(len(self._names))),
        )

    def on_input_changed(self) -> None:
        # A queued Changed event may arrive after a Preview click. Do not erase
        # a preview for the exact values that were just validated.
        if self._validated_token is not None and self._input_token() == self._validated_token:
            return
        self._validated_token = None
        self._payload = None
        self.query_one("#form-submit", Button).disabled = True

    def on_text_area_changed(self) -> None:
        self.on_input_changed()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "form-cancel":
            self.dismiss()
        elif event.button.id == "form-validate":
            try:
                self._payload = self._input()
                self._validated_token = self._input_token()
            except (ValueError, KeyError) as error:
                self.query_one("#form-preview", Static).update(str(error))
                return
            self.query_one("#form-preview", Static).update(json.dumps(self._payload, indent=2))
            self.query_one("#form-submit", Button).disabled = False
        elif event.button.id == "form-submit" and self._payload is not None:
            if self._validated_token != self._input_token():
                self.on_input_changed()
                return
            payload = self._payload
            self.dismiss()
            if self.context == self.app.extension_context:
                self.submit(payload)

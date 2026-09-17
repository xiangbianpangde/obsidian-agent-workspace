"""Frozen contract validation for the Paper Workbench.

The three JSON Schemas in ``schemas/`` are the frozen P0 interface:

- ``paper.workbench.schema.json``   — in-folder manifest (identity + bindings)
- ``paper.annotations.schema.json`` — authoritative annotation sidecar
- ``ai-context.v1.schema.json``     — P1 context envelope (defined, never called in P0)

Validation is deliberately dependency-free: ``jsonschema`` is not installed and
the project avoids adding a runtime dependency for a contract that only changes
with an ADR. The subset implemented here covers exactly the keywords these
schemas use. Anything outside that subset is rejected at load time, so a schema
edit can never silently weaken validation.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from . import AI_CONTEXT_SCHEMA_PATH, ANNOTATIONS_SCHEMA_PATH, MANIFEST_SCHEMA_PATH

__all__ = [
    "SchemaError",
    "load_schema",
    "validate",
    "validate_manifest",
    "validate_annotations",
    "validate_ai_context",
    "SUPPORTED_KEYWORDS",
]

# Keywords this validator understands. A schema using anything else fails closed
# at load time rather than silently skipping the constraint.
SUPPORTED_KEYWORDS = {
    "$schema",
    "$id",
    "$defs",
    "$ref",
    "title",
    "description",
    "type",
    "const",
    "enum",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "minItems",
    "uniqueItems",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
    "oneOf",
    "not",
    "format",
}

_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
    "null": type(None),
}

_SCALAR_KEYWORDS = {
    "const",
    "enum",
    "type",
    "format",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "minItems",
    "uniqueItems",
    "required",
    "additionalProperties",
}


class SchemaError(Exception):
    """Raised when a document violates its frozen contract."""

    def __init__(self, pointer: str, message: str):
        self.pointer = pointer or "/"
        self.message = message
        super().__init__(f"{self.pointer}: {message}")


@lru_cache(maxsize=8)
def load_schema(path: str) -> Dict[str, Any]:
    """Load a schema document and confirm it stays inside the enforced subset."""
    with open(path, "r", encoding="utf-8") as handle:
        schema = json.load(handle)
    _assert_supported(schema, "#")
    return schema


def _assert_supported(node: Any, pointer: str) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key not in SUPPORTED_KEYWORDS:
                raise SchemaError(
                    f"{pointer}/{key}",
                    f"unsupported schema keyword {key!r}; refusing to validate "
                    "against a contract this validator cannot fully enforce",
                )
            if key in ("properties", "$defs"):
                for sub_key, sub_value in value.items():
                    _assert_supported(sub_value, f"{pointer}/{key}/{sub_key}")
            elif key == "items":
                _assert_supported(value, f"{pointer}/items")
            elif key == "oneOf":
                for index, sub in enumerate(value):
                    _assert_supported(sub, f"{pointer}/oneOf/{index}")
            elif key == "not":
                _assert_supported(value, f"{pointer}/not")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _assert_supported(item, f"{pointer}/{index}")


def _resolve_ref(root: Dict[str, Any], ref: str, pointer: str) -> Dict[str, Any]:
    if not ref.startswith("#/"):
        raise SchemaError(pointer, f"external $ref is not permitted: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise SchemaError(pointer, f"unresolvable $ref: {ref}")
        node = node[part]
    if not isinstance(node, dict):
        raise SchemaError(pointer, f"$ref {ref} does not resolve to a schema object")
    return node


def _check_type(value: Any, expected: Any, pointer: str) -> None:
    names = expected if isinstance(expected, list) else [expected]
    for name in names:
        python_type = _TYPE_MAP.get(name)
        if python_type is None:
            raise SchemaError(pointer, f"unknown type {name!r}")
        # bool is an int subclass in Python; JSON Schema keeps them distinct.
        if isinstance(value, bool) and name != "boolean":
            continue
        if isinstance(value, python_type):
            return
    label = "/".join(names) if isinstance(names, list) else str(names)
    raise SchemaError(pointer, f"expected {label}, got {type(value).__name__}")


def _check_format(value: Any, fmt: str, pointer: str) -> None:
    if not isinstance(value, str):
        return
    if fmt == "date-time":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SchemaError(pointer, f"invalid date-time: {value!r}") from exc


def validate(instance: Any, schema: Dict[str, Any], root: Dict[str, Any], pointer: str = "") -> None:
    """Validate ``instance`` against ``schema``. ``root`` supplies ``$defs`` for ``$ref``."""
    if "$ref" in schema:
        target = _resolve_ref(root, schema["$ref"], pointer)
        siblings = {k: v for k, v in schema.items() if k != "$ref"}
        validate(instance, {**target, **siblings}, root, pointer)
        return

    if "oneOf" in schema:
        branch_errors: List[str] = []
        for option in schema["oneOf"]:
            try:
                validate(instance, option, root, pointer)
                break
            except SchemaError as exc:
                branch_errors.append(exc.message)
        else:
            raise SchemaError(
                pointer,
                "does not match any oneOf branch: " + "; ".join(branch_errors),
            )
        # Sibling keywords outside oneOf still apply.
        rest = {k: v for k, v in schema.items() if k != "oneOf"}
        if any(k in rest for k in _SCALAR_KEYWORDS):
            validate(instance, rest, root, pointer)
        return

    if "not" in schema:
        try:
            validate(instance, schema["not"], root, pointer)
        except SchemaError:
            pass
        else:
            raise SchemaError(pointer, "matches a forbidden 'not' branch")

    if "const" in schema and instance != schema["const"]:
        raise SchemaError(pointer, f"expected const {schema['const']!r}, got {instance!r}")

    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(pointer, f"value {instance!r} not in enum {schema['enum']}")

    if "type" in schema:
        _check_type(instance, schema["type"], pointer)

    if "format" in schema:
        _check_format(instance, schema["format"], pointer)

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise SchemaError(pointer, f"shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise SchemaError(pointer, f"longer than maxLength {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            raise SchemaError(pointer, f"does not match pattern {schema['pattern']!r}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaError(pointer, f"below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            raise SchemaError(pointer, f"above maximum {schema['maximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise SchemaError(pointer, f"fewer than minItems {schema['minItems']}")
        if schema.get("uniqueItems"):
            seen = set()
            for item in instance:
                marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if marker in seen:
                    raise SchemaError(pointer, "array items are not unique")
                seen.add(marker)
        if "items" in schema:
            for index, item in enumerate(instance):
                validate(item, schema["items"], root, f"{pointer}/{index}")

    if isinstance(instance, dict):
        missing = [key for key in schema.get("required", []) if key not in instance]
        if missing:
            raise SchemaError(pointer or "/", f"missing required {missing}")

        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties")
        for key, value in instance.items():
            if key in properties:
                validate(value, properties[key], root, f"{pointer}/{key}")
            elif isinstance(additional, dict):
                validate(value, additional, root, f"{pointer}/{key}")
            elif additional is False:
                raise SchemaError(pointer, f"additional property {key!r} is not allowed")


def _validate(document: Any, schema_path: Path) -> None:
    schema = load_schema(str(schema_path))
    if "$ref" in schema:
        raise SchemaError("/", "top-level $ref is not supported")
    validate(document, schema, schema, "")


def validate_manifest(document: Any) -> None:
    """Validate an in-folder ``paper.workbench.json`` document."""
    _validate(document, MANIFEST_SCHEMA_PATH)


def validate_annotations(document: Any) -> None:
    """Validate a ``paper.annotations.json`` sidecar document.

    Runs the frozen schema first, then the version-dependent anchor rules that
    JSON Schema cannot express in the subset this validator enforces.
    """
    _validate(document, ANNOTATIONS_SCHEMA_PATH)
    for index, record in enumerate(document.get("annotations", [])):
        if not isinstance(record, dict):
            continue
        _validate_anchor_semantics(record, f"/annotations/{index}")


#: Anchor version at which a locator must be resolvable.
ANCHOR_SCHEMA_VERSION_RESOLVABLE = 2


def _validate_anchor_semantics(record: Dict[str, Any], pointer: str) -> None:
    """Enforce the rules that distinguish a usable anchor from a placeholder.

    Version 1 tolerated an empty PDF quad list and a null Markdown fingerprint,
    which meant an annotation could validate while being impossible to
    re-anchor. Version 2 requires a resolvable locator. Version 1 records stay
    readable so history is not invalidated; they are upgraded on next write.
    """
    version = record.get("anchor_schema_version", 1)
    if version < ANCHOR_SCHEMA_VERSION_RESOLVABLE:
        return

    anchor = record.get("anchor") or {}
    kind = anchor.get("type")

    if kind == "PDF_TEXT":
        quads = anchor.get("quad_points_normalized")
        if not isinstance(quads, list) or len(quads) < 1:
            raise SchemaError(
                f"{pointer}/anchor",
                "anchor_schema_version 2 requires at least one normalised quad "
                "point; an anchor with no geometry cannot be re-resolved",
            )
        quote = (anchor.get("text_quote") or {}).get("exact")
        if not quote:
            raise SchemaError(
                f"{pointer}/anchor/text_quote",
                "anchor_schema_version 2 requires a text quote as the "
                "second-chance locator",
            )
        return

    if kind == "MARKDOWN_TEXT":
        path = anchor.get("heading_path")
        fingerprint = anchor.get("block_fingerprint")
        position = anchor.get("text_position")
        quote = (anchor.get("text_quote") or {}).get("exact")
        # Headings alone move between revisions, and offsets alone shift with
        # any edit, so at least one positional locator plus the quote is needed.
        if not (fingerprint or position):
            raise SchemaError(
                f"{pointer}/anchor",
                "anchor_schema_version 2 requires block_fingerprint or "
                "text_position in addition to the heading path",
            )
        if not quote:
            raise SchemaError(
                f"{pointer}/anchor/text_quote",
                "anchor_schema_version 2 requires a text quote as the "
                "second-chance locator",
            )
        if not isinstance(path, list):
            raise SchemaError(f"{pointer}/anchor/heading_path", "must be an array")
        return

    raise SchemaError(f"{pointer}/anchor", f"unknown anchor type: {kind}")


def validate_ai_context(document: Any) -> None:
    """Validate an ``AIContextV1`` envelope. P0 defines this; it is never sent."""
    _validate(document, AI_CONTEXT_SCHEMA_PATH)

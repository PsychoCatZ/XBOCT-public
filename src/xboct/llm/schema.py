"""JSON Schema of the intent contract in the OpenAI strict function-calling subset.

Strict mode requires: every object has `additionalProperties: false` and lists ALL of its
properties in `required`; optional values are expressed as nullable; composition only
through `anyOf`; the root is an object. Pydantic still validates every answer, so
constraints the subset does not carry (string lengths etc.) are enforced afterwards.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

ALLOWED_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "anyOf",
        "enum",
        "$ref",
        "$defs",
        "description",
        "format",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
    }
)
DROP_KEYS = frozenset(
    {"title", "default", "discriminator", "minLength", "maxLength", "examples", "pattern"}
)
MAX_DEPTH = 10


def _nullable(node: dict[str, Any]) -> dict[str, Any]:
    if node.get("type") == "null":
        return node
    variants = node.get("anyOf")
    if variants is not None:
        if any(v.get("type") == "null" for v in variants):
            return node
        return {**node, "anyOf": [*variants, {"type": "null"}]}
    if "$ref" in node or "enum" in node:
        return {"anyOf": [node, {"type": "null"}]}
    return {"anyOf": [node, {"type": "null"}]}


def _normalize(node: Any) -> Any:
    if isinstance(node, list):
        return [_normalize(n) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return {"$ref": node["$ref"]}
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in DROP_KEYS:
            continue
        if key == "oneOf":
            out["anyOf"] = _normalize(value)
        elif key == "const":
            out["enum"] = [value]
        elif key == "properties":
            out["properties"] = {name: _normalize(sub) for name, sub in value.items()}
        elif key == "$defs":
            out["$defs"] = {name: _normalize(sub) for name, sub in value.items()}
        else:
            out[key] = _normalize(value)
    if out.get("type") == "object" and "properties" in out:
        originally_required = set(node.get("required", []))
        for name, sub in list(out["properties"].items()):
            if name not in originally_required and not _is_single_constant(sub):
                out["properties"][name] = _nullable(sub)
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


def _is_single_constant(node: dict[str, Any]) -> bool:
    return isinstance(node.get("enum"), list) and len(node["enum"]) == 1


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = copy.deepcopy(model.model_json_schema())
    return _normalize(schema)


def check_strict_subset(schema: dict[str, Any]) -> list[str]:
    """Problems that OpenAI strict mode would reject (empty list = OK)."""
    problems: list[str] = []
    if schema.get("type") != "object":
        problems.append("root must be an object")

    def walk(node: Any, path: str, depth: int) -> None:
        if isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]", depth)
            return
        if not isinstance(node, dict):
            return
        extra = set(node) - ALLOWED_KEYS
        if extra:
            problems.append(f"{path}: unsupported keys {sorted(extra)}")
        if depth > MAX_DEPTH:
            problems.append(f"{path}: nesting deeper than {MAX_DEPTH}")
        if node.get("type") == "object" or "properties" in node:
            props = node.get("properties", {})
            if node.get("additionalProperties") is not False:
                problems.append(f"{path}: additionalProperties must be false")
            if set(node.get("required", [])) != set(props):
                problems.append(f"{path}: every property must be required")
            for name, sub in props.items():
                walk(sub, f"{path}.{name}", depth + 1)
        for key in ("items", "anyOf"):
            if key in node:
                walk(node[key], f"{path}.{key}", depth + (key == "items"))
        for name, sub in node.get("$defs", {}).items():
            walk(sub, f"$defs.{name}", 1)

    walk(schema, "#", 0)
    return problems


def strip_nulls(value: Any) -> Any:
    """Strict mode sends every optional field as null; our models use defaults instead."""
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value

"""Sanitize MCP tool input schemas for strict LLM backends.

MCP servers forward their tools' ``inputSchema`` verbatim. Cloud routes
(OpenRouter, OpenAI, Anthropic) accept almost anything, but stricter backends
reject shapes that are common in Pydantic/MCP output:

- ``{"anyOf": [{"type": "string"}, {"type": "null"}]}`` — nullable unions for
  optional fields. Anthropic's input-schema validator rejects the null branch;
  collapse to the single non-null variant (optionality already lives in the
  parent's ``required`` array).
- ``"type": ["string", "null"]`` — array types. Many tool-call grammar
  generators only accept a single string ``type``.
- ``{"type": "object"}`` with no ``properties`` — llama.cpp's
  ``json-schema-to-grammar`` can't constrain a free-form object ("Unable to
  generate parser for this template"). Inject an empty ``properties`` dict.
- A bare string (``"object"``) where a schema dict is expected — malformed MCP
  output. Replace with the equivalent dict.
- Top-level ``anyOf``/``oneOf``/``allOf``/``enum``/``not`` — OpenAI's strict
  endpoints require the outermost parameters to be a plain ``type: object``.
- ``required`` entries naming properties that don't exist — drop them.

THE SCAR (hermes ``tools/schema_sanitizer.py``): sanitizers must NOT mutate the
shared tool registry. MCP ``inputSchema`` dicts are cached on the client and
reused every turn; we deep-copy before touching anything so a sanitized schema
never corrupts the cache or a sibling call. The work is conservative — it only
rewrites shapes the strict backend couldn't have used anyway.
"""
from __future__ import annotations

import copy
from typing import Any

from ..obs import get_logger

_log = get_logger("mcp")

_BARE_TYPES = {"object", "string", "number", "integer", "boolean", "array", "null"}
_TOP_LEVEL_FORBIDDEN = ("allOf", "anyOf", "oneOf", "enum", "not")
# Keys whose VALUES are not schemas (lists of names / literals), so the
# recursive walk must pass them through untouched instead of mistaking a
# literal string like "path" for a bare-string schema.
_NON_SCHEMA_KEYS = {"required", "enum", "examples", "const", "default"}


def sanitize_input_schema(schema: Any) -> dict[str, Any]:
    """Return a sanitized DEEP COPY of an MCP tool's ``inputSchema``.

    The input is never mutated — the caller may hand us a dict cached on the
    MCP client. The result is always a valid top-level object schema with a
    ``properties`` dict, safe to forward as a function's ``parameters``.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    node = _sanitize_node(copy.deepcopy(schema))
    if not isinstance(node, dict):
        return {"type": "object", "properties": {}}

    # Strip top-level combinators FIRST (drop the key, keep the object body):
    # a node carrying both ``anyOf`` and real ``properties`` must keep the
    # properties, not be collapsed to a bare union branch.
    for key in _TOP_LEVEL_FORBIDDEN:
        if key in node:
            _log.debug("schema_sanitizer: stripped top-level %r combinator", key)
            node.pop(key, None)
    # Collapse nullable unions nested inside properties (the top no longer has
    # one to collapse — it was just stripped above).
    node = _strip_nullable_unions(node)
    # Top-level must be a plain object with properties (strict-backend rule).
    if node.get("type") != "object":
        node["type"] = "object"
    if not isinstance(node.get("properties"), dict):
        node["properties"] = {}
    return node


def _sanitize_node(node: Any) -> Any:
    """Recursively normalize a JSON-Schema fragment (operates on a copy)."""
    if isinstance(node, str):
        # A bare string where a schema dict belongs (malformed MCP output).
        if node == "object":
            return {"type": "object", "properties": {}}
        if node in _BARE_TYPES:
            return {"type": node}
        return {"type": "object", "properties": {}}

    if isinstance(node, list):
        return [_sanitize_node(item) for item in node]

    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "type" and isinstance(value, list):
            non_null = [t for t in value if t != "null"]
            if len(non_null) == 1 and isinstance(non_null[0], str):
                out["type"] = non_null[0]
                if "null" in value:
                    out.setdefault("nullable", True)
            else:
                first = next((t for t in value if isinstance(t, str) and t != "null"), None)
                out["type"] = first or "object"
            continue
        if key in _NON_SCHEMA_KEYS:
            out[key] = value  # already deep-copied; never a schema
            continue
        if key in {"properties", "$defs", "definitions"} and isinstance(value, dict):
            out[key] = {k: _sanitize_node(v) for k, v in value.items()}
        elif key in {"items", "additionalProperties"}:
            out[key] = value if isinstance(value, bool) else _sanitize_node(value)
        else:
            out[key] = _sanitize_node(value) if isinstance(value, (dict, list)) else value

    # Object nodes need a properties dict for grammar-based backends.
    if out.get("type") == "object" and not isinstance(out.get("properties"), dict):
        out["properties"] = {}

    # Prune ``required`` entries that name absent properties.
    if out.get("type") == "object" and isinstance(out.get("required"), list):
        props = out.get("properties") or {}
        valid = [r for r in out["required"] if isinstance(r, str) and r in props]
        if not valid:
            out.pop("required", None)
        elif len(valid) != len(out["required"]):
            out["required"] = valid

    return out


def _strip_nullable_unions(schema: Any) -> Any:
    """Collapse ``anyOf``/``oneOf`` nullable unions to their non-null branch.

    ``{"anyOf": [{"type": "string"}, {"type": "null"}]}`` → ``{"type": "string",
    "nullable": true}``. Only collapses when exactly one non-null branch
    survives — a union with two real branches is meaningful and left intact.
    Outer metadata (title/description/default/examples) carries over.
    """
    if isinstance(schema, list):
        return [_strip_nullable_unions(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    stripped = {k: _strip_nullable_unions(v) for k, v in schema.items()}
    for key in ("anyOf", "oneOf"):
        variants = stripped.get(key)
        if not isinstance(variants, list):
            continue
        non_null = [
            v for v in variants
            if not (isinstance(v, dict) and v.get("type") == "null")
        ]
        if len(non_null) == 1 and len(non_null) != len(variants):
            replacement = dict(non_null[0]) if isinstance(non_null[0], dict) else {}
            replacement.setdefault("nullable", True)
            for meta in ("title", "description", "default", "examples"):
                if meta in stripped and meta not in replacement:
                    replacement[meta] = stripped[meta]
            return _strip_nullable_unions(replacement)
    return stripped

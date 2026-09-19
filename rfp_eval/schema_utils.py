"""Turn a Pydantic model into the strict JSON Schema both providers demand.

Anthropic's ``output_config.format`` and OpenAI's ``response_format`` strict
mode both require the same three things that Pydantic does *not* emit by
default:

1. no ``$ref``/``$defs`` indirection we can rely on identically across providers
2. ``additionalProperties: false`` on every object
3. every property listed in ``required`` (optionality is expressed by allowing
   an empty value, not by omitting the key)

Doing this once here rather than per-provider avoids two subtly different bugs.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

_MAX_DEPTH = 50


def _resolve(node: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    """Recursively inline every ``$ref`` against ``defs``."""
    if depth > _MAX_DEPTH:
        raise ValueError("schema nesting too deep -- is the model self-referential?")

    if isinstance(node, list):
        return [_resolve(item, defs, depth + 1) for item in node]

    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        ref = node["$ref"]
        name = ref.rsplit("/", 1)[-1]
        if name not in defs:
            raise ValueError(f"unresolvable schema reference: {ref}")
        target = _resolve(copy.deepcopy(defs[name]), defs, depth + 1)
        # Sibling keys alongside a $ref (e.g. a description) should survive.
        extras = {k: v for k, v in node.items() if k != "$ref"}
        target.update(_resolve(extras, defs, depth + 1))
        return target

    return {k: _resolve(v, defs, depth + 1) for k, v in node.items()}


def _strictify(node: Any) -> Any:
    """Apply the strict-mode object rules in place, depth-first."""
    if isinstance(node, list):
        return [_strictify(item) for item in node]

    if not isinstance(node, dict):
        return node

    node = {k: _strictify(v) for k, v in node.items()}

    # A default implies the key may be omitted, which strict mode forbids.
    node.pop("default", None)

    if node.get("type") == "object" and "properties" in node:
        node["additionalProperties"] = False
        node["required"] = list(node["properties"].keys())

    return node


def strict_schema(
    model: type[BaseModel], *, exclude: set[str] | None = None
) -> dict[str, Any]:
    """Return a provider-ready strict JSON Schema for ``model``.

    ``exclude`` drops top-level properties the model should not be asked to
    produce -- provenance fields like ``generated_at`` that the pipeline fills
    in itself. Strict mode makes every remaining property required, so leaving
    them in would mean spending output tokens on values we overwrite.
    """
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    if exclude:
        properties = raw.get("properties", {})
        for name in exclude:
            properties.pop(name, None)
        raw["required"] = [r for r in raw.get("required", []) if r not in exclude]
    resolved = _resolve(raw, defs)
    strict = _strictify(resolved)

    # A bare top-level schema still needs the object rules applied.
    if strict.get("type") == "object":
        strict.setdefault("additionalProperties", False)

    # Titles are noise in the prompt; descriptions are the useful part.
    return _drop_titles(strict)


def _drop_titles(node: Any) -> Any:
    if isinstance(node, list):
        return [_drop_titles(i) for i in node]
    if isinstance(node, dict):
        return {k: _drop_titles(v) for k, v in node.items() if k != "title"}
    return node

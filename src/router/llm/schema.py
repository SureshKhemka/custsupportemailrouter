"""Turn a Pydantic model into a strict JSON schema every provider accepts.

Providers enforce the *shape* (types, enums, required keys, no extra keys); numeric and length
constraints are stripped for the provider and enforced afterwards by Pydantic validation (LL-3).
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

_DROP = {"title", "default", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength",
         "maxLength", "minItems", "maxItems", "multipleOf", "pattern", "uniqueItems"}


def strict_schema(model: type[BaseModel], enums: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """`enums` injects allowed values for string fields by property name (e.g. the configured taxonomy)."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})
    return _clean(_inline(raw, defs), enums or {})


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline(copy.deepcopy(defs[node["$ref"].split("/")[-1]]), defs)
        return {k: _inline(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def _clean(node: Any, enums: dict[str, list[str]]) -> Any:
    if isinstance(node, list):
        return [_clean(v, enums) for v in node]
    if not isinstance(node, dict):
        return node
    out = {k: _clean(v, enums) for k, v in node.items() if k not in _DROP}
    if out.get("type") == "object" and "properties" in out:
        for name, prop in out["properties"].items():
            if name in enums and prop.get("type") == "string":
                prop["enum"] = list(enums[name])
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out

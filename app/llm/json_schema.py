from typing import Any

from pydantic import BaseModel


def build_strict_response_format(
    model_cls: type[BaseModel],
    name: str,
) -> dict[str, Any]:
    """Build a strict json_schema response_format from a Pydantic model.

    Providers that support strict structured output (e.g. OpenAI) require every
    object to disallow additional properties and to mark all properties as
    required, so we post-process Pydantic's schema to satisfy those rules.
    """
    schema = model_cls.model_json_schema()
    _enforce_strict(schema)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


def json_object_response_format() -> dict[str, Any]:
    """Lenient JSON mode for providers without strict json_schema support."""
    return {"type": "json_object"}


def _enforce_strict(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        for value in node.values():
            _enforce_strict(value)
    elif isinstance(node, list):
        for item in node:
            _enforce_strict(item)

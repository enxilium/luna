"""Model-facing schema expansion and canonical MCP argument validation."""

import copy
import json
import re

from jsonschema import Draft202012Validator
from mcp.types import Tool


def operation_names(tool: Tool) -> set[str]:
    description = tool.description or ""
    if (
        "requests" not in tool.input_schema.get("properties", {})
        or "list of possible updates" not in description
    ):
        return set()
    return set(re.findall(r"^- `([A-Za-z][A-Za-z0-9]+)`:", description, re.MULTILINE))


def invalid_arguments(tool: Tool, arguments: dict) -> dict | None:
    error = next(Draft202012Validator(tool.input_schema).iter_errors(arguments), None)
    if error:
        return {
            "reason": "invalid_arguments",
            "path": list(error.absolute_path),
            "constraint": error.validator,
        }
    sections = operation_names(tool)
    if sections and "requests" in tool.input_schema.get("properties", {}):
        requests = arguments.get("requests")
        if not isinstance(requests, list) or not requests:
            return {
                "reason": "invalid_arguments",
                "path": ["requests"],
                "constraint": "nonempty_batch",
            }
        for i, request in enumerate(requests):
            if (
                not isinstance(request, dict)
                or len(request) != 1
                or next(iter(request)) not in sections
                or not isinstance(next(iter(request.values())), dict)
            ):
                return {
                    "reason": "invalid_arguments",
                    "path": ["requests", i],
                    "constraint": "operation_wrapper",
                }
            if "replaceAllText" in request:
                body = request["replaceAllText"]
                criteria = body.get("containsText", {})
                if (
                    not isinstance(criteria, dict)
                    or not isinstance(criteria.get("text"), str)
                    or not isinstance(body.get("replaceText"), str)
                ):
                    return {
                        "reason": "invalid_arguments",
                        "path": ["requests", i, "replaceAllText"],
                        "constraint": "text_replacement",
                    }
    return None


def model_schema(schema: dict) -> dict:
    """Expand local references for models; execution validates the canonical schema."""

    def expand(value, seen=()):
        if isinstance(value, list):
            return [expand(v, seen) for v in value]
        if not isinstance(value, dict):
            return value
        ref = value.get("$ref", "")
        if isinstance(ref, str) and ref.startswith("#/") and ref not in seen:
            target = schema
            try:
                for key in ref[2:].split("/"):
                    target = target[key.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError):
                return copy.deepcopy(value)
            resolved = expand(target, (*seen, ref))
            siblings = expand({k: v for k, v in value.items() if k != "$ref"}, seen)
            if isinstance(resolved, dict) and set(siblings) <= {
                "description",
                "title",
                "default",
                "examples",
            }:
                return resolved | siblings
            return {"allOf": [resolved, siblings]}
        return {k: expand(v, seen) for k, v in value.items()}

    result = expand(copy.deepcopy(schema))
    if '"$ref"' not in json.dumps({k: v for k, v in result.items() if k != "$defs"}):
        result.pop("$defs", None)
    return result

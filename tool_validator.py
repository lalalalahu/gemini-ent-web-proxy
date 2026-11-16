import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple


TOOL_VALIDATION_MAX_RETRIES: int = int(os.getenv("TOOL_VALIDATION_MAX_RETRIES", "1"))

UNPARSED_TOOL_CALL_RE = re.compile(r"(?m)^[ \t]*<tool_call\b", re.IGNORECASE)


def _coerce_value(val: Any, prop_schema: Dict[str, Any]) -> Tuple[Any, Optional[str]]:
    """Deterministically coerce unambiguously typed string values according to JSON Schema."""
    raw_type = prop_schema.get("type")
    if isinstance(raw_type, list):
        allowed_types = [t for t in raw_type if t != "null"]
        if val is None and "null" in raw_type:
            return None, None
    elif isinstance(raw_type, str):
        allowed_types = [raw_type]
    else:
        return val, None

    if not allowed_types:
        return val, None

    # Safe deterministic coercions when model emits stringified primitives
    if isinstance(val, str):
        stripped = val.strip()
        if "boolean" in allowed_types and "string" not in allowed_types:
            low = stripped.lower()
            if low in ("true", "1"):
                return True, None
            if low in ("false", "0"):
                return False, None
        if "integer" in allowed_types and "string" not in allowed_types:
            if re.fullmatch(r"-?\d+", stripped):
                return int(stripped), None
        if "number" in allowed_types and "string" not in allowed_types:
            try:
                num = float(stripped)
                if math.isfinite(num):
                    return num, None
            except ValueError:
                pass
        if "object" in allowed_types and "string" not in allowed_types and stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed, None
            except Exception:
                pass
        if "array" in allowed_types and "string" not in allowed_types and stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return parsed, None
            except Exception:
                pass

    # Type verification
    def _matches(v: Any, t: str) -> bool:
        if t == "string":
            return isinstance(v, str)
        if t == "integer":
            return isinstance(v, int) and not isinstance(v, bool)
        if t == "number":
            return isinstance(v, (int, float)) and not isinstance(v, bool)
        if t == "boolean":
            return isinstance(v, bool)
        if t == "array":
            return isinstance(v, list)
        if t == "object":
            return isinstance(v, dict)
        if t == "null":
            return v is None
        return True

    if not any(_matches(val, t) for t in allowed_types):
        return val, f"expected type {'/'.join(allowed_types)}, got {type(val).__name__}"

    enum_vals = prop_schema.get("enum")
    if isinstance(enum_vals, list) and val not in enum_vals:
        return val, f"value {val!r} not in allowed enum {enum_vals}"

    return val, None


def validate_and_normalize_tool_calls(
    tool_calls: Optional[List[Dict[str, Any]]],
    allowed_tools: Optional[List[Any]],
    raw_response: str = "",
) -> Tuple[Optional[List[Dict[str, Any]]], List[str]]:
    """
    Normalize unambiguously coerced arguments and validate against OpenAI tool JSON Schemas.
    Returns (normalized_tool_calls, validation_errors).
    """
    if not allowed_tools:
        return tool_calls, []

    schema_map: Dict[str, Dict[str, Any]] = {}
    for t in allowed_tools:
        fn = getattr(t, "function", None)
        if fn is not None:
            schema_map[fn.name] = fn.parameters or {}

    if not tool_calls:
        if raw_response and UNPARSED_TOOL_CALL_RE.search(raw_response):
            return None, [
                "A <tool_call> block was emitted, but it could not be parsed as valid JSON with 'name' and 'arguments'."
            ]
        return None, []

    errors: List[str] = []
    normalized: List[Dict[str, Any]] = []

    for tc in tool_calls:
        fn_obj = tc.get("function") or {}
        name = fn_obj.get("name", "")
        args_raw = fn_obj.get("arguments", "{}")

        if schema_map and name not in schema_map:
            errors.append(f"Unknown tool '{name}'. Allowed tools: {', '.join(sorted(schema_map.keys()))}.")
            normalized.append(tc)
            continue

        if isinstance(args_raw, dict):
            args = dict(args_raw)
        elif isinstance(args_raw, str):
            try:
                parsed = json.loads(args_raw)
                if not isinstance(parsed, dict):
                    errors.append(f"Tool '{name}' arguments must be a JSON object.")
                    normalized.append(tc)
                    continue
                args = parsed
            except Exception as exc:
                errors.append(f"Tool '{name}' arguments contain invalid JSON: {exc}.")
                normalized.append(tc)
                continue
        else:
            errors.append(f"Tool '{name}' arguments must be a JSON object.")
            normalized.append(tc)
            continue

        schema = schema_map.get(name) or {}
        props = schema.get("properties") or {}
        required = schema.get("required") or []

        if "raw" in args and "raw" not in props and len(args) == 1:
            errors.append(f"Tool '{name}' arguments could not be parsed into structured JSON fields.")
            normalized.append(tc)
            continue

        for req_key in required:
            if req_key not in args or args[req_key] is None:
                errors.append(f"Tool '{name}' is missing required argument '{req_key}'.")

        for key, val in list(args.items()):
            if key in props and isinstance(props[key], dict):
                coerced_val, err = _coerce_value(val, props[key])
                if err:
                    errors.append(f"Tool '{name}' argument '{key}': {err}.")
                else:
                    args[key] = coerced_val

        updated_tc = dict(tc)
        updated_fn = dict(fn_obj)
        updated_fn["arguments"] = json.dumps(args, ensure_ascii=False)
        updated_tc["function"] = updated_fn
        normalized.append(updated_tc)

    return normalized, errors


def build_tool_repair_prompt(errors: List[str]) -> str:
    """Build a concise correction prompt so the primary Gemini model fixes invalid tool arguments."""
    bullet_lines = "\n".join(f"- {e}" for e in errors[:6])
    return (
        "Your previous tool call failed schema validation:\n"
        f"{bullet_lines}\n\n"
        "Please re-emit ONLY the corrected tool call(s) using the exact <tool_call> XML format "
        "with valid JSON and all required arguments."
    )

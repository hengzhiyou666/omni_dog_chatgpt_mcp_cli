"""Finite JSON and identifier validation shared by both entry points."""
import json
import math
from collections.abc import Mapping

from .errors import OmniError


def sanitize(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    return value


def json_text(value, *, pretty: bool = False) -> str:
    return json.dumps(sanitize(value), ensure_ascii=False, allow_nan=False,
                      indent=2 if pretty else None)


def identifier(value: str, label: str = "identifier") -> str:
    if (not isinstance(value, str) or not value or len(value) > 256
            or any(ord(c) < 32 for c in value) or value in {".", ".."}
            or "/" in value or "\\" in value):
        raise OmniError("invalid_argument", f"Invalid {label}")
    return value


def task_identifier(value: str) -> str:
    # Native operation IDs contain slashes. This value is never a filesystem path.
    if (not isinstance(value, str) or not value or len(value) > 256
            or any(ord(c) < 32 for c in value)):
        raise OmniError("invalid_argument", "Invalid task_id")
    return value

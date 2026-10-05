import hashlib
import json
import math
from pathlib import Path
import uuid


def json_safe(value):
    # IDs are normally already ResourceId strings. Preserve every larger integer
    # exactly for JavaScript clients, including uint64 values in shader buffers.
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) > 9007199254740991:
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")}
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


class Artifacts:
    def __init__(self, directory: Path, inline_bytes: int):
        self.directory = directory
        self.inline_bytes = inline_bytes

    def bound_result(self, data):
        safe = json_safe(data)
        raw = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(raw) <= self.inline_bytes:
            return safe, {"inline_bytes": len(raw), "truncated": False}
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / ("result-" + uuid.uuid4().hex + ".json")
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(raw)
        temporary.replace(destination)
        return {
            "artifact": str(destination),
            "format": "json",
            "byte_count": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "complete": True,
            "preview": raw[:2048].decode("utf-8", errors="ignore"),
        }, {"truncated": True, "full_result_in_artifact": True, "inline_limit_bytes": self.inline_bytes}

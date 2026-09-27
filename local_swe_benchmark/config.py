from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

def load_yaml(path: Path) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        # JSON is a strict subset of YAML 1.2. This fallback keeps the bundled
        # examples usable without dependencies; general YAML needs PyYAML.
        try:
            value = json.loads(source)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Install the project dependencies (PyYAML) to read YAML task files") from exc
    else:
        value = yaml.safe_load(source)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return value


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

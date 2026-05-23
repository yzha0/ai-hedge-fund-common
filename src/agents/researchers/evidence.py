from __future__ import annotations

import math
import numbers
from typing import Any


RESEARCH_EVIDENCE_SCHEMA_VERSION = "research_evidence_v1"


def sanitize_for_json(value: Any) -> Any:
    """Convert common model/dataframe scalar values into JSON-safe primitives."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): sanitize_for_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_for_json(item) for item in value]

    if hasattr(value, "model_dump"):
        return sanitize_for_json(value.model_dump())
    if hasattr(value, "dict"):
        return sanitize_for_json(value.dict())

    return value


def build_raw_evidence(
    *,
    factor: str,
    signal: str,
    confidence: float,
    metrics: dict | None = None,
    components: dict | None = None,
    weights: dict | None = None,
    metadata: dict | None = None,
) -> dict:
    """Build a consistent raw-evidence packet for the research synthesizer."""
    return {
        "schema_version": RESEARCH_EVIDENCE_SCHEMA_VERSION,
        "factor": factor,
        "signal": signal,
        "confidence": confidence,
        "metrics": sanitize_for_json(metrics or {}),
        "components": sanitize_for_json(components or {}),
        "weights": sanitize_for_json(weights or {}),
        "metadata": sanitize_for_json(metadata or {}),
    }

"""Recursive JSON sanitizer: NaN/Inf/numpy/pandas/datetime -> strict-JSON-safe primitives."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import PurePath
from typing import Any

import numpy as np
import pandas as pd
from fastapi.responses import JSONResponse


def sanitize(obj: Any) -> Any:
    """Return a deep copy of ``obj`` containing only ``None/bool/int/float/str/list/dict``.

    * ``NaN`` / ``+-Inf`` (python, numpy, Decimal) -> ``None``
    * numpy / pandas scalars -> python scalars; ``NaT`` -> ``None``
    * ``Timestamp`` / ``datetime`` / ``date`` / ``time`` -> ISO strings; ``timedelta`` -> seconds
    * ``Series`` / ``DataFrame`` / ``ndarray`` / sets / tuples -> lists or dicts
    * dict keys are coerced to ``str``; unknown objects fall back to ``str(obj)``
    """
    if obj is None or isinstance(obj, (bool, np.bool_)):
        return None if obj is None else bool(obj)
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, Decimal):
        f = float(obj)
        return f if math.isfinite(f) else None
    if obj is pd.NaT:
        return None
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (date, time)):
        return obj.isoformat()
    if isinstance(obj, (timedelta, pd.Timedelta)):
        return obj.total_seconds()
    if isinstance(obj, dict):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [sanitize(v) for v in obj.tolist()]
    if isinstance(obj, pd.Series):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, pd.DataFrame):
        return [sanitize(r) for r in obj.to_dict(orient="records")]
    if is_dataclass(obj) and not isinstance(obj, type):
        return sanitize(asdict(obj))
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


class SafeJSONResponse(JSONResponse):
    """JSONResponse that sanitizes content first and never emits NaN/Infinity tokens."""

    def render(self, content: Any) -> bytes:
        return json.dumps(
            sanitize(content),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")

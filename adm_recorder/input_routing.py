from __future__ import annotations

import json

import numpy as np
from PySide6.QtCore import QSettings


def default_identity_route(n_in: int, n_out: int) -> np.ndarray:
    r = np.zeros((max(1, n_in), max(1, n_out)), dtype=np.float32)
    for i in range(min(n_in, n_out)):
        r[i, i] = 1.0
    return r


def resize_route(
    route: np.ndarray | None, n_in: int, n_out: int
) -> np.ndarray:
    """Keep overlapping coefficients; pad/truncate to new input × output size."""
    n_in = max(1, n_in)
    n_out = max(1, n_out)
    new = np.zeros((n_in, n_out), dtype=np.float32)
    if route is None:
        return default_identity_route(n_in, n_out)
    a = np.asarray(route, dtype=np.float32)
    h = min(a.shape[0], n_in)
    w = min(a.shape[1], n_out)
    if h > 0 and w > 0:
        new[:h, :w] = a[:h, :w]
    return new


def route_to_json(route: np.ndarray) -> str:
    a = np.asarray(route, dtype=np.float32)
    payload = {
        "n_in": int(a.shape[0]),
        "n_out": int(a.shape[1]),
        "data": a.tolist(),
    }
    return json.dumps(payload, separators=(",", ":"))


def route_from_json(s: str | None) -> np.ndarray | None:
    if not s or not isinstance(s, str):
        return None
    try:
        o = json.loads(s)
        data = o.get("data")
        if not isinstance(data, list):
            return None
        return np.asarray(data, dtype=np.float32)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def load_route_from_settings(settings: QSettings) -> np.ndarray | None:
    raw = settings.value("recorder/input_route_json")
    if isinstance(raw, str):
        return route_from_json(raw)
    if raw is not None:
        return route_from_json(str(raw))
    return None


def save_route_to_settings(settings: QSettings, route: np.ndarray) -> None:
    settings.setValue("recorder/input_route_json", route_to_json(route))

"""Tiny TOML writer for azan-sip's config (tables of scalars/lists + an array of tables)."""
from __future__ import annotations

import math

HEADER = "# azan-sip settings - written by the control panel. You can edit it in the app instead.\n"


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise ValueError("invalid number")
        return repr(v)
    if isinstance(v, str):
        esc = (v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
               .replace("\r", "\\r").replace("\t", "\\t"))
        return f'"{esc}"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v if x is not None) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() else _value(k)


def _table(d: dict) -> str:
    lines = []
    for k, v in d.items():
        if v is None or isinstance(v, dict):
            continue
        lines.append(f"{_key(k)} = {_value(v)}")
    return "\n".join(lines) + "\n"


def dumps(cfg: dict) -> str:
    out = [HEADER]
    for name, val in cfg.items():
        if isinstance(val, dict):
            out.append(f"\n[{_key(name)}]\n" + _table(val))
    for name, val in cfg.items():
        if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
            for item in val:
                out.append(f"\n[[{_key(name)}]]\n" + _table(item))
    return "".join(out)

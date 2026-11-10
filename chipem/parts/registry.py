"""Part type registry: `@part("type")` registers a Part subclass, `create(type, id, params)` builds one,
`catalog()` describes every type for UIs (`[{type, params: {name: default}, ports: {name: dir}, common_params}]`;
`common_params` are accepted by every type: `quiet`, see chipem.parts.base).

Generic glue (no vendor knowledge): parts live on a `chipem.board.Board` and talk to it only through nets.
"""
from __future__ import annotations

from typing import Any, Callable

__all__ = ["part", "create", "catalog", "types", "get", "is_type"]

_TYPES: dict[str, type] = {}


def part(type_name: str) -> Callable[[type], type]:
    """Class decorator: register a Part subclass under `type_name` (lower case)."""
    def deco(cls: type) -> type:
        key = type_name.lower()
        if key in _TYPES and _TYPES[key] is not cls:
            raise ValueError(f"part type {key!r} registered twice")
        cls.type = key
        _TYPES[key] = cls
        return cls
    return deco


def _load() -> None:
    from . import encoder, i2c_eeprom, i2c_register_device, i2s_codec, std  # noqa: F401  (standard types)
    from . import camera  # noqa: F401  (parallel camera, DVP hook)
    from . import serial  # noqa: F401  (serial cable chain: adapter, transceiver, connectors, cable)


def get(type_name: str) -> type:
    _load()
    cls = _TYPES.get(str(type_name).strip().lower())
    if cls is None:
        raise ValueError(f"unknown part type {type_name!r} (known: {', '.join(types())})")
    return cls


def is_type(type_name: Any) -> bool:
    _load()
    return str(type_name).strip().lower() in _TYPES


def types() -> list[str]:
    _load()
    return list(_TYPES)


def create(type_name: str, part_id: str, params: dict[str, Any] | None = None):
    """A new, unattached Part of `type_name` (params validated; unknown params are an error)."""
    return get(type_name)(part_id, params or {})


def catalog() -> list[dict]:
    """Every part type with its default params and default ports (for the add-part dialog)."""
    _load()
    out = []
    for name, cls in _TYPES.items():
        out.append({"type": name, "params": dict(cls.default_params()), "common_params": {"quiet": False},
                    "ports": {p: d.dir for p, d in cls.port_descs(cls.default_params()).items()},
                    "doc": " ".join((cls.__doc__ or "").split())})
    return out

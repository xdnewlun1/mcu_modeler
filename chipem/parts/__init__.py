"""Board parts: switches, buttons, clocks, LEDs, probes, counters and logic gates attached to board nets.

Generic glue (no vendor knowledge). `registry.create(type, id, params)` / `registry.catalog()`;
`chipem.board.Board.add_part()` wires a part's ports to nets. See docs/CONTRACTS.md "Board parts".
"""
from .base import Part, PortDesc
from .registry import catalog, create, part, types

__all__ = ["Part", "PortDesc", "catalog", "create", "part", "types"]

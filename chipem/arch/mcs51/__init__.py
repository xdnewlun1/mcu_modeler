"""MCS-51 (8051) interrupt architecture: IE/IP/IPH priority levels, polling order, vectors, RETI, reset, and the
core's own SFRs as bus blocks.  Mirrors the role of `chipem.arch.armv7m` / `chipem.arch.c28x`."""
from .arch import MCS51Arch, MCS51Fault, SFR_BASE

__all__ = ["MCS51Arch", "MCS51Fault", "SFR_BASE"]

"""TI C28x interrupt architecture: PIE controller (MMIO at 0x0CE0, vector table at 0x0D00) and CPU interrupt
model (IFR/IER/INTM, vector fetch, reset, ITRAP).  Mirrors the role of `chipem.arch.armv7m`."""
from .arch import CPU_VECTOR_NAMES, C28xArch, C28xFault, pie_vector, vector_name
from .pie import PIEBlock

__all__ = ["C28xArch", "C28xFault", "PIEBlock", "pie_vector", "vector_name", "CPU_VECTOR_NAMES"]

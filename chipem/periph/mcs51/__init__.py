"""MCS-51 (8051) family peripheral models for the NXP P89CV51RB2/RC2/RD2 (ports, timers 0/1/2, enhanced UART, SPI,
PCA, watchdog, flash/IAP, system control, external data bus).  Importing the package registers every model
(`mcs51.<model>`); new modules are picked up automatically.  See docs/MCS51_CORE.md."""
import importlib
import pkgutil

for _m in pkgutil.iter_modules(__path__):
    if not _m.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_m.name}")

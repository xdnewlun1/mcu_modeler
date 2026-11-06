"""NXP/Freescale Kinetis peripheral models (K22F family).

Importing the package imports every submodule (found with `pkgutil.iter_modules`), so each module's
`@register("kinetis.xxx")` decorators run and models written by different people register themselves without
editing this file.  A submodule that fails to import is recorded in `IMPORT_ERRORS` (and reported with a
`warnings.warn`) instead of breaking the whole family; the registry then falls back to `stub` for its models.
"""
from __future__ import annotations

import importlib
import pkgutil
import warnings

IMPORT_ERRORS: dict[str, BaseException] = {}


def _import_all() -> None:
    for info in pkgutil.iter_modules(__path__):
        name = info.name
        if name.startswith("__"):
            continue
        try:
            importlib.import_module(f"{__name__}.{name}")
        except Exception as e:  # noqa: BLE001 - one broken model must not hide the others
            IMPORT_ERRORS[name] = e
            warnings.warn(f"chipem.periph.kinetis.{name} failed to import: {e!r}", RuntimeWarning, stacklevel=2)


_import_all()

"""TI C2000 (TMS320F2833x/F2823x) peripheral models.  Importing the package registers every model
(`ti_c2000.<model>`); new modules are picked up automatically."""
import importlib
import pkgutil

for _m in pkgutil.iter_modules(__path__):
    if not _m.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_m.name}")

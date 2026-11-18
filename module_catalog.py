"""Registry of modules that are implemented and may be licensed.

Do not add planned verticals here until their complete server-side workflows,
migrations, authorization and accounting integration tests are shipped.
"""
from __future__ import annotations

from types import MappingProxyType

MODULE_DEFINITIONS = MappingProxyType({
    "accounting": MappingProxyType({"title": "حسابداری", "status": "available", "dependencies": ()}),
    "cafe": MappingProxyType({"title": "کافه و فروش", "status": "available", "dependencies": ()}),
    "customers": MappingProxyType({"title": "مشتریان", "status": "available", "dependencies": ()}),
    "gamenet": MappingProxyType({"title": "گیم‌نت", "status": "available", "dependencies": ()}),
    "inventory": MappingProxyType({"title": "انبار و کالا", "status": "available", "dependencies": ()}),
    "reports": MappingProxyType({"title": "گزارش‌ها", "status": "available", "dependencies": ("accounting",)}),
    "settings": MappingProxyType({"title": "تنظیمات", "status": "available", "dependencies": ()}),
})

# Keep the current signed-license allow-list stable until new modules are complete.
MODULE_KEYS = tuple(MODULE_DEFINITIONS)


def resolve_module_dependencies(module_keys: list[str] | tuple[str, ...]) -> list[str]:
    """Return sorted unique entitlements including all declared dependencies."""
    resolved: set[str] = set()
    pending = list(module_keys)
    while pending:
        key = pending.pop()
        definition = MODULE_DEFINITIONS.get(key)
        if definition is None:
            raise ValueError("ماژول ناشناخته است.")
        if key in resolved:
            continue
        resolved.add(key)
        pending.extend(definition["dependencies"])
    return sorted(resolved)

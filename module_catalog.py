"""Central registry for modules that are implemented and may be licensed.

Planned industry modules are deliberately not listed here: adding a key here
makes it part of the signed-license allow-list and must only happen together
with a complete server-side implementation and acceptance coverage.
"""
from __future__ import annotations

from types import MappingProxyType

MODULE_DEFINITIONS = MappingProxyType({
    "accounting": MappingProxyType({
        "title": "حسابداری",
        "status": "available",
        "dependencies": (),
    }),
    "cafe": MappingProxyType({
        "title": "کافه و فروش",
        "status": "available",
        "dependencies": (),
    }),
    "customers": MappingProxyType({
        "title": "مشتریان",
        "status": "available",
        "dependencies": (),
    }),
    "gamenet": MappingProxyType({
        "title": "گیم‌نت",
        "status": "available",
        "dependencies": (),
    }),
    "inventory": MappingProxyType({
        "title": "انبار و کالا",
        "status": "available",
        "dependencies": (),
    }),
    "reports": MappingProxyType({
        "title": "گزارش‌ها",
        "status": "available",
        "dependencies": ("accounting",),
    }),
    "settings": MappingProxyType({
        "title": "تنظیمات",
        "status": "available",
        "dependencies": (),
    }),
})

# Preserve the existing signed-license contract and its accepted module keys.
MODULE_KEYS = tuple(MODULE_DEFINITIONS)


def resolve_module_dependencies(module_keys: list[str] | tuple[str, ...]) -> list[str]:
    """Return a sorted, de-duplicated entitlement set including dependencies."""
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

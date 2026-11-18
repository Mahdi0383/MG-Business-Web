"""Registry of implemented modules and their license dependencies.

Accounting is the mandatory financial core. Any operational module that can
create or affect business/financial records must include its entitlement.
Planned verticals are intentionally excluded until fully implemented.
"""
from __future__ import annotations

from types import MappingProxyType

MODULE_DEFINITIONS = MappingProxyType({
    "accounting": MappingProxyType({"title": "حسابداری", "status": "available", "dependencies": ()}),
    "cafe": MappingProxyType({"title": "کافه و فروش", "status": "available", "dependencies": ("accounting",)}),
    "customers": MappingProxyType({"title": "مشتریان", "status": "available", "dependencies": ("accounting",)}),
    "gamenet": MappingProxyType({"title": "گیم‌نت", "status": "available", "dependencies": ("accounting",)}),
    "inventory": MappingProxyType({"title": "انبار و کالا", "status": "available", "dependencies": ("accounting",)}),
    "reports": MappingProxyType({"title": "گزارش‌ها", "status": "available", "dependencies": ("accounting",)}),
    "settings": MappingProxyType({"title": "تنظیمات", "status": "available", "dependencies": ()}),
})

# Preserve the current signed-license key set. Dependencies are expanded at
# issuance and verification time, so existing valid codes remain verifiable.
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

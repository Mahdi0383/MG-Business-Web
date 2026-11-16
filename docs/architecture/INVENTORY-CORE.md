# Inventory domain foundation

This is the first inventory data-model slice, not a finished stock-management module.

## Implemented
- Tenant-owned products with normalized SKU, name, base unit, and an inventory-tracking flag.
- Immutable stock movement records with tenant, branch, product, movement type, quantity, unit cost, reference, and timestamp.
- Positive quantity validation with up to three decimal places and non-negative unit cost with two decimal places.
- Tenant-scoped unique SKU and composite foreign keys that prevent a movement from referencing another tenant's product or branch.
- SQLite persistence tests for valid movements and cross-tenant product rejection.

## Not implemented yet
- Purchase/sales document workflows, stock reservations, serial/lot/batch tracking, expiry dates, warehouses and bin locations, transfers, reorder rules, costing layers (FIFO/weighted average), stock valuation, and negative-stock policy.
- A stock balance projection or ledger service. On-hand quantity must be derived from auditable movements (or a rebuildable projection), never edited directly.
- Atomic posting that links a sale/purchase, its stock movements, payment, and general-ledger entries.

## Next gates
1. Add movement direction and valuation policies per movement type, including atomic paired transfers.
2. Add integration tests for tenant and branch isolation on every persistence path.
3. Implement transaction-backed application services and idempotency before exposing write APIs.
4. Generate and test EF Core migrations for SQLite and PostgreSQL.

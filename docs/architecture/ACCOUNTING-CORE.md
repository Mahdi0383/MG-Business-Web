# Accounting domain foundation

This document records the first accounting domain slice. It is deliberately not described as a complete accounting subsystem or as production-ready posting.

## Implemented in this slice

- Tenant-owned chart-of-account entries with a typed account category: asset, liability, equity, revenue, or expense.
- Journal entry aggregate with a required tenant and branch.
- Journal lines carry the tenant identifier, so the aggregate rejects cross-tenant lines before persistence.
- Each line must contain exactly one positive side (debit or credit); negative values and precision beyond two decimal places are rejected.
- Journal entry totals must be equal and greater than zero.
- EF Core mappings add tenant-scoped unique account codes and composite foreign keys from journal lines to both their parent entry and account. Journal entries reference a branch using both tenant and branch identifiers.

## Deliberate limitations

- No API endpoint creates or posts journal entries yet.
- No authentication, authorization, server-side tenant resolver, or tenant membership model is implemented yet.
- No database migrations have been generated or applied.
- Fiscal periods, tax calculation, multi-currency, exchange rates, rounding policy, account hierarchy, opening balances, reversals, posting locks, audit trail, and financial statements are not implemented.
- Two-decimal precision is an initial baseline only; it must become currency-aware before supporting currencies with different minor-unit precision.
- The current domain aggregate validates balance in memory. Production posting must also validate referenced accounts and branch membership within the same tenant inside a transaction.

## Required next gates

1. Add relational persistence tests for composite tenant foreign keys and uniqueness constraints on SQLite and PostgreSQL.
2. Introduce controlled EF Core migrations and migration verification in CI.
3. Implement authentication and authorization with server-side tenant membership resolution before exposing tenant data.
4. Add a posting application service with transaction boundaries, immutable posted entries, reversals, idempotency, and audit events.
5. Add API contract and integration tests before connecting the frontend.

## Accounting design rule

Operational documents (sales, purchases, café orders, game sessions, payments, inventory movements) must eventually post through one accounting application service. They must not write directly to the general ledger or independently calculate profit and loss.

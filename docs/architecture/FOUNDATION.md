# MG Business Web — Architecture Foundation

## Product boundaries
MG Business is a modular business platform, not a collection of disconnected screens. Shared business records must be the source of truth for POS, inventory, customer accounts, café operations, game-net sessions, and accounting.

## Initial architecture
- Web client: React + Vite (existing prototype; TypeScript migration is planned)
- API: ASP.NET Core 10
- Domain: framework-independent business entities and rules
- Persistence: EF Core 10; SQLite for local development and PostgreSQL for hosted deployments
- Hosting: GitHub Pages is only a static frontend preview. It is not an API host or database host.

## Tenant and branch isolation
Every business-owned record must be tenant-scoped. Branch-owned records must also carry a branch identifier where applicable. Tenant identity must be derived from a verified authenticated principal and server-side membership lookup. Never trust a tenant ID supplied only by a request header or body.

## Accounting invariants
- Posted journal entries are immutable; corrections use reversing entries.
- Every posted journal entry must balance (total debits equal total credits).
- Operational documents must not silently mutate posted accounting history.
- Currency precision, tax rules, fiscal periods, and rounding policy must be explicit and tested before posting.

## Inventory and POS invariants
- Stock changes are recorded as auditable movements, not direct quantity overwrites.
- A completed sale, its payment allocation, inventory movements, and accounting posting must be committed atomically or through a documented outbox workflow.
- Idempotency keys are required for retryable write operations to prevent duplicate orders and payments.

## Offline-first direction
Offline writes require durable local storage, stable operation IDs, server acknowledgements, conflict policy, and replay-safe APIs. Browser localStorage is prototype-only and must not be treated as a production data store.

## Delivery gates
No feature is production-ready until it has:
1. Domain validation and automated tests.
2. Authorization and tenant-isolation tests.
3. Database migration and rollback considerations.
4. API contract and error behavior.
5. Observability and audit events where relevant.
6. Accessibility, RTL/LTR, responsive, and browser coverage for UI changes.

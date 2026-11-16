# MG Business API

ASP.NET Core 10 backend foundation for the independent MG Business Web platform.

## Requirements
- .NET 10 SDK (repository pinned in `global.json`)
- SQLite for local development and automated tests
- PostgreSQL for hosted/production deployments

## Run locally
From the repository root:

```bash
dotnet run --project backend/src/MG.Business.Api/MG.Business.Api.csproj
```

The default development configuration uses SQLite. Configure hosted environments only through environment variables or a secret store:

- `Database__Provider=PostgreSql`
- `ConnectionStrings__Business`
- `DataProtection__KeysDirectory` — persistent, private storage for bearer-token protection keys
- `Cors__AllowedOrigins__0`, `Cors__AllowedOrigins__1`, ... — exact trusted frontend origins
- `Database__ApplyMigrations=true` — opt-in startup migration execution for PostgreSQL after reviewing the committed migration set

Never commit production credentials, signing keys, or Data Protection key material.

## Implemented HTTP surface

### Authentication
- `POST /auth/register`
- `POST /auth/login`
- Identity API bearer-token endpoints are used; these are opaque ASP.NET Core Identity tokens, not JWTs.
- Passwords require 12 characters, upper/lowercase, a digit and a non-alphanumeric character.
- Five failed attempts trigger a 15-minute lockout.
- Authentication endpoints are rate-limited to 10 requests per minute per remote IP.

### Tenant and branch administration
- `POST /api/tenants` — create a tenant, its MAIN branch, and the first Owner membership
- `GET /api/tenants/mine`
- `GET/POST/PUT /api/branches`
- `PATCH /api/branches/{id}/active`
- `GET/POST/PUT/DELETE /api/memberships` — Owner-only membership administration for users who have already registered

### Core business operations
- `GET/POST /api/inventory/products`
- `GET /api/inventory/stock`
- `POST /api/inventory/movements`
- `GET/POST /api/customers`, `GET/PUT /api/customers/{id}`
- `GET/POST /api/accounting/accounts`
- `POST /api/accounting/journal-entries`
- `GET /api/accounting/trial-balance`
- `GET/POST /api/sales/orders`
- `GET/POST /api/cafe/kitchen/tickets`, `PUT /api/cafe/kitchen/tickets/{id}/status`
- `GET/POST /api/gamenet/devices`
- `GET /api/gamenet/sessions/active`
- `POST /api/gamenet/sessions`
- `POST /api/gamenet/sessions/{id}/close`

Health checks:
- `GET /health/live`
- `GET /health/ready`

## Tenant and branch security
Authenticated clients may send `X-MG-Tenant` and optionally `X-MG-Branch` as selection hints. These values never grant access by themselves. The API resolves the user ID from the authenticated principal, verifies an active tenant membership in the database, and validates any selected branch against that tenant. Branch-scoped memberships cannot switch branches. Business routes require server-side tenant context and role policies.

Composite foreign keys and tenant-scoped indexes reinforce tenant isolation in the database. The API does not expose arbitrary tenant IDs as authorization claims.

## Accounting and inventory rules
- Journal entries are double-entry and must balance.
- Account references are tenant-scoped.
- Stock movements are immutable ledger records; corrections use compensating movements.
- Stock quantity and value are calculated from movement history using moving-average cost.
- Outbound inventory movements run in serializable transactions and reject negative stock.
- POS checkout records the sales order, payment, stock movements, revenue/receivable journal entry, and cost-of-goods-sold journal entry in one database transaction.
- Game-net session closure creates a sales order and revenue journal entry.
- Café kitchen tickets follow explicit preparation-state transitions and link to a sales order.

## Current limitations — not production-ready yet
- An initial PostgreSQL EF Core migration and model snapshot are committed under `backend/src/MG.Business.Infrastructure/Persistence/Migrations`.
- Startup migration execution is opt-in and PostgreSQL-only through `Database__ApplyMigrations=true`. SQLite test databases use `EnsureCreated` only in tests; a separate SQLite migration set is still required before SQLite production deployments.
- User registration is currently open; production must add email verification/invitation approval and abuse controls.
- Password reset/email delivery, MFA, token revocation UI, audit trails, license activation/module enforcement, suppliers/purchase invoices, tax/VAT, customer credit subledger, online ordering, reservations, and the customer portal are not yet implemented.
- The frontend is still a prototype using local browser storage and is not yet securely connected to this API.
- Production requires persistent Data Protection keys, HTTPS, trusted CORS origins, secret management, PostgreSQL backups, migration/rollback testing, monitoring, audit logging, and a security review.

Do not use this build for live financial records until these remaining controls and deployment steps are completed.

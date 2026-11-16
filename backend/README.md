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
- `Cors__AllowedOrigins__0`, `Cors__AllowedOrigins__1`, ... — exact trusted frontend origins (for local Vite development, allow `http://localhost:5173`; for production, allow only the deployed frontend origin)
- `Database__ApplyMigrations=true` — opt-in startup migration execution for PostgreSQL after reviewing the committed migration set

Never commit production credentials, signing keys, or Data Protection key material.

## Run API and PostgreSQL with Docker Compose

From the repository root:

```bash
cp .env.docker.example .env.docker
```

Edit `.env.docker` and replace the database password with a long random alphanumeric value. Set `MG_FRONTEND_ORIGIN` to the exact origin serving the frontend (for local Vite this is normally `http://localhost:5173`). Keep `MG_API_BIND_ADDRESS=127.0.0.1` unless a properly secured HTTPS reverse proxy and firewall are configured.

```bash
docker compose --env-file .env.docker up -d --build
docker compose --env-file .env.docker ps
docker compose --env-file .env.docker logs -f api
```

The API listens on the host port configured by `MG_API_PORT` (default 8080). PostgreSQL data and ASP.NET Core Data Protection keys are stored in named Docker volumes. The container runs as a non-root user. PostgreSQL migrations are applied at API startup because `Database__ApplyMigrations=true` is set in Compose.

To stop the services without deleting data:

```bash
docker compose --env-file .env.docker down
```

Do not use `docker compose down -v` unless you intentionally want to permanently delete the database and Data Protection keys. This Compose setup is a deployment foundation, not a public internet security boundary: do not expose port 8080 directly to the internet. Put the API behind HTTPS, configure a restrictive firewall, backups, monitoring and secrets management first.

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
- `POST /api/accounting/accounts/initialize-default-chart` — idempotently create the starter chart of accounts
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

### Device presence and connectivity alerts

- `POST /api/sync/heartbeat` — authenticated tenant device heartbeat; stores device identity, last-seen time, branch, platform and client version.
- `GET /api/sync/devices` — Owner/Manager-only tenant device inventory, including last acknowledged journal sequence. A device is considered online while its last heartbeat is within 90 seconds; stale devices return `DEVICE_HEARTBEAT_MISSED`. The response also reports each device's last acknowledged sequence and estimated pending journal-operation count.
- The web client sends a heartbeat every 30 seconds while a tenant workspace is open and displays stale-device alerts to Owners/Managers.

This is tenant-API monitoring only. Alerts are currently surfaced by manager polling in the open web workspace; push notifications, background monitoring independent of an open session, relay to MG Master, and customer-central/MG-Master failover alert routing are not implemented yet. Device IDs are browser-profile identifiers and are not cryptographic device enrollment credentials.

### Durable synchronization journal (initial implementation)

- `POST /api/sync/operations` — append up to 100 tenant-scoped operations per request. Payloads are limited to 64 KiB each, JSON-validated and SHA-256 fingerprinted. Repeating the same operation ID and envelope returns the original sequence; reusing an ID with a different envelope returns a conflict.
- `GET /api/sync/operations?afterSequence=0&limit=100` — pull the tenant operation stream using a durable sequence cursor. The maximum page size is 200.
- `POST /api/sync/acknowledgements` — monotonically acknowledge the highest persisted sequence for a tenant/device pair. Acknowledgements are tenant-scoped and cannot move backwards; they are not yet used for journal compaction.
- `POST /api/sync/acknowledgements` — persist a monotonic, tenant-scoped last-durably-received sequence for each device. Acknowledgements cannot exceed the tenant journal head; stale acknowledgements cannot move a device cursor backwards.
- Every accepted record is associated with the authenticated tenant user and optional tenant branch. Cross-tenant branch references are rejected.
- The journal is append-only and persisted by EF Core. PostgreSQL migrations cover the journal, device presence and per-device acknowledgement cursors; SQLite development/test databases create the schema from the EF model.
- The browser client pulls the tenant journal into a durable IndexedDB inbox and commits the page cursor in the same transaction. Incoming operations are retained for inspection; they are **not yet applied** to local business projections.

This is the first transport/storage slice, **not a complete offline synchronization engine**. It does not yet register or cryptographically authenticate devices, enforce authority leases/fencing epochs, apply journal commands to business aggregates, maintain per-device acknowledgements/cursors, resolve domain conflicts, monitor peer heartbeats, or perform CCS/MG Master failover and failback. Do not use this endpoint as a production replication protocol until those controls and failure tests are implemented.

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
- The frontend now has an initial live API mode: bearer-token login, tenant/branch selection, product/customer management, starter chart setup, POS sale entry with invoice/line discounts and optional kitchen dispatch, kitchen ticket status handling, game-net device registration, customer-linked session start, live server-side charge quote, and session close with payment/receivable posting. The demo dashboard remains available when `VITE_API_BASE_URL` is not configured.
- Browser tokens are held in `sessionStorage`; this is an initial integration, not the final production session architecture. Production should consider an HttpOnly secure-cookie flow, strict CSP, XSS protections, token revocation, and session/device management.
- The live workspace is still incomplete: purchase invoices, full accounting screens/reports, game-session settlement UI, kitchen ticket creation from POS, online ordering, licensing, and customer portal are not finished.
- Production requires persistent Data Protection keys, HTTPS, trusted CORS origins, secret management, PostgreSQL backups, migration/rollback testing, monitoring, audit logging, and a security review.

Do not use this build for live financial records until these remaining controls and deployment steps are completed.

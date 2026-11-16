# Authentication, tenant resolution, and inventory API

## Authentication
- ASP.NET Core Identity API endpoints are mounted under `/auth`.
- Registration and login use Identity's supported API endpoints; login can return short-lived bearer tokens.
- Password policy requires 12 characters, upper/lowercase, a digit, and a non-alphanumeric character.
- New accounts are locked for 15 minutes after five failed attempts.
- Unique email addresses are required.
- Inactive application users are rejected by the request pipeline even if they still hold a previously issued token.

## Tenant selection and authorization
- Authenticated requests may select a tenant with `X-MG-Tenant`; this value is only a selector, never proof of access.
- The server resolves the user ID from the validated authentication principal and verifies an active database membership for the selected tenant.
- Unknown, malformed, or unauthorized tenant selections are rejected.
- `X-MG-Branch` is validated against the selected tenant. A branch-scoped membership cannot select a different branch.
- Tenant-scoped endpoints require the `TenantMember` policy. Inventory writes additionally require Owner, Manager, or InventoryManager.
- Tenant creation is an authenticated onboarding operation. It creates the tenant, initial MAIN branch, and Owner membership in one SaveChanges operation.
- `GET /api/tenants/mine` returns the current user's active tenant memberships.

## Inventory endpoints
- `GET /api/inventory/products`
- `POST /api/inventory/products`
- `GET /api/inventory/stock`
- `POST /api/inventory/movements`

All inventory operations are filtered by the server-resolved tenant. Stock movement creation requires a selected branch and an active inventory-tracked product. The movement history is immutable; corrections should be made with compensating movements rather than editing historical rows.

## Operational cautions
- The current bearer token implementation uses ASP.NET Core Identity API token support, not JWT. Production deployments must persist and protect ASP.NET Core Data Protection keys, enforce HTTPS, configure trusted origins/CORS, rate-limit registration/login, and use a managed secret store.
- Registration is currently open. Before public launch, decide whether tenant creation is invite-only, email-verified, or admin-approved and add anti-abuse controls.
- The API currently has no refresh-token revocation UI, audit log, password reset/email sender configuration, or multi-factor authentication.
- EF Core migrations and production database initialization are still required. Do not use `EnsureCreated` in production.
- Sales, purchasing, POS, accounting posting, café, game-net sessions, licensing, and customer-facing ordering are not yet implemented as operational APIs.

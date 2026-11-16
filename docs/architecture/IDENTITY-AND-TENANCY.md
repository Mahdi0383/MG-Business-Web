# Identity and tenant membership foundation

## Implemented
- The infrastructure persistence context now derives from ASP.NET Core Identity's IdentityDbContext and stores ApplicationUser records with display name, active state, and creation timestamp.
- TenantMembership links a user to a tenant and assigns a tenant-scoped role.
- Membership can be branch-scoped; the composite foreign key includes both tenant and branch, preventing assignment to a branch owned by another tenant.
- A unique (TenantId, UserId) index prevents duplicate memberships for the same user in one tenant.
- Roles currently modeled: Owner, Manager, Accountant, Cashier, Waiter, GameOperator, InventoryManager, and Viewer.
- SQLite persistence tests verify valid user membership and reject cross-tenant branch assignments.

## Not implemented; do not treat as authentication
This is only the identity data model and persistence layer. The API does not yet issue or validate access tokens, expose registration/login/password-reset endpoints, resolve a tenant from an authenticated principal, enforce role/permission policies, or protect business endpoints. No business-data endpoint should be exposed until these controls exist and are integration-tested.

## Required security gates
1. Configure Identity password, lockout, email uniqueness, and account lifecycle policies.
2. Implement secure authentication and token/session lifecycle with secrets supplied only through environment/secret storage.
3. Resolve tenant membership server-side from the verified user identity; client-supplied tenant identifiers are selection hints only and must always be validated against active membership.
4. Implement policy-based authorization for tenant and branch scope, with deny-by-default behavior.
5. Add API integration tests proving unauthenticated access is denied, inactive memberships are denied, users cannot cross tenant boundaries, and branch-scoped users cannot access other branches.
6. Add audit events for authentication, membership changes, role changes, and privileged operations.

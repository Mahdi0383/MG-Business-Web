# MG Master and MG Control Center — Control-plane specification

**Status:** design baseline; no production Master API or creator portal is implemented yet.

## Product boundary

The creator's management surface is a separate application and security boundary:

1. **MG Control Center** — creator-only web application, deployed separately from the customer app.
2. **MG Master API** — private control-plane API for creator administration, tenant provisioning, subscription entitlement, device/central-server registration, signed leases, update channels and alert intake.
3. **MG Business Customer App** — customer-facing application; no creator routes or creator credentials are shipped to it.
4. **Customer Central Server** — customer-owned local authority for business data and LAN devices.

MG Master stores control-plane metadata. It is not the default home for customer invoices, customer lists, accounting journals or inventory.

## Creator access and security

- Separate creator identity store and separate database from tenant/customer identities.
- MFA required for every creator account; WebAuthn/passkeys preferred, TOTP recovery as a fallback.
- Roles: PlatformOwner, LicenseAdministrator, SupportEngineer, ReleaseManager, Auditor. Sensitive actions require a reason and are append-only audited.
- Creator account recovery requires a controlled break-glass procedure with notification.
- No password viewing, including customer owner passwords. Creator actions may reset/revoke credentials but cannot retrieve them.
- Support access is time-limited, scoped to one tenant, visible to the tenant owner, and audited. Any support access to business data requires an explicit policy and customer authorization.
- Rate limits, IP/device risk checks, CSRF protection, short sessions, secure cookies, CSP, secrets manager and key rotation are mandatory.

## Master data model (initial)

### TenantAccount
- TenantId (globally unique, not the customer's chosen slug)
- DisplayName, LegalName?, CountryCode, CreatedAtUtc
- Status: PendingActivation | Active | Suspended | Closed
- SuspensionCode?, SuspensionReason?, SuspendedAtUtc?
- PrimaryOwnerUserId, SupportPolicyVersion

### Subscription
- SubscriptionId, TenantId, PlanCode
- StartsAtUtc, ExpiresAtUtc, GraceEndsAtUtc?
- Status: Trial | Active | PastDue | Suspended | Expired | Cancelled
- SeatLimit, DeviceLimit, BranchLimit
- RenewalReference?, CreatedByCreatorId, UpdatedAtUtc
- Extension and plan changes are versioned events, never destructive edits.

### ModuleEntitlement
- TenantId, ModuleCode, Enabled, EffectiveFromUtc, EffectiveToUtc?
- Optional per-branch scope and configuration JSON validated against a module schema.
- Module entitlement is signed into leases and rechecked on renewal.

### CustomerUser
- Master stores identity linkage and status metadata only. Customer credentials remain in the tenant identity system.
- TenantId, UserId, RoleSummary, Status, DisabledAtUtc?, LastLoginAtUtc?
- Creator may disable a user or force password reset; no plaintext or reversible password storage.

### CentralServerRegistration
- TenantId, CentralServerId, PublicKeyThumbprint, DisplayName, Platform, AppVersion
- Status: ClaimPending | Active | Revoked | Replaced
- ClaimedAtUtc, LastHeartbeatAtUtc, CurrentAuthorityEpoch
- One active central server per tenant in the initial release; controlled migration/recovery workflow for replacement.
- First owner login does not automatically bind whichever device happened to log in. The owner explicitly confirms a one-time setup claim on the intended central host.

### DeviceRegistration
- TenantId, DeviceId, CentralServerId?, PublicKeyThumbprint, BranchId?
- Status: Pending | Active | Revoked | Stale
- FirstSeenAtUtc, LastHeartbeatAtUtc, LastAcknowledgedSequence
- Per-device credential rotation and revocation.

### EntitlementLease
- LeaseId, TenantId, CentralServerId, AuthorityEpoch
- IssuedAtUtc, NotBeforeUtc, ExpiresAtUtc, OfflineGraceEndsAtUtc
- Enabled modules, limits, subscription version, policy version
- Signed with an asymmetric platform key. Private signing key stays in managed KMS/HSM; customer apps pin the public verification key.

### PlatformAuditEvent
- Creator ID, action, target tenant/user/server, reason, timestamp, correlation ID, before/after metadata, source IP/device, result.
- No secrets or raw credentials in audit payloads.

## Activation and account lifecycle

1. Creator creates a tenant in Control Center, assigns modules, seat/device/branch limits, subscription start/end and an initial owner invitation.
2. Master issues a one-time, short-lived, single-use activation claim. The owner sets a unique username/password and completes MFA/recovery setup where supported.
3. Owner installs the Customer Central Server package on the intended host, signs in and confirms "Register this computer as my central server".
4. Master binds the tenant to the central server's generated identity and public key using an atomic claim transaction. Replays and parallel claims are rejected.
5. Owner invites users. Each user receives an invitation to set their own credentials; role and branch/module scopes are assigned by the tenant owner.
6. Additional devices enroll through the central server or owner approval; they do not become central servers by signing in.
7. Server replacement requires owner approval plus recovery proof, creator-assisted audited recovery, or a previously configured recovery key. Old server credentials are revoked and authority epoch increases.

## License and offline policy

- Customer sign-in is username/password; subscription entitlement is not represented by a reusable plaintext license code.
- Master signs a lease with subscription, module, limit, central-server identity, authority epoch and offline-grace claims.
- Default offline grace: 30 days from the last successful trusted Master validation, measured from signed time anchors and monotonic elapsed time where available.
- At grace expiry or when the next trusted validation confirms suspension/expiry, the app blocks new business operations. It does not delete, corrupt, encrypt-destructively or remotely wipe customer data.
- Read/export/backup access during suspension is governed by contract and must preserve a customer data retrieval path.
- A remote suspension cannot instantly lock an isolated device. It is enforced at the next Master/CCS validation or lease boundary.
- Reinstatement issues a new lease and authority epoch after subscription and policy checks pass.

## Required creator workflows

- Create, edit and suspend tenant accounts.
- Create/extend/cancel subscriptions and configure plans, modules, seat/device/branch limits.
- Search and inspect tenant users, role/status, devices, central server, version, last heartbeat and alert history.
- Disable/restore a user; revoke a device; initiate central-server replacement/recovery.
- Publish signed application releases, choose stable/beta channels, staged rollout cohorts and minimum compatible versions.
- View license validation, failover, synchronization backlog and connectivity alerts.
- Create a temporary support session with tenant consent and full audit.
- Export audit logs and subscription reports.

## API surface (initial proposal)

Creator-only:
- POST /master/tenants
- GET /master/tenants, GET /master/tenants/{tenantId}
- PATCH /master/tenants/{tenantId}/status
- POST /master/tenants/{tenantId}/owner-invitations
- POST /master/tenants/{tenantId}/subscriptions
- PATCH /master/subscriptions/{subscriptionId}
- PUT /master/tenants/{tenantId}/modules
- GET /master/tenants/{tenantId}/users
- PATCH /master/tenants/{tenantId}/users/{userId}/status
- GET /master/tenants/{tenantId}/devices
- POST /master/tenants/{tenantId}/devices/{deviceId}/revoke
- POST /master/tenants/{tenantId}/central-server/recovery
- GET /master/audit-events

Customer/server:
- POST /master/activation/claim
- POST /master/central-servers/heartbeat
- POST /master/leases/renew
- POST /master/devices/enroll
- POST /master/devices/heartbeat
- POST /master/alerts/batch

Every endpoint is tenant-scoped, authenticated, rate-limited and idempotent where it changes state. Creator endpoints require a creator role and audited reason for suspension, revocation, plan override and recovery.

## Delivery gates before production

- Separate Master database, migrations and backup/restore drills.
- MFA and creator RBAC tests, tenant-isolation tests, abuse/rate-limit tests.
- KMS-backed signing and key-rotation/revocation tests.
- Replay-resistant activation and central-server claim tests.
- Lease clock rollback, offline grace, expiry, suspension and reinstatement tests.
- Central-server replacement and stale-server fencing tests.
- Customer export/recovery tests proving suspension never destroys business data.
- Independent security review before onboarding paying customers.

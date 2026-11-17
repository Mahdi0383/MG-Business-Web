# MG Business — Control Center, licensing and customer server model

Status: Architecture decision baseline  
Date: 2026-11-16

## 1. Separate applications and trust boundaries

- MG Control Center: independent creator-only application, not a hidden route in the customer app. Uses a separate admin identity store, MFA, strict roles and audited privileged actions.
- MG Master API: central control-plane API for tenants, subscriptions, entitlements, server/device registration, signed leases, status and alerts.
- MG Business customer app: customer-facing application. Enforces tenant, branch, role and module permissions server-side.
- Customer Central Server (CCS): customer-operated service coordinating local devices and storing the customer's primary local business database.
- MG Master Failover: separately provisioned optional service for internet failover, not a replacement for customer data ownership.

## 2. Customer provisioning and owner identity

Creator provisions a tenant with display/legal name, unique tenant ID, initial owner username and one-time activation credential, subscription dates/status, licensed modules and limits, region/data residency, backup/failover options and support-access policy.

Initial username/password is a bootstrap credential only. On first login require password change and configured MFA enrollment. Passwords are never retrievable by creator staff; they can issue reset or revoke sessions.

## 3. Customer Central Server binding

First login must not silently bind whichever machine happens to be used. Use a one-time claim flow:
1. Owner authenticates and chooses Register this machine as my central server.
2. Server generates a cryptographic key pair and server identity.
3. Owner confirms with a short-lived one-time code or explicit owner-session approval.
4. Master API atomically binds one active primary CCS identity to the tenant and issues a signed server certificate/lease.
5. Additional devices enroll with owner/admin authorization and receive device-bound credentials.
6. Server replacement uses recovery/rebinding, audit, notification and revocation of the old identity.

## 4. Tenant user management

Owner and authorized admins invite/create users, assign tenant-scoped roles, branch scopes and module permissions, disable accounts and revoke device sessions. Suggested roles: Owner, General Manager, Accountant, Sales Manager, Cashier, Inventory Operator, Café Operator, Waiter and Game-net Operator. Effective access is the intersection of role permissions, branch scope, module entitlement and tenant status.

## 5. Subscription and module entitlement

Statuses: Trial, Active, PastDue, Suspended, Expired, Cancelled.

Entitlements are versioned and signed, and include tenant, modules, limits, effective dates, server/device identities, policy version and lease expiry. A local editable feature flag is never proof of entitlement.

Module/subscription changes create immutable audit events and a new entitlement version. Renewal never resets customer data or identity.

## 6. Suspension, expiry and data preservation

Suspension blocks authentication and new operations at next trusted validation. Offline devices are bounded by their signed offline lease; immediate enforcement is impossible while disconnected. Preserve customer data and provide policy-approved export/recovery. Reinstatement issues fresh entitlements and resumes operation in a controlled manner.

## 7. Creator privileges and customer privacy

Control Center can view tenant metadata, subscription, health, device inventory, sync state and security events. Business-record access or support impersonation is separate, disabled by default, and requires explicit customer approval or documented contractual basis, least-privilege scope and expiry, visible support-session indicator, immutable audit trail and immediate revocation.

No staff member can view user passwords. Account recovery uses reset flows.

## 8. Security baseline

Separate admin/customer authentication realms and signing keys. Require MFA for creator accounts; prefer phishing-resistant MFA. Use RBAC/ABAC for creator actions and two-person approval for destructive/high-impact actions. Use signed, short-lived, tenant-scoped activation codes and entitlements; rotate/revoke server and device keys. Apply rate limits, anomaly detection and audit retention. Keep secrets out of Git. Customer servers authenticate with device-bound certificates, not shared passwords.

## 9. Planned API boundaries

Control plane: tenant provisioning/status; owner bootstrap/recovery; subscription/module entitlement; server claim/rebinding/revocation; device enrollment/revocation; signed lease issue/renewal; heartbeat/alert ingestion; failover activation and fencing epoch.

Customer plane: tenant user authentication/membership; branch/device enrollment; sync push/pull and acknowledgements; operational APIs; audit and conflict review.

All endpoints require explicit authorization, tenant scoping, idempotency and appropriate audit coverage.

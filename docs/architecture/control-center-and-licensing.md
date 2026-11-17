# MG Control Center and licensing protocol

**Status:** required design; backend and creator UI are not yet implemented.

## Product boundaries
- **MG Control Center** is a separate creator-only web application with its own authentication surface, deployment configuration and navigation. It must not share the customer application's session cookie or expose creator APIs through customer authorization.
- **MG Master API** is the creator control plane and licensing authority. It owns tenant provisioning, subscription state, module entitlements, CCS claims, device enrollment, signed leases, administrative audit and optional continuity coordination.
- **MG Business Customer API** serves customer business operations. It validates the signed entitlement lease but does not possess the MG Master signing private key.

## Creator access
- Creator accounts are provisioned by the product owner; there is no public creator registration.
- MFA for every creator identity; phishing-resistant MFA preferred for super-admins.
- Separate roles: SuperAdmin, LicenseManager, SupportOperator, ReleaseManager and Auditor.
- Step-up authentication for suspension, subscription changes, CCS transfer, support access and key rotation.
- Immutable audit event for every privileged action, recording actor, target, timestamp, reason, correlation ID and before/after metadata.
- No API or UI endpoint returns a customer's password or password hash. Password reset is the only supported creator action.
- Support sessions are time-limited, tenant-approved where feasible, scoped to a task and visibly logged.

## Customer onboarding and credentials
1. Creator creates a customer organization, subscription term, allowed modules, device/branch limits and the initial owner identity.
2. The system sends an invitation or one-time activation link. The initial owner sets a unique password; creators do not choose or see the final password.
3. The first activation transaction creates a one-time CCS claim challenge bound to the tenant, owner, expiry and nonce.
4. The CCS presents its generated node identity and proves possession of its private key. MG Master atomically consumes the challenge and binds the tenant to that CCS.
5. A repeated claim attempt, another browser login or a second CCS cannot silently replace the binding.
6. Additional users join through owner/manager invitations and receive tenant-scoped roles, branch scopes and module permissions.
7. Device enrollment creates revocable per-device credentials; browser local IDs alone are not sufficient proof of identity.

## Subscription and entitlement data
The canonical subscription record includes tenant ID, subscription ID, plan/status, start and expiry UTC, enabled module identifiers, per-module limits, allowed branches/devices/CCS count, offline grace policy (default 30 days), policy version, entitlement version, revocation sequence, suspension reason and audit references.

Changing a module or subscription creates a new entitlement version. It does not rewrite historical business data.

## Signed entitlement lease
MG Master issues a compact signed lease containing tenant/subscription/CCS IDs, lease ID, monotonic entitlement and fencing versions, issued/not-before/expiry times, signed server-time anchor, enabled modules, limits, policy flags, offline grace end, status and revocation sequence.

The signing private key stays in an isolated MG Master secret store or HSM. Customer software embeds only public verification keys. Keys support key IDs and overlapping rotation windows. A lease is rejected if signature, tenant binding, CCS identity, policy version or time constraints are invalid.

A 30-day offline grace starts from the last successfully verified lease/server-time anchor, not from a user-editable local date. After grace expires, new business operations are blocked. Existing databases, journal entries, backups and export functions remain intact. A restored subscription or cleared suspension results in a newly signed lease and normal operation resumes after validation.

## Suspension and recovery
- Suspending a tenant, subscription or user changes authoritative status in MG Master and increments its revocation sequence.
- Connected CCS/devices receive the update on their next authenticated validation/heartbeat. A fully disconnected device cannot be remotely reached; its cached lease remains bounded by the 30-day grace.
- The customer UI enters a read-only/restricted state according to policy. It must never delete the tenant's data.
- Reactivation requires a new valid lease after MG Master confirms the suspension has been cleared.
- CCS replacement requires a documented recovery flow: verify owner identity, revoke the old CCS credential, issue a transfer authorization, bind the replacement node, and audit both nodes. If the old CCS is unavailable, use an owner-approved recovery policy with additional verification.

## Planned MG Master API boundary
- POST /creator/customers — provision tenant and initial owner invitation.
- GET /creator/customers and GET /creator/customers/{tenantId} — customer inventory and status.
- POST /creator/customers/{tenantId}/suspend and /reactivate.
- PUT /creator/customers/{tenantId}/subscription — change term, plan and module grants.
- POST /activation/claim-challenge — authenticated owner requests first CCS claim challenge.
- POST /activation/claim — CCS proves node key and atomically claims the tenant.
- POST /leases/renew — CCS/device validates and receives signed entitlement.
- POST /devices/enroll and /devices/revoke.
- POST /alerts/relay — CCS/device relays queued health incidents.
- POST /support-sessions — create scoped, expiring and audited support access.

Customer APIs must not accept client-supplied module grants, subscription status, tenant ownership or authority epochs as trusted facts.

## Implementation gate
This design is not implemented by the current feature branch. Do not expose creator administration or issue production customer credentials until:
1. separate creator identity/MFA and role policies exist;
2. MG Master persistence, signing keys and key rotation are deployed;
3. one-time CCS claim and recovery flows are tested against replay/race attacks;
4. entitlement verification and offline-grace enforcement are covered by clock rollback, suspension, renewal and data-retention tests;
5. customer and creator audit logs are operational.
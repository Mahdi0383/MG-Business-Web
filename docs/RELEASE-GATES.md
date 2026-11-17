# MG Business Web — Production Release Gates

This checklist is a release control, not a claim that the platform is production-ready.
A release is blocked until every mandatory gate below has objective evidence attached to the release candidate.

## Current validated baseline

- [x] Frontend CI tests and production build pass on code head `0f92fbd75843161e274cbd2d28e39753041d8e4d` (run `43980450449`).
- [x] Backend SQLite build, model-snapshot verification and integration/domain tests pass on code head `0f92fbd75843161e274cbd2d28e39753041d8e4d` (run `43980450373`).
- [x] API/PostgreSQL CI passes on code head `0f92fbd75843161e274cbd2d28e39753041d8e4d` (run `43980443175`).
- [x] Sales receipt creation and journal posting are transactional.
- [x] Partial and full customer-balance settlement API exists.
- [x] Payment history is tenant/branch scoped.
- [x] Payment collection persists idempotency keys and request fingerprints; exact retries replay the original receipt and changed payloads are rejected.
- [x] Game-session closure replays the committed invoice after a lost response and rejects changed settlement payloads.
- [x] Inventory movement validates the active stock-tracked product and stock ledger inside the serializable write transaction.
- [x] Manual inventory stock-movement API requires `Idempotency-Key`, persists a request fingerprint under a tenant/branch unique constraint, replays exact retries and rejects changed-payload reuse.
- [x] Inventory workspace exposes per-product receipt/issue/return/adjustment entry; on network failure it atomically queues the local movement and reuses its idempotency key for retry.
- [x] Public ordering rejects ambiguous multi-tenant routing.
- [x] Sync journal validates payload integrity, actor scope, idempotency envelopes and monotonic cursors.
- [x] PWA shell does not cache authentication, API or synchronization traffic.
- [x] Customer profile create, update and deactivation commands use versioned local projections, durable outbox records, authoritative server replay and transactional inbound projection/acknowledgement with origin-echo handling and stale-version conflicts.
- [x] Initial inventory offline movement slice includes cached products/baselines, durable local commands, atomic local projection/outbox writes, server replay with idempotency and stale-balance fencing, inbound stock projection and origin-echo suppression.
- [x] Browser device enrollment, signed sync request verification, credential rotation/revocation and authority lease/fencing foundations pass current frontend, backend SQLite and API/PostgreSQL CI.
- [ ] Production database concurrency/race testing, multi-node nonce replay validation and operational security review remain mandatory.
- [ ] Offline transactional business command handlers for sales, accounting, purchasing, café and GameNet remain incomplete; inventory and customer are the only implemented local-first business slices.
- [ ] MG Master / Control Center failover, failback, entitlement lifecycle and creator-only administration remain incomplete.
- [ ] Customer portal, complete online ordering/reservations, full accounting/purchasing and feature-parity UI remain incomplete.

## Mandatory blockers before live financial use

### Identity and device trust
- [x] Replace browser-profile UUID trust with WebCrypto ECDSA P-256 enrollment, single-use challenges and digest-only opaque credentials; browser private keys are non-extractable IndexedDB CryptoKeys.
- [x] Bind credentials and browser device IDs to tenant, branch and actor scope.
- [x] Add same-key recovery, proof-of-possession key rotation and authenticated device/lease revocation endpoints.
- [x] Verify `/api/sync/*` requests (except enrollment lifecycle) against active credential, actor/tenant/branch binding, key version, timestamp, body hash, ECDSA signature and one-time nonce.
- [ ] Complete independent security review, multi-node nonce replay/race tests and lost-device operational runbook.

### Authority, failover and split-brain safety
- [x] Implement short-lived ECDSA-signed tenant/branch authority leases with serializable acquisition and a pinned client verification key.
- [x] Enforce expiry, monotonic fencing epochs, branch-scope binding, independently signed source-device identity and stale-epoch rejection on sync push when production-required lease mode is enabled; follower devices share the current branch epoch.
- [x] Add backend integration coverage for lease signature verification, competing-device exclusion and fenced push; final PostgreSQL concurrency/race proof remains open.
- [ ] Implement MG Master / Control Center authority handoff and audited failback.
- [ ] Run multi-node partition, clock-skew, stale-leader and recovery tests.

### Offline business transactions
- [x] Implement the first complete inventory movement slice: cached product/stock baseline, atomic IndexedDB movement + outbox write, stable local idempotency key, local weighted-average valuation, signed-lease epoch capture, server replay and stock-ledger idempotency.
- [x] Apply inbound inventory journal operations to local stock projections transactionally; suppress originating-device echoes and stop on stale base-balance conflicts.
- [ ] Implement equivalent transactional local command handlers and inbound projections for sales, accounting, purchasing, café and GameNet.
- [ ] Add domain-specific conflict resolution and operator review workflows for inventory and all other business domains.
- [ ] Add durable conflict/health alert history and an independent notification relay.
- [ ] Verify financial, stock, café and game-session consistency under multi-device offline replay and production database concurrency.

### Financial correctness and retry safety
- [x] Add persisted idempotency keys and request fingerprints to payment collection.
- [ ] Prove concurrent collection cannot overpay an invoice under the production database (SQLite concurrency regression exists; production PostgreSQL race proof remains required).
- [ ] Add retry/replay tests for timeouts after commit and client reconnect.
- [ ] Add immutable audit trails, period controls, reversal/credit-note workflows and reconciliation.
- [ ] Validate tax, currency, rounding and fiscal requirements for each deployment jurisdiction.

### Operational product completeness
- [ ] Complete operational UI flows for accounting, purchasing, inventory, café, game-net, memberships, online orders and reservations.
- [ ] Complete licensing, entitlements, tenant provisioning and suspension/recovery workflows.
- [ ] Complete backup, restore, retention, export and disaster-recovery procedures.
- [ ] Complete monitoring, structured logs, metrics, alerting and support diagnostics.
- [ ] Complete accessibility, responsive-device and real-hardware acceptance testing.

### Security and deployment
- [ ] Run dependency, secret, static-analysis and penetration/security checks.
- [ ] Validate TLS, secure headers, CORS allowlists, rate limits and production secret handling.
- [ ] Test database migrations against clean install and supported upgrade paths.
- [ ] Verify backup restoration and rollback against production-sized data.
- [ ] Publish a signed release artifact, SBOM, migration notes and operator runbook.
- [ ] Obtain explicit business-owner acceptance after end-to-end UAT.

## Release decision

**Current decision: NOT APPROVED for production financial operations.**

Passing CI demonstrates that the current automated test/build gates pass; it does not prove the incomplete controls above. Do not enable live financial/offline multi-node operation until all mandatory blockers are checked and independently verified.


## Implementation notes — 2026-11-17

The cryptographic enrollment, request-signature middleware, credential rotation/revocation, signed authority lease and fencing items marked complete above are implemented in code and covered by automated tests. The current protocol and canonical messages are documented in `docs/architecture/DEVICE-ENROLLMENT-AND-REQUEST-AUTH.md`.

Important scope boundaries:

- Device credentials are explicitly bound to tenant, actor, branch and device. Browser device IDs and IndexedDB credentials are also isolated by tenant/branch/actor.
- Enrollment lifecycle endpoints require authenticated tenant membership and fresh proof-of-possession challenges. All other `/api/sync/*` routes are subject to request-signature verification when production enforcement is enabled.
- The browser client supports same-key recovery, explicit proof-of-possession key rotation and credential revocation; rotation/revocation invalidate cached authority state.
- Signed lease acquisition and fencing are implemented, but no claim is made that a multi-node control plane, automated failover/failback or production PostgreSQL race certification is complete.
- The only complete local-first business vertical slices are customer profiles and inventory movements. Financial, café and GameNet offline command processing is still blocked.

CI status is recorded against exact commit SHAs and workflow run IDs in the validated baseline above. Any new code change requires fresh frontend, SQLite backend and PostgreSQL/API validation before release review.


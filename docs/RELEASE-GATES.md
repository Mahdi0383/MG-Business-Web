# MG Business Web — Production Release Gates

This checklist is a release control, not a claim that the platform is production-ready.
A release is blocked until every mandatory gate below has objective evidence attached to the release candidate.

## Current validated baseline

- [x] Frontend CI (tests and production build) passes on the current development head.
- [x] Backend SQLite CI passes on the current development head.
- [x] API CI passes on the current development head.
- [x] Sales receipt creation and journal posting are transactional.
- [x] Partial and full customer-balance settlement API exists.
- [x] Payment history is tenant/branch scoped.
- [x] Payment collection persists idempotency keys and request fingerprints; exact retries replay the original receipt and changed payloads are rejected.
- [x] Game-session closure replays the committed invoice after a lost response and rejects changed settlement payloads.
- [x] Inventory movement validates the active stock-tracked product and stock ledger inside the serializable write transaction.
- [x] Public ordering rejects ambiguous multi-tenant routing.
- [x] Sync journal validates payload integrity, actor scope, idempotency envelopes and monotonic cursors.
- [x] PWA shell does not cache authentication, API or synchronization traffic.

## Mandatory blockers before live financial use

### Identity and device trust
- [ ] Replace browser-profile device IDs with cryptographic device enrollment.
- [ ] Bind enrolled device keys to tenant, branch, actor and revocation state.
- [ ] Add rotation, revocation, recovery and lost-device procedures.
- [ ] Verify every device command against its enrolled identity and authorization.

### Authority, failover and split-brain safety
- [ ] Implement signed authority leases with issuer trust roots.
- [ ] Enforce lease expiry, monotonic fencing epochs and replay protection.
- [ ] Prove that an expired or fenced authority cannot commit writes.
- [ ] Implement MG Master / Control Center authority handoff and audited failback.
- [ ] Run multi-node partition, clock-skew, stale-leader and recovery tests.

### Offline business transactions
- [ ] Implement transactional local command handlers for every enabled business module.
- [ ] Apply inbound operations to local projections; do not merely retain them in an inbox.
- [ ] Add domain-specific conflict resolution and operator review workflows.
- [ ] Add durable conflict/health alert history and an independent notification relay.
- [ ] Verify that accounting, stock, sales, café and game-session operations remain balanced and consistent during offline replay.

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

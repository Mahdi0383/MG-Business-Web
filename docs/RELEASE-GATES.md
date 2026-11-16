# MG Business Web — Production Release Gates

This checklist is a release control, not a claim that the platform is production-ready.
A release is blocked until every mandatory gate below has objective evidence attached to the release candidate.

## Current validated baseline

- [x] Frontend CI (tests and production build) passes on code head `82e77a8bf9aa8db7c57fb5e44b1b30bd28fc78ce` (run `43960052093`); the subsequent stock enum mapping change is awaiting its own CI run.
- [x] Backend SQLite build, model-snapshot verification and integration/domain tests pass on code head `100acedc10b0d471d44740b9d05e9ead93ea0a09` (run `43958619643`).
- [x] PostgreSQL API CI, including PostgreSQL migration application and integration/domain tests, passes on code head `100acedc10b0d471d44740b9d05e9ead93ea0a09` (run `43958607238`).
- [x] Sales receipt creation and journal posting are transactional.
- [x] Partial and full customer-balance settlement API exists.
- [x] Payment history is tenant/branch scoped.
- [x] Payment collection persists idempotency keys and request fingerprints; exact retries replay the original receipt and changed payloads are rejected.
- [x] Game-session closure replays the committed invoice after a lost response and rejects changed settlement payloads.
- [x] Inventory movement validates the active stock-tracked product and stock ledger inside the serializable write transaction.
- [x] Manual inventory stock-movement API requires `Idempotency-Key`, persists a request fingerprint under a tenant/branch unique constraint, replays exact retries and rejects changed-payload reuse; offline/local stock commands and sales-order-originated stock writes still need end-to-end command idempotency.
- [x] Inventory workspace exposes per-product receipt/issue/return/adjustment entry and sends a cryptographically random idempotency key; retries within the open form reuse the same key. This is online-only until the offline stock command pipeline is implemented.
- [x] Public ordering rejects ambiguous multi-tenant routing.
- [x] Sync journal validates payload integrity, actor scope, idempotency envelopes and monotonic cursors.
- [x] PWA shell does not cache authentication, API or synchronization traffic.
- [x] Customer profile create, update and deactivation commands use versioned local projections, durable outbox records, authoritative server replay and transactional inbound projection/acknowledgement with origin-echo handling and stale-version conflicts.
- [x] Frontend CI passed on intermediate development head `9c337aee8e91ed81e927abbaa312cd432bef699c`; subsequent changes to documentation and tests are awaiting CI on the current head.\n- [ ] Current branch head `8134cf8471fa08c6982b552ac4204fc498ebbf2c`: frontend/backend/API CI and PostgreSQL migration validation must all pass before release review.

## Mandatory blockers before live financial use

### Identity and device trust
- [x] Replace browser-profile UUID trust with WebCrypto ECDSA P-256 enrollment, single-use challenges and digest-only opaque credentials; browser private keys are non-extractable IndexedDB CryptoKeys.
- [x] Bind credentials and browser device IDs to tenant, branch and actor scope.
- [x] Add same-key recovery, proof-of-possession key rotation and authenticated device/lease revocation endpoints.
- [x] Verify `/api/sync/*` requests (except enrollment lifecycle) against active credential, actor/tenant/branch binding, key version, timestamp, body hash, ECDSA signature and one-time nonce.
- [ ] Complete independent security review, multi-node nonce replay/race tests and lost-device operational runbook.

### Authority, failover and split-brain safety
- [x] Implement short-lived ECDSA-signed tenant/branch authority leases with serializable acquisition and a pinned client verification key.
- [x] Enforce expiry, monotonic fencing epochs, device/branch binding and stale-epoch rejection on sync push when production-required lease mode is enabled.
- [x] Add backend integration coverage for lease signature verification, competing-device exclusion and fenced push; final PostgreSQL concurrency/race proof remains open.
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


### Cryptographic device trust design — 2026-11-17

- Added `docs/architecture/DEVICE-ENROLLMENT-AND-REQUEST-AUTH.md` as a proposed security contract covering one-time enrollment challenges, non-extractable Web Crypto signing keys, credential hashing/rotation/revocation, canonical request signatures, replay protection, and signed authority leases with fencing.
- This is design documentation only; no enrollment or request-signing code is implemented by this document. Browser UUID identity remains untrusted, and this release gate remains open.


### Device-enrollment persistence reconciliation — 2026-11-17

- Added an EF-generated reconciliation migration for the nullable single-use challenge consumption timestamp and refreshed the authoritative model snapshot.
- The migration is additive/relaxing only: it changes `ConsumedAtUtc` to nullable, matching the challenge's unconsumed state. Existing rows remain valid.
- The temporary snapshot-generation workflow has been removed. Enrollment endpoint and persistence tests still require a clean SQLite and PostgreSQL CI run before this gate can close.


## Cryptographic device request authentication

- Browser enrollment creates a Web Crypto ECDSA P-256 key pair. The private key is non-extractable and persisted as a structured-clone CryptoKey in IndexedDB; the server stores the public SPKI key and only a SHA-256 digest of the one-time-returned device credential.
- Enrollment challenges are random, short-lived, single-use and bound to the authenticated tenant, actor, branch context and device ID. A proof-of-possession signature is required to complete enrollment.
- Authenticated sync heartbeat, pull, push and acknowledgement requests are signed over method, path/query, tenant, branch, actor, device, key version, timestamp, UUID nonce and exact serialized body SHA-256. The server checks credential digest, active status, actor scope, key version, ECDSA signature and a two-minute timestamp window, then persists the nonce under a unique key before executing the endpoint.
- Device credentials are tenant-scoped so one browser identity can be used in the tenant's authorized branches; enrollment proof itself remains branch-context-bound. A future branch-restricted credential can set an explicit branch scope.
- Production startup now fails unless `SyncSecurity:RequireDeviceSignatures=true`. Enrollment bootstrap routes are excluded from request-signature middleware but still require authenticated tenant membership and challenge proof.
- This closes device request authentication and replay protection at the API boundary; key rotation/revocation APIs, expired nonce cleanup, browser key recovery, signed authority leases/fencing and end-to-end multi-node recovery tests remain open release gates.


### Device enrollment recovery and nonce retention — 2026-11-17

- Device enrollment now persists the browser's non-extractable private key before the server can commit. A repeated proof using that same key and a fresh challenge rotates the one-time credential and key version, allowing recovery after a lost response without exposing the private key.
- Signed request middleware deletes expired replay nonces for the authenticated tenant/device during normal traffic and records last credential use. The replay store therefore remains bounded for active devices; idle-device records expire from acceptance and are removed on the next request.
- Integration coverage verifies enrollment response recovery, credential version rotation, replay rejection and request-body tampering. Key revocation/rotation administration, device loss recovery and lease fencing remain separate gates.


## Current branch validation
- Frontend CI passed on intermediate development head `9c337aee8e91ed81e927abbaa312cd432bef699c`.
- Current branch head at this documentation update: `0bfe5d64880c88fb43b94696b60d557efb9693a6`. Frontend/backend/API CI and PostgreSQL migration validation must all pass on the final candidate before release review.

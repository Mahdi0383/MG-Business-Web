# MG Business — Delivery roadmap

Status: Working plan; estimates are planning ranges, not delivery promises.  
Date: 2026-11-17

## Product outcome

Deliver a modular business platform with customer web/PWA app, customer-operated central server, independent local operation on every device, optional MG Master internet failover, creator-only MG Control Center, subscription/module licensing, and integrated accounting, inventory, café and game-net workflows.

## Workstreams

### A. Platform foundation
- [ ] Incrementally convert frontend to strict TypeScript.
- [ ] Define versioned API contracts and shared error/idempotency conventions.
- [ ] Establish production SQLite migrations as well as PostgreSQL migrations.
- [ ] Centralized configuration, structured logs, tracing, health and metrics.
- [ ] CI build, unit/integration/security/dependency checks and reproducible release artifacts.

### B. Identity, tenant and licensing
- [ ] Separate creator identity realm and Control Center.
- [ ] Creator MFA and privileged-action audit.
- [ ] Customer owner bootstrap, password reset, MFA and session/device revocation.
- [x] Cryptographic browser device enrollment, tenant/branch/actor binding, signed sync requests, key rotation, same-key recovery and revocation APIs are implemented; final security review and operational lost-device runbook remain open.
- [ ] One-time CCS claim, recovery/rebinding and creator-side Control Center device lifecycle.
- [ ] Signed subscription/module entitlements and 30-day maximum offline validation grace.
- [ ] Suspension/expiry that preserves data and provides recovery/export paths.

### C. Offline-first and high availability
- [ ] Local durable business database and operation journal on every supported client platform; browser PWA currently has IndexedDB outbox/inbox and customer/inventory projections.
- [x] Initial tenant-scoped append-only sync journal, idempotent push/pull transport, IndexedDB outbox/inbox and per-device durable acknowledgement cursor (transport foundation; customer creation is the first applied business vertical slice below).
- [x] Per-device, per-branch server acknowledgement watermarks; tenant-wide manager acknowledgement remains separately scoped.
- [x] Atomic IndexedDB customer creation: local customer projection and branch-originated outbox command commit in one transaction; server-side customer creation replay and transactional inbound projection on other devices are implemented.
- [x] Versioned customer deactivation is now included in the same local-first vertical slice: local inactive projection/outbox is atomic, server replay uses optimistic concurrency, and inbound deactivation/acknowledgement is transactional with origin-echo and stale-version handling.
- [x] Server-side `customer.profile.created` replay creates the authoritative customer with the client-generated stable ID in the same EF transaction as the journal insert; exact retries are idempotent.
- [x] Transactional inbound customer create/update projection with atomic inbox acknowledgement, origin-operation echo suppression and durable stale-version conflict records; browser IndexedDB command handling has separate verification requirements.
- [x] First inventory offline vertical slice: cached products and raw weighted-average stock baselines, durable local stock movement commands, atomic IndexedDB outbox/projection writes, server replay with idempotency and stale-balance fencing, inbound stock projection and originating-device echo suppression; the inventory workspace queues movements automatically when the API is unreachable.
- [ ] Transactional local business command handlers and inbound projections for sales, accounting, purchasing, café and GameNet; customer and inventory are the only complete local-first business slices.
- [x] Persist branch-scoped unresolved incoming conflicts and surface their open count in the workspace sync status; automatic resolution remains intentionally unavailable.
- [x] Customer workspace falls back to the durable local customer projection on fetch/network failure, including online-but-unreachable API conditions; HTTP/API errors remain visible rather than being masked.
- [x] Revalidate persisted inbox payload SHA-256 immediately before replay; missing, malformed or tampered hashes fail closed.
- [x] Enforce branch/tenant/manager visibility in local inbox reads and processing, including unassigned tenant-wide context.
- [x] Scope device backlog telemetry to the active membership's authorized branch and tenant-wide visibility.
- [ ] Per-domain conflict policy and operator reconciliation UI.
- [x] Initial tenant device heartbeat, Owner/Manager connectivity inventory and stale-heartbeat alerts in the open workspace.
- [ ] Independent CCS heartbeat service, peer health monitoring, durable alert queue and MG Master relay.
- [ ] Optional tenant-isolated MG Master failover data plane.
- [x] Short-lived signed authority leases, serializable acquisition, monotonic fencing epochs and stale-device/branch push rejection are implemented as the first HA authority foundation.
- [ ] MG Master/CCS distributed lease coordination, multi-node split-brain prevention and tested failback.
- [ ] LAN-only, WAN-only, total partition and recovery test suites.

### D. Business modules
- [ ] Accounting: chart of accounts, double-entry, subledgers, periods, tax, reports, audit and closing.
- [ ] Sales/POS: checkout, discounts, payments, refunds, receivables and idempotency.
- [ ] Purchasing: suppliers, purchase invoices, costs, taxes and returns.
- [ ] Inventory: multi-warehouse, movements, costing and stock counts.
- [ ] Café/restaurant: menu, add-ons, kitchen workflow, tables/orders and receipt printing.
- [ ] Game-net: devices, sessions, customer-linked billing, transfers, group sessions and settlement.
- [ ] Customer experience: online ordering, reservations, membership/club and customer portal.
- [ ] Reports, exports, integrations and configurable peripherals.

### E. Release and operations
- [ ] Responsive PWA and Android packaging strategy.
- [ ] Signed updates, staged rollout, compatibility gates and rollback.
- [ ] Backup/restore drills, monitoring, alerting and support tooling.
- [ ] Threat model, penetration test, load test and disaster-recovery exercise.
- [ ] Pilot with synthetic/test data, then limited opt-in customer pilot.
- [ ] Production release checklist, support policy, documentation and migration tooling.

## Release gates

Gate 0 — Architecture and CI: approved domain boundaries, threat model, data classification, architecture records and reproducible CI.

Gate 1 — Core pilot: tenant-safe authentication, customer/branch/users, product/customer, POS sale, inventory ledger and balanced accounting posting; migrations and backup/restore tested.

Gate 2 — Operational modules: purchase flow, accounting reports, café and game-net workflows, printer/payment integration, audit and responsive PWA.

Gate 3 — Multi-node pilot: durable local journals on all clients, offline workflows, reconnect sync, conflict review, CCS monitoring and alerting. No cloud failover claim until fencing and failback tests pass.

Gate 4 — High availability: optional MG Master failover workspace, signed leases, epoch fencing, split-brain prevention, CCS failback, 30-day offline license enforcement and recovery tests.

Gate 5 — Commercial release: independent Control Center, customer onboarding, subscription/module administration, production hardening, security/load tests, backup restore proof, documentation and pilot sign-off.

## Latest delivery increment — 2026-11-16

- Hardened incoming replay so persisted payload bytes are re-hashed immediately before projection processing; tampering, missing hashes and unavailable secure cryptography stop replay before projection reads.
- Corrected tenant-wide inbox reads for unassigned manager context so branch-scoped rows are not exposed.
- Corrected per-device backlog telemetry to count only journal rows visible to that device's active membership and branch.
- Replaced the misleading single per-device acknowledgement watermark with durable branch-specific cursors; switching branches no longer suppresses another branch's older pending operations.
- Added the first local business projection store for customers with validated input and an atomic projection-plus-outbox write; this now connects to authoritative server replay and transactional inbound projection on other devices.
- Added a regression integration test that acknowledges a later operation in Branch A, switches the same device to Branch B, and verifies Branch B's earlier operation remains pending.
- Hardened sales settlement validation so overpayments and partial payments without a customer return validation errors rather than surfacing domain exceptions as server errors.
- Added regression tests for the above. Customer creation is now the first real local-first business vertical slice across local projection, outbox, authoritative server replay and inbound projection. Sales, accounting, inventory, café and game-net workflows are not yet offline-ready.

## Latest delivery increment — 2026-11-17

- Implemented cryptographic browser-device enrollment with ECDSA P-256 proof-of-possession, one-time tenant/branch/actor/device-bound challenges, non-extractable IndexedDB private keys and digest-only server credentials.
- Implemented signed sync request verification with key-version and credential validation, canonical method/path/context/body-hash signatures, timestamp checks and persisted one-time nonce replay protection.
- Added same-key enrollment recovery, explicit proof-of-possession key rotation and authenticated credential revocation. Credential changes revoke the device's authority leases and invalidate the browser's cached lease.
- Implemented signed short-lived tenant/branch authority leases, serialized acquisition, monotonically increasing fencing epochs and stale-epoch push rejection. Production multi-node failover/failback and PostgreSQL race certification remain open.
- Extended the local-first inventory slice through the actual workspace: manual movements are atomically stored with the local stock projection/outbox when the API is unavailable, then replayed with idempotency and stale-balance protection.
- Refreshed release gates to distinguish passing CI from production approval and to identify incomplete verticals and operational controls. Fresh CI is required for the current documentation head.
- PR #3 remains open and Draft. Production financial use, merge and deployment remain blocked until all release gates have implementation and test evidence.

### Validated product foundations

- Backend foundations exist for tenant/branch identity, membership roles, customer profiles, product catalog, inventory movements/valuation, sales orders/payments, double-entry accounting journals, café kitchen tickets, GameNet sessions and sync transport.
- Customer and inventory have local-first projections, durable outbox commands, server replay and inbound projection handling. Sales, accounting, purchasing, café and GameNet do not yet have equivalent offline command processing.
- Existing APIs and domain entities are not equivalent to completed operator workflows. Accounting reports/period controls, purchasing, complete online customer ordering/reservations, full café/game-net workflows, hardware integrations, Control Center and licensing remain release work.
- Automated tests passing on a commit do not replace production database concurrency testing, security review, multi-device partition/recovery tests, real-device acceptance and business-owner UAT.

## Planning estimate## Planning estimate

The current repository is a foundation, not a finished commercial product. Full delivery depends on completing the remaining business verticals, Control Center/licensing, security and HA validation, real-device acceptance and customer UAT. Any time estimate is provisional until the remaining work is decomposed and measured against a stable test environment.

## Current engineering status

- The current implementation is more advanced than several historical delivery notes below this point in earlier revisions; this section is the authoritative current status.
- Cryptographic device enrollment, signed sync requests, credential recovery/rotation/revocation, signed authority leases and fencing have implementation and automated test coverage. They still require independent security review, PostgreSQL concurrency/race proof, multi-node partition testing and a lost-device runbook.
- Customer and inventory are the only complete local-first business slices. The inventory workspace now queues stock movements locally when the API is unavailable. Sales, accounting, purchasing, café and GameNet still need transactional local command handlers and inbound projections.
- Backend foundations exist for tenant/membership, customers, products/inventory, sales/payments, accounting journals, café kitchen tickets and GameNet sessions. The operator workflows, reporting, reconciliation, hardware integration and full feature parity are not complete.
- MG Master, CCS/Control Center, creator identity, signed module entitlements, customer portal, online ordering/reservations, backup/restore, observability, deployment hardening and production UAT remain release blockers.
- The normative architecture and release decision are maintained in REFERENCE-ARCHITECTURE.md and ../RELEASE-GATES.md. Do not merge, deploy or enable live financial operations until all mandatory release gates pass.


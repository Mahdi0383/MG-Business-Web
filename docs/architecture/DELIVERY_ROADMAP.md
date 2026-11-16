# MG Business — Delivery roadmap

Status: Working plan; estimates are planning ranges, not delivery promises.  
Date: 2026-11-16

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
- [ ] One-time CCS claim, recovery/rebinding and device enrollment.
- [ ] Signed subscription/module entitlements and 30-day maximum offline validation grace.
- [ ] Suspension/expiry that preserves data and provides recovery/export paths.

### C. Offline-first and high availability
- [ ] Local durable database and operation journal on every supported client.
- [x] Initial tenant-scoped append-only sync journal, idempotent push/pull transport, IndexedDB outbox/inbox and per-device durable acknowledgement cursor (transport foundation only; not yet applied to business domain projections).
- [x] Per-device, per-branch server acknowledgement watermarks; tenant-wide manager acknowledgement remains separately scoped.
- [x] Atomic IndexedDB customer creation: local customer projection and branch-originated outbox command commit in one transaction; server-side customer creation replay is implemented; inbound projection on other devices remains pending.
- [x] Server-side `customer.profile.created` replay creates the authoritative customer with the client-generated stable ID in the same EF transaction as the journal insert; exact retries are idempotent.
- [ ] Transactional local business command handlers, incoming operation application and per-domain conflict resolution.
- [x] Revalidate persisted inbox payload SHA-256 immediately before replay; missing, malformed or tampered hashes fail closed.
- [x] Enforce branch/tenant/manager visibility in local inbox reads and processing, including unassigned tenant-wide context.
- [x] Scope device backlog telemetry to the active membership's authorized branch and tenant-wide visibility.
- [ ] Per-domain conflict policy and operator reconciliation UI.
- [x] Initial tenant device heartbeat, Owner/Manager connectivity inventory and stale-heartbeat alerts in the open workspace.
- [ ] Independent CCS heartbeat service, peer health monitoring, durable alert queue and MG Master relay.
- [ ] Optional tenant-isolated MG Master failover data plane.
- [ ] Fencing epochs, failover lease, split-brain prevention and tested failback.
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
- Added the first local business projection store for customers with validated input and an atomic projection-plus-outbox write; this does not yet enable offline customer reconciliation on the server.
- Added a regression integration test that acknowledges a later operation in Branch A, switches the same device to Branch B, and verifies Branch B's earlier operation remains pending.
- Hardened sales settlement validation so overpayments and partial payments without a customer return validation errors rather than surfacing domain exceptions as server errors.
- Added regression tests for the above. This remains transport/security hardening: no claim is made that business projections are offline-wired.

## Planning estimate

The current repository is a foundation, not a finished commercial product. A focused solo development effort with AI-assisted implementation may need roughly 20–30 months for the full scope, depending on test environments, hardware, deployment infrastructure and user acceptance cycles. A narrower paid pilot may be possible earlier, but only after its release gate passes. Recalibrate estimates from measured delivery throughput and test results.

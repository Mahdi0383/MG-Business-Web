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
- [x] Initial tenant-scoped append-only sync journal, idempotent push/pull transport, IndexedDB outbox/inbox and per-device durable acknowledgement cursor (transport foundation; customer creation is the first applied business vertical slice below).
- [x] Per-device, per-branch server acknowledgement watermarks; tenant-wide manager acknowledgement remains separately scoped.
- [x] Atomic IndexedDB customer creation: local customer projection and branch-originated outbox command commit in one transaction; server-side customer creation replay and transactional inbound projection on other devices are implemented.
- [x] Server-side `customer.profile.created` replay creates the authoritative customer with the client-generated stable ID in the same EF transaction as the journal insert; exact retries are idempotent.
- [x] Transactional inbound customer create/update projection with atomic inbox acknowledgement, origin-operation echo suppression and durable stale-version conflict records; browser IndexedDB command handling has separate verification requirements.
- [ ] Transactional local business command handlers, incoming operation application and per-domain conflict resolution.
- [x] Persist branch-scoped unresolved incoming conflicts and surface their open count in the workspace sync status; automatic resolution remains intentionally unavailable.
- [x] Customer workspace falls back to the durable local customer projection on fetch/network failure, including online-but-unreachable API conditions; HTTP/API errors remain visible rather than being masked.
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
- Added the first local business projection store for customers with validated input and an atomic projection-plus-outbox write; this now connects to authoritative server replay and transactional inbound projection on other devices.
- Added a regression integration test that acknowledges a later operation in Branch A, switches the same device to Branch B, and verifies Branch B's earlier operation remains pending.
- Hardened sales settlement validation so overpayments and partial payments without a customer return validation errors rather than surfacing domain exceptions as server errors.
- Added regression tests for the above. Customer creation is now the first real local-first business vertical slice across local projection, outbox, authoritative server replay and inbound projection. Sales, accounting, inventory, café and game-net workflows are not yet offline-ready.

## Planning estimate

The current repository is a foundation, not a finished commercial product. A focused solo development effort with AI-assisted implementation may need roughly 20–30 months for the full scope, depending on test environments, hardware, deployment infrastructure and user acceptance cycles. A narrower paid pilot may be possible earlier, but only after its release gate passes. Recalibrate estimates from measured delivery throughput and test results.


### Offline customer availability correction — 2026-11-16

- The customer workspace previously fell back to IndexedDB only when the browser reported `navigator.onLine === false`. Browsers can report an active network connection while the tenant API is unreachable, which caused a valid local customer projection to be hidden during real outages.
- Added a shared network-unavailable classifier and use it in the customer loader. Fetch-level network failures now display locally stored customers; HTTP responses such as authorization, validation and server errors are not silently converted into offline success.
- Added unit coverage for fetch failures versus HTTP/application errors. This improves offline read availability for customers only; it does not claim that other business modules are offline-ready.


### Incoming customer replay safety and conflict visibility — 2026-11-16

- The local inbox processor now applies only the supported customer-create projection path. Customer profile/contact update command types remain fail-closed for manual review until a complete outbound edit workflow and version-precondition contract exist; this avoids accepting commands that the local product cannot safely originate or reconcile.
- Incoming conflict records retain their originating branch. The local store exposes unresolved conflicts through the tenant/status/created-time index, and the workspace status line shows the open conflict count alongside pending outbox and unapplied inbox counts.
- This is conflict visibility, not conflict resolution. Operators cannot yet merge or discard conflicts through a governed workflow, and unresolved entries intentionally block later journal replay in sequence order.

## Architecture baseline publication — 2026-11-16

- Published [REFERENCE-ARCHITECTURE.md](./REFERENCE-ARCHITECTURE.md) as the normative target architecture and engineering contract.
- Consolidated component ownership, trust boundaries, deployment/persistence strategy, domain boundaries, command envelope, API conventions, local transaction rules, sync/conflict lifecycle, fencing/failback, security, licensing, observability, backup/restore, verification suites and delivery gates.
- Recorded seven architecture decisions: modular monolith first, local journal/at-least-once sync, append-only financial facts, fenced failover, separate creator control plane, SQLite/PostgreSQL deployment roles, and signed entitlement leases.
- This is completion of the documented architecture baseline, not completion of its implementation. Gate 0 still requires threat-model review, contract verification and reproducible CI evidence. The implementation blockers in section 16 of the reference architecture remain release blockers.

### Outgoing sync envelope integrity gate — 2026-11-16

- Before transmitting a pending batch, the client now verifies tenant and device ownership, branch scope, operation identity/type, schema version, timestamp, supported authority epoch, payload size/shape and SHA-256 integrity.
- Missing secure cryptography, malformed payloads, cross-device rows and tampered payloads fail closed before network transmission; no acknowledgement is written for rejected local queue data.
- Added regression tests for payload tampering and foreign-device queue entries.
- This hardens the browser sync transport boundary; it does not replace server authorization, device cryptographic enrollment or signed authority leases.

### Customer edit concurrency contract — 2026-11-16

- Added ADR-008 defining versioned customer updates, database-enforced optimistic concurrency, operation idempotency, origin-device echo handling and atomic local/inbound projection requirements.
- The current API still lacks a required baseVersion and database concurrency token; inbound projection still supports customer creation only. Customer updates therefore remain disabled and fail-closed.
- The next implementation increment must add the version migration and 409 stale-write behavior before wiring the browser outbox or inbound update handler.
- Required two-device, duplicate-delivery, origin-echo, stale-version, crash-atomicity and SQLite/PostgreSQL tests are specified in ADR-008.

### Customer optimistic concurrency implementation — 2026-11-16

- Added a non-null customer aggregate version initialized to 1 and an EF concurrency-token mapping.
- Added the customer version migration with a default of 1 so existing rows retain a valid version.
- Customer API read/create responses now expose the version. Customer updates require a positive baseVersion; successful updates advance the version and stale writes return HTTP 409 without overwriting the newer record.
- Added SQLite-backed API integration coverage for version advancement and stale-write rejection, plus a domain version progression test.
- Offline customer update commands remain fail-closed: server journal replay, durable idempotency outcomes, atomic inbound projection, origin-device echo handling and conflict lifecycle are still required before enabling offline edits.

### Customer update sync vertical slice — 2026-11-16

- Added authoritative server replay for `customer.profile.updated`: schema/branch validation, tenant-scoped customer lookup, required positive baseVersion, aggregate version increment and journal insertion in the same EF SaveChanges transaction.
- Exact retries reuse the existing operation ID and durable journal acknowledgement without applying the customer mutation twice. Reusing an operation ID with a changed envelope remains rejected by the existing idempotency gate.
- A stale customer baseVersion is rejected with HTTP 409; the business mutation and journal insert are not committed.
- Added browser-side atomic customer update + outbox enqueue, version-aware inbound projection, durable conflict classification and origin-device echo suppression. Acknowledgement advances the local confirmed version.
- Added API/SQLite tests for accepted update, duplicate retry and stale update rejection; added classifier tests for matching versions, stale versions and origin echoes.
- Remaining verification before treating the browser slice as production-ready: IndexedDB crash/restart integration tests, two-browser end-to-end tests, and PostgreSQL migration/concurrency parity.

### Browser inbox schema hardening — 2026-11-16

- Added IndexedDB schema version 7 migration for the incoming inbox status index. IndexedDB does not accept boolean values as index keys; the previous compound index could throw DataError when processing an inbox page.
- Existing inbox rows are backfilled to a numeric `appliedIndex` (0/1), while the public `applied` boolean remains unchanged. New rows and all apply/acknowledge paths maintain both values.
- Added fake IndexedDB-backed tests for local customer edit/outbox atomicity, single-pending-edit protection, remote update projection, stale-version conflict persistence and origin-device echo suppression.
- Frontend CI on commit 4552b80a passed after the schema correction.

### Customer sync authorization hardening — 2026-11-16

- Customer create/update journal commands now require Owner, Manager or Cashier membership in addition to authenticated tenant/device/branch ownership.
- Added an integration test proving a Viewer can enroll a device but cannot push a customer creation command.
- The role check is applied to new commands; exact retries remain idempotent and do not repeat business effects.

- Additional verification added for customer updates: missing/zero baseVersion is rejected, changed payload reuse under an existing operation ID is rejected, and a multi-operation batch containing a stale customer version rolls back earlier customer mutations and journal writes.
- Customer create/update sync commands now enforce the same Owner/Manager/Cashier write-role boundary as the online customer API.

- Local acknowledgement now fails closed and aborts the entire IndexedDB acknowledgement transaction if a pending customer update's local baseVersion/pendingBaseVersion no longer matches. A regression test confirms the outbox item is not silently marked acknowledged.


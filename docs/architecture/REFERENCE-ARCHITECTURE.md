# MG Business — Reference Architecture and Engineering Contracts

**Status:** normative architecture baseline  
**Revision:** 1.0 — 2026-11-16  
**Scope:** customer app, device clients, Customer Central Server (CCS), MG Master control plane, optional continuity data plane and creator-only Control Center.

This document defines the target architecture and implementation rules. It is not a claim that all components are implemented. The delivery roadmap remains the implementation-status authority.

## 1. Non-negotiable principles

1. Local-first: each supported device owns durable local data and a journal; browser cache is not a business database.
2. Tenant isolation is enforced server-side from authenticated identity and membership, never from a client-supplied tenant identifier alone.
3. Local acceptance, CCS acknowledgement and financial finalization are distinct states.
4. Network delivery is at-least-once; business effects must be idempotent. Exactly-once transport is not promised.
5. Financial facts, payments, inventory movements and audit events are append-only; corrections are explicit reversal/adjustment commands.
6. There is no universal last-write-wins policy for money, stock or shared resources.
7. Failover requires signed authority leases and monotonically increasing fencing epochs; timeout or DNS changes cannot promote a node.
8. MG Master control plane and creator administration are separate from tenant business data.
9. No production/HA/offline-ready claim without test evidence for the exact topology and workflows.
10. Data remains recoverable and exportable during suspension; licensing never deletes or corrupts customer data.

## 2. System context and ownership

| Component | Responsibility | Data ownership |
|---|---|---|
| MG Device Client | UI, local workflows, durable local journal, sync | Device-local journal, projections and device keys |
| Customer Central Server | Tenant coordination and primary API while holding valid lease | Primary tenant database, device registry, server journal and audit |
| MG Master Control Plane | Tenant provisioning, subscriptions, signed entitlements, registration, update metadata and alert relay | Control-plane metadata only |
| MG Master Continuity Data Plane | Optional licensed continuity workspace | Explicitly replicated, tenant-isolated operational data |
| MG Control Center | Creator-only platform operations | Platform administration and audit; no default ownership of business records |
| External integrations | Payment, messaging, DNS and other providers | Only data required by each integration contract |

### Trust boundaries

- Device ↔ CCS: TLS, enrolled device identity, tenant/branch/actor authorization on every command.
- CCS ↔ Master: service identity, narrow scopes, signed requests, replay protection and key rotation.
- Device/CCS ↔ continuity: tenant-scoped credentials plus verified fencing lease.
- Creator ↔ Control Center: separate identity issuer, database, MFA, sessions and privileged-action audit.
- Provider integrations: isolated credentials, webhook signature verification, explicit consent and idempotency.

Creator credentials, database administrator secrets and signing private keys must never ship in customer clients or installers.

## 3. Deployment and persistence

- CCS runs on customer-controlled hardware or a customer-selected host. LAN access is the default; remote access is opt-in and requires supported TLS termination/reverse-proxy/tunnel configuration.
- Database files, backups, application secrets and keys are outside the public web root.
- Static frontend hosting is not an API/database host. GitHub Pages is preview/documentation hosting only.
- Development, test, staging and production use isolated credentials and datasets.
- SQLite is supported for device-local and validated single-node deployments. PostgreSQL is the preferred shared/hosted production database.
- Provider-specific migrations are explicit. Never run PostgreSQL migrations against SQLite or use development schema creation as a production migration strategy.
- Every schema change requires a forward migration, backup precondition, compatibility policy and tested recovery plan.

## 4. Domain boundaries

The initial deployment is a modular monolith. Boundaries must be enforced in code/contracts before extracting services.

| Domain | Responsibility | Core invariant |
|---|---|---|
| Identity & Access | Users, memberships, roles, sessions, device credentials | Least privilege and tenant/branch scope |
| Tenant & Branch | Tenant lifecycle and branch configuration | Every business record is tenant-owned |
| Catalog & Pricing | Products, services, modifiers, price/tax versions | Historical documents retain captured prices |
| Customers | Profiles, contacts, membership | Versioned edits and personal-data minimization |
| Sales & Payments | Orders, tenders, refunds, receivables | Idempotent posting; no overpayment |
| Inventory & Purchasing | Suppliers, purchase docs, stock ledger, valuation | Append-only movements and explicit costing |
| Accounting | Accounts, journal, periods, tax, close | Balanced double-entry; posted entries immutable |
| Café | Tables, orders, add-ons, kitchen tickets | Shared Sales/Inventory/Accounting contracts |
| Game Net | Devices, sessions, rates, transfers, settlement | Stable session identity and auditable time |
| Online Customer | Orders, reservation requests, club/portal | Scarce capacity confirmed by authoritative coordinator |
| Sync & Operations | Journal, inbox/outbox, cursors, conflicts, health | Idempotent apply and durable conflict records |
| Licensing | Entitlements, signed leases, grace and module limits | Server-side enforcement; preserve data |
| Control Center | Creator accounts, provisioning, support, releases | Separate identity and audit boundary |

Modules are licensed capabilities, not separate copies of business truth. Café and Game Net activity must use shared ledgers and accounting contracts.

## 5. Data model and identity contract

Every client-originated command carries:
- tenant ID, branch ID (nullable only for explicitly tenant-wide operations), actor ID and enrolled device ID;
- globally unique operation ID, aggregate type/ID and optional aggregate base version;
- command type, schema version and canonical payload;
- occurrence time, server receipt time, correlation ID and causation ID;
- authority epoch and lease ID when shared authority is required;
- canonical payload SHA-256 integrity hash.

IDs are generated before first transmission so offline-created records retain identity after synchronization. Server authorization derives tenant/actor from the authenticated principal and validates that they match the command context.

Source-of-truth rules:
- Journals, payments, stock movements and audit facts are append-only.
- Operational aggregate state is versioned and derived from accepted commands.
- Read models and local projections are rebuildable.
- UI state is never authoritative proof of a completed transaction.
- Financial/legal deletion is represented by void/reversal/status events, not destructive deletion.

Data classes:
- Restricted: credentials, recovery secrets, signing keys, payment tokens and personal data.
- Confidential: tenant financial, sales, inventory, staff and operational records.
- Internal: platform diagnostics and non-customer metadata.
- Public: explicitly published catalog/marketing content.
Logs must not contain passwords, tokens, payment secrets or raw customer payloads.

## 6. API and command processing contract

Every retryable write uses a versioned command envelope. The exact payload is strongly typed per command; unknown major schema versions are quarantined, never guessed.

Processing order:
1. Authenticate; validate tenant membership, branch, role and enrolled device.
2. Validate command schema, domain rules and authority lease/epoch.
3. Check idempotency key and any prior durable result.
4. Load aggregate and validate expected base version.
5. Apply domain rules in a database transaction.
6. Persist business changes, journal position, audit/outbox and idempotency result atomically.
7. Commit, then return durable acknowledgement and canonical server version.
8. Publish notifications asynchronously from a durable outbox.

API conventions:
- HTTPS JSON, versioned routes or explicit schema versions for breaking changes.
- Standard problem response includes stable error code, title/detail, trace ID and field errors.
- 400 invalid input; 401 unauthenticated; 403 forbidden scope; 404 absent/invisible resource; 409 concurrency/idempotency/business conflict; 429 rate limit; 5xx unexpected service failure.
- Bounded cursor pagination for operational feeds.
- Reusing an idempotency key with a different canonical payload is rejected.
- Contract tests run against both client and server.

## 7. Offline storage and synchronization

For each supported offline command, one local database transaction must commit:
- validated local projection/domain mutation;
- durable outbox operation;
- local audit metadata;
- local aggregate version/sequence update.

If any part fails, none is reported as locally committed. UI states must distinguish local-only, pending, centrally acknowledged, conflicted and rejected.

Sync rules:
- At-least-once delivery with exponential backoff, jitter and bounded concurrency.
- Server stores operation ID and business effect in one transaction.
- Pull cursors are durable and scoped by tenant, device and branch/visibility context.
- Acknowledgement cursor alone is not proof of business application.
- Revalidate persisted payload hashes immediately before projection/application.
- Compact an outbox item only after durable acknowledgement and retention checks.
- Browser background sync is an optimization; startup and foreground recovery are required.

Conflict lifecycle: detected → awaiting review → resolved/rejected/superseded. Each record retains original operation, aggregate/version, branch, actor/device, reason, timestamps and resolution command reference. Resolution is a new audited domain command; operators do not edit/delete conflict records directly.

Domain policy:
- Accounting: immutable posted entries; reverse/adjust, never overwrite.
- Sales/payments: stable IDs and provider references; duplicate effects forbidden.
- Inventory: preserve movements; shortages/negative projection require explicit policy and review.
- Customer fields: merge only explicitly safe fields under version checks; sensitive conflicts require review.
- Catalog/prices/tax/roles: versioned authoritative configuration; offline edits are proposals.
- Game Net: stable session IDs, monotonic elapsed-time accounting and clock anomaly review.
- Reservations/shared scarce resources: provisional offline state; authoritative confirmation only after coordination.

## 8. High availability and split-brain prevention

A signed authority lease contains tenant, authority node, lease ID, monotonically increasing fencing epoch, issue/not-before/expiry times, permitted capabilities/modules, subscription version and bounded offline grace. Signing private keys remain in managed or hardware-backed storage; clients pin trusted public keys.

- CCS writes only while holding a valid lease for the current epoch.
- Continuity promotion requires verified tenant, licensed continuity, healthy replication and a higher epoch.
- Nodes reject stale epochs after observing a newer epoch.
- Returning CCS joins read-only; reconciliation verifies operation ranges, hashes, signatures, idempotency outcomes and conflicts.
- A new higher-epoch CCS lease is issued only after reconciliation gates pass.
- If authority cannot be proven, preserve local journals as pending; do not create a second global writer.

Offline devices may independently accept local commands. Last-stock sales, reservations and other scarce shared resources cannot be globally guaranteed across isolated partitions. The UI must mark provisional results and each domain must define reconciliation/compensation. No zero-data-loss or seamless-failover claim is permitted without fault-injection evidence.

## 9. Security architecture

- Authentication uses short-lived sessions/tokens, refresh rotation, revocation and session inventory.
- Passwords use adaptive one-way hashing; never plaintext or reversible storage.
- Authorization is role + tenant + branch + module + resource policy, deny by default.
- Device enrollment uses one-time claim, device-generated key pair, server binding, rotation and revocation.
- Creator identity realm is separate from customer identity.
- Secrets live in a secret manager or OS-protected store; no secrets committed or bundled.
- TLS, strict CORS allowlist, production HSTS, secure cookies and CSRF controls where cookie auth is used.
- Browser CSP, output encoding, dependency pinning/audits and no unsafe dynamic script execution.
- API size limits, validation, rate limits, anti-enumeration, timeouts and bounded query cost.
- Encrypted backups, key rotation, append-only privileged/financial audit and redaction.
- Supply-chain controls: lockfiles, secret/dependency/code scanning, SBOM, signed artifacts and provenance.
- Privacy controls: purpose limitation, retention, export, correction and deletion subject to legal accounting retention.

## 10. Licensing and modules

User authentication is separate from subscription entitlement. MG Master signs tenant/server-bound leases containing module set, limits, subscription version, authority epoch and expiry. Default maximum offline validation grace is 30 days from last trusted validation, enforced using signed time anchors and monotonic elapsed time where available.

After validated expiry/suspension, new licensed operations are blocked without deleting/corrupting data. Export, backup and recovery remain available according to contract and law. A disconnected device cannot learn about a remote suspension until reconnect or lease expiry. Feature gating is enforced server-side, not only by hiding UI.

## 11. Observability and alert lifecycle

Every node reports node/app/schema identity, last heartbeat, local DB health, CCS/Master reachability, queue depth/oldest age, last successful sync, open conflict count, lease expiry and clock drift.

Connectivity transitions are debounced. Alerts are durable events with severity, source, first/last occurrence, acknowledgement and resolution. Offline alerts queue locally and relay idempotently when a trusted route returns. A missing heartbeat means stale/unreachable, not proof of permanent loss.

Structured logs use trace/correlation IDs. Metrics include latency, error rate, DB health, sync throughput/lag, retry count, queue age, conflicts, lease status and backup age. Liveness does not depend on the database; readiness validates required dependencies.

## 12. Backup, restore and portability

- CCS backups are encrypted, scheduled, versioned and stored outside live data directories.
- A backup is valid only after a successful restore drill.
- SQLite uses a consistent snapshot/backup mechanism; copying a live DB file without coordination is not accepted.
- PostgreSQL recovery includes database and required configuration/key procedures.
- Define RPO/RTO per deployment tier and expose them to tenant owners.
- Restore validates schema, integrity, journal continuity, tenant isolation and startup.
- Tenant export includes portable data, data dictionary and attachment manifest.
- Disaster recovery never rewrites immutable accounting history.

## 13. Required verification suites

- Domain unit tests and deterministic calculation tests.
- API integration tests on SQLite and PostgreSQL migration targets.
- Tenant/branch authorization and cross-tenant negative tests.
- Contract compatibility and schema-version tests.
- Offline queue, crash/restart, duplicate/reordered delivery and payload-tampering tests.
- Multi-device concurrency, stale base-version and conflict lifecycle tests.
- HA fencing, split-brain, promotion, failback and partition fault-injection tests.
- Backup/restore and migration upgrade tests.
- Security, dependency, secret, auth/session, rate-limit and abuse tests.
- Browser/device matrix, RTL/LTR, accessibility, PWA install and storage quota/error handling.
- Load/soak tests for sync bursts and large tenant histories.

Green CI proves only the jobs and commit it actually executed. It does not replace staging, security review, hardware validation or recovery drills.

## 14. Delivery gates

1. Architecture baseline: boundaries, data classification, threat model, command/API contracts, persistence strategy and ADRs reviewed.
2. Secure single-tenant pilot: identity, tenant/branch, audit, catalog, customer, sales, inventory and balanced accounting with migrations and restore proof.
3. Offline domain pilot: one complete atomic local command → server idempotency → inbound projection → conflict workflow; repeat by domain.
4. Operational modules: purchasing, café, Game Net, reports, customer flows and peripherals.
5. Multi-node operations: enrollment, health inventory, durable alerts, LAN/WAN partition and recovery tests.
6. Licensed continuity: signed leases, fencing, replicated data plane, promotion/failback drills and 30-day offline enforcement.
7. Commercial release: creator Control Center, onboarding, subscription/module admin, signed updates, support, security/load tests and pilot acceptance.

A gate is complete only with linked CI, migration, integration/fault test, runbook and owner sign-off.

## 15. Architecture decision records

**ADR-001 — Modular monolith first.** Keep a modular monolith until measured scale or independent deployment justifies service extraction. This reduces early distributed-transaction and operational complexity.

**ADR-002 — Local journal and at-least-once sync.** Persist local mutation and outbox atomically; retries are expected. Every handler must be idempotent and conflicts durable.

**ADR-003 — Append-only financial and inventory facts.** Posted journal entries, payments, audit facts and stock movements are corrected by reversal/adjustment commands.

**ADR-004 — Fenced failover.** Signed leases and increasing epochs control write authority; timeouts and DNS cannot prevent split brain.

**ADR-005 — Separate creator control plane.** Creator identity and Control Center are isolated from customer identities and business records.

**ADR-006 — SQLite local, PostgreSQL shared production.** Provider-specific migrations and parity tests are mandatory; compatibility is not assumed.

**ADR-007 — Signed entitlement leases.** Authenticate users normally; distribute scoped signed leases. Private signing keys never ship to customer devices.

## 16. Implementation boundary at baseline date

Implemented foundation includes ASP.NET Core API/domain/persistence, tenant membership and branch authorization context, initial sales/inventory/accounting endpoints, durable server sync journal, browser IndexedDB outbox/inbox primitives, per-device branch cursors, initial device heartbeat/presence, versioned customer create/update replay and local projection, optimistic concurrency, payload hash revalidation, branch-scoped conflict visibility, monotonic customer read caching and limited customer offline read fallback.

Not yet end-to-end: creator Control Center and Master services, separate creator identity realm, cryptographic device enrollment, signed entitlement/authority leases, durable relational local database on every target device, complete transactional outbound/inbound handlers for all domains, governed conflict resolution, continuity data plane, fencing/failback, durable alert relay, production PostgreSQL migration/restore validation, complete business modules and commercial release hardening.

These are explicit release blockers for any claim of complete offline-first, high availability or commercial readiness.

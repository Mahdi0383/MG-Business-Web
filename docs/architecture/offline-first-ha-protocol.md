# MG Business — Offline-first and high-availability protocol

**Status:** architecture baseline; implementation is partial.  
**Scope:** customer devices, customer central server (CCS), MG Master service, MG Control Center, licensing, synchronization and connectivity alarms.

## 1. Required topology

- **Customer devices:** every workstation/tablet/phone has its own durable local database and operation journal. It must not require the CCS to open the application or perform its locally supported core workflows.
- **Customer Central Server (CCS):** the customer's authoritative on-premises coordinator while it holds a valid authority lease. It hosts the customer's primary database and serves the local network.
- **MG Master:** central control-plane service for tenant identity, subscription/license status, signed entitlement leases, device/CCS registry, alert relay and (when configured) encrypted customer replication/temporary failover.
- **MG Control Center:** a separate creator-only application. It manages customers, subscriptions, modules, accounts, devices, CCS registrations, releases and support access. It is not a route hidden inside the customer workspace.

MG Master is not the default storage location for all customer data. Customer data remains on the customer's CCS unless the customer explicitly enables an encrypted replication/continuity service.

## 2. Operation modes

### A. CCS reachable on the local network

1. Devices continue to use their local database and append every committed business command to a durable outbox in the same local transaction.
2. Devices synchronize with the CCS when it is reachable.
3. The CCS validates tenant, branch, actor, device, command schema, idempotency key and authority epoch before applying a command.
4. The CCS returns a durable acknowledgement. A device may compact an outbox item only after that acknowledgement.
5. Internet loss does not stop local-network operations. Entitlement is checked against the last signed lease; the configured offline grace is 30 days.

### B. CCS is powered off; devices can still reach one another locally

- Each device continues its supported core workflows against its own local store and outbox.
- No device may silently promote itself to the authoritative CCS.
- On CCS recovery, devices exchange journal ranges and the CCS reconciles them using stable operation IDs, domain-specific conflict rules and idempotent command handlers.
- Conflicts that cannot be resolved deterministically (for example, two offline devices selling the last unit of stock) are held for an explicit manager resolution. Financial records are never silently overwritten.

### C. Devices can reach the internet but cannot reach the CCS

- Devices may contact MG Master for entitlement and health status.
- They may use the MG Master continuity endpoint only after MG Master has granted a signed, time-bounded failover lease with a strictly higher fencing epoch.
- The failover authority is exclusive. CCS writes are rejected while the MG Master lease is active, even if the CCS becomes reachable again.
- The MG Master continuity store accepts the same idempotent journal protocol. It must not become an independent uncoordinated writable copy.

### D. CCS returns after MG Master failover

1. The CCS rejoins as a follower and remains read-only for writes.
2. MG Master verifies tenant identity, lease epoch, sequence ranges, signatures and hashes.
3. Accepted operations are replayed into the CCS through the same domain command handlers.
4. Reconciliation and integrity checks complete; unresolved conflicts are surfaced.
5. MG Master issues a new signed lease with a higher epoch to the CCS.
6. Only after lease activation may the CCS resume authoritative writes. The continuity endpoint becomes read-only and later releases retained replication data according to retention policy.

### E. A device loses all network connectivity

- The device continues locally within the signed offline entitlement lease and its supported local workflows.
- It queues peer-disconnection and sync-failure alerts locally.
- When any trusted path returns, it sends queued alerts to the CCS and/or MG Master; delivery is idempotent and acknowledged.
- The manager UI must distinguish **offline**, **stale heartbeat**, **sync backlog**, **conflict**, and **license grace**. A stale heartbeat is not proof that a device is permanently lost.

## 3. Authority and split-brain prevention

- Every authoritative write requires a signed lease containing tenant ID, authority node ID, lease ID, monotonically increasing fencing epoch, issued/expiry times, allowed modules and offline grace.
- The lease is signed by MG Master. Customer devices and CCS verify it with a pinned public key. The signing private key never ships in the customer application.
- The server rejects commands with a stale fencing epoch. A node cannot become primary based only on a local timeout or an administrator clicking a button.
- Clock rollback is mitigated using signed server time anchors, monotonic elapsed time where available, bounded clock drift and lease renewal. Wall-clock time alone is not a trust source.
- A network partition must never permit two writable authorities. If authority cannot be proven, the system preserves local journals and blocks only operations that require authoritative shared state.

## 4. Command envelope and delivery guarantees

Each command envelope contains:

- tenant, branch, actor and enrolled device identifiers;
- globally unique operation ID (idempotency key);
- command type and schema version;
- authority epoch and optional lease ID;
- client occurrence time and server receipt time;
- canonical payload and SHA-256 integrity hash;
- causal/base version metadata where a domain conflict policy needs it.

Delivery is **at least once**. Exactly-once network delivery is not assumed. The server guarantees idempotent application per tenant + operation ID. Applying the command to business tables and recording its acknowledgement/journal position must happen in one database transaction.

## 5. Domain conflict policy

- Append-only accounting journal entries and audit events are never overwritten.
- Sales, payments, stock movements and game-session settlements use stable IDs and idempotent posting.
- Inventory is reconciled with explicit stock movement history; negative-stock policy is tenant-configurable but never silently corrected.
- Customer profile fields may use field-level last-writer/version rules where safe.
- Concurrent edits to the same invoice, payment, session settlement or accounting period are not auto-merged; they enter a conflict queue.
- Every resolution records the operator, reason, original versions and resulting journal entries.

## 6. Device health and alarms

- Devices send authenticated heartbeats while active. The current prototype uses a 30-second heartbeat and a 90-second stale threshold.
- Alerts are tenant-scoped and deduplicated by device + alert type + incident window.
- The CCS displays alerts to authorized managers. MG Master receives relayed alerts when an internet route exists.
- A disconnected device cannot transmit an alert at the instant of disconnection. It queues the incident locally or the CCS detects the missing heartbeat and creates the alert.
- Recovery sends a resolution event; alert history is retained for audit.
- Current code implements the initial tenant heartbeat endpoint and dashboard polling only. Persistent alert incidents, background monitor, relay to MG Master and push delivery remain unimplemented.

## 7. Licensing and 30-day offline grace

- A customer receives an owner account and subscription entitlement, not a reusable plaintext license key.
- First activation uses a one-time setup claim to bind exactly one CCS identity. Additional users/devices join through tenant invitations and enrollment; ordinary logins never silently claim a new CCS.
- The owner can request a CCS migration through a recovery/transfer workflow with audit.
- MG Master issues signed entitlement leases. The CCS and each device cache the last verified lease and last trusted server-time anchor.
- If MG Master cannot be reached, core local operation may continue for up to 30 days from the last valid lease, subject to the lease's module and device constraints.
- Expiry or suspension blocks new business actions after the grace/next validation boundary, but never deletes or corrupts the local database, journals, backups or exportable customer data.
- Once connectivity returns, the client revalidates subscription, suspension, CCS binding, module entitlement and fencing epoch before resuming.
- Remote suspension cannot instantly reach a device that is fully disconnected; it takes effect on its next trusted validation or lease boundary.

## 8. Security and privacy boundaries

- Customer passwords are one-way hashed by the identity provider and are never readable by creators or support staff.
- MG Control Center has separate creator identities, MFA, strict role separation and audited administrative actions.
- Support access is temporary, customer-visible, least-privilege and audited. Any broader creator access to customer business data must be explicit in contract and product UI.
- Transport uses TLS. Device enrollment uses revocable device credentials; browser profile IDs are not considered trusted credentials.
- Backups are encrypted, tested by restore drills and retained according to customer policy.
- Update packages are signed, staged, health-checked and rollback-capable. Software updates do not override license state or customer data.

## 9. Current implementation status (honest boundary)

Implemented on the feature branch:
- tenant-scoped append-only sync journal and idempotent push/pull transport;
- browser IndexedDB outbound queue and inbound inbox with cursor persistence;
- PWA application-shell caching;
- tenant heartbeat registration and Owner/Manager stale-device polling.

Not yet implemented:
- routing live sales, stock, accounting, cafe and Game Net commands through a transactional local database/outbox;
- applying inbound operations to local domain projections;
- cryptographic device enrollment, lease signing/verification and fencing enforcement;
- CCS runtime/installer, MG Master continuity replication and automatic failover/failback;
- domain conflict engine, persistent alert incident history, alert relay/push;
- MG Control Center, subscription provisioning, module entitlements and one-time CCS claim;
- complete backup/restore, security review, load and chaos testing.

The current sync journal is a transport foundation, not production-ready offline POS or HA replication. Do not deploy it for live financial operations until the missing items and failure tests are complete.

# MG Business — Offline-first, multi-device and failover architecture

Status: Architecture decision baseline  
Date: 2026-11-16

## 1. Goals and boundaries

Every client device must continue permitted core workflows while disconnected from the customer central server and the internet. Each device owns a durable local database and operation journal. The Customer Central Server (CCS) coordinates the tenant when available, but is not a single point of failure.

When clients have internet access and CCS is unavailable, eligible customers may use a tenant-isolated MG Master failover workspace. When CCS returns, it must be reconciled and verified before it resumes primary coordination.

This design does not promise strong consistency during a network partition. Reservations and other scarce shared resources cannot be guaranteed across disconnected partitions. MG Master is not automatically a permanent cloud host for every customer's complete database.

## 2. Node roles

1. Client device: PC, tablet or phone with a durable local database, append-only operation journal and permitted offline workflows.
2. Customer Central Server: customer-operated local coordination node, tenant database, local sync endpoint, device registry and LAN health monitor.
3. MG Master Control Plane: tenant identity, subscriptions, signed license leases, module entitlements, server/device registration, revocation and monitoring.
4. MG Master Failover Data Plane: optional, encrypted, tenant-isolated failover workspace containing only the replicated operational data required by the contracted continuity plan.
5. MG Control Center: separate creator-only application for customers, subscriptions, modules, servers, devices, alerts and releases.

Control plane and failover data plane are separate security boundaries. License validation must not expose business data, and data replication must not grant creator administration rights.

## 3. Connectivity and routing

Each client independently monitors local database health, CCS reachability, MG Master control-plane reachability, failover data-plane reachability and peer reachability where enabled.

Routing rules:
- If CCS is healthy and holds a valid write-authority lease, use it for shared tenant coordination while preserving the local journal.
- If CCS is unreachable, continue eligible workflows against local durable storage without blocking the operator on network timeouts.
- If internet is available and failover is licensed, clients may connect to the tenant's MG Master failover workspace after validating tenant identity, device registration, entitlement and fencing epoch.
- If CCS and MG Master are both unreachable, continue isolated local operation and queue all mutations durably.
- On recovery, exchange journals and acknowledgements. Do not promote CCS back to primary until reconciliation and fencing checks pass.

A LAN outage and an internet outage are distinct incidents. Local operations must never depend on MG Master connectivity.

## 4. Split-brain prevention and write authority

The critical failure case is a partition where CCS and MG Master both believe they are primary. Prevent this with signed leases and fencing epochs, not client preference.

- A write-authority lease identifies tenant, server, epoch, issue/expiry times and permitted role.
- MG Master issues a higher epoch when authorizing failover.
- Every accepted shared write carries the current epoch and unique operation ID.
- Nodes reject stale epochs after observing a newer epoch.
- CCS recovery obtains a fresh lease and reconciles all failover-epoch operations before becoming active.
- Devices unable to validate authority still write to their local journal, but label those operations locally saved/pending rather than globally confirmed.
- Validate lease duration using signed server-time anchors and monotonic elapsed time where possible; wall-clock changes must not extend grace.

If the whole customer site is offline, no remote authority can issue a new epoch. Devices act as independent local writers. Reconciliation is append-only and domain-specific. The UI distinguishes locally saved, pending sync and centrally confirmed states.

## 5. Durable operation journal and sync protocol

Every mutation creates a durable operation envelope before the UI reports local success. Required fields:
- operationId: globally unique UUID/ULID;
- tenantId, branchId, deviceId and actorId;
- aggregate type/id, command type and schema version;
- device occurrence time and server receipt time;
- authority epoch and source node;
- payload and payload hash;
- idempotency, causation and correlation identifiers;
- synchronization state.

Processing:
1. Validate locally and append to the local journal.
2. Apply to the local projection in the same local transaction.
3. Send ordered batches per aggregate with retry/backoff.
4. Receiver verifies tenant/device authorization, schema, fencing and idempotency.
5. Record operation ID, business effect and acknowledgement atomically.
6. Mark only acknowledged operations confirmed; keep failures visible and retryable.
7. Pull remote changes using durable cursors.
8. Use transactional outbox/inbox on server nodes. Browser background sync must never be the only durable queue.

Network delivery is at-least-once; business effects must be idempotent. Do not claim exactly-once network delivery.

## 6. Conflict policy by domain

There is no safe universal last-write-wins policy.
- Accounting: posted journal entries are immutable. Correct using reversing/adjusting entries. Conflicting postings are quarantined for review.
- Sales/payments: operation IDs and payment-provider references prevent duplicate effects. Offline sales may be provisional where shared resources are contested.
- Inventory: movement ledger is append-only. Concurrent outbound movements can create a negative projected balance; preserve both operations, flag shortage and require authorized adjustment. Never silently discard a sale.
- Customer/profile notes: merge only explicitly non-financial fields; sensitive conflicts require review.
- Catalog, prices, tax, roles: centrally versioned configuration. Offline changes become authorized, versioned proposals.
- Game-net sessions: session identity includes device; use monotonic elapsed time with trusted time anchors and flag clock anomalies.
- Reservations and shared scarce resources: show provisional state and confirm only after authoritative coordination.

Retain original operations, actor, source device, resolution, approver and timestamp for every conflict.

## 7. CCS recovery and failback

1. Mark CCS as Recovering; do not immediately accept primary writes.
2. Authenticate server identity and validate license, subscription, device registrations and schema compatibility.
3. Fetch failover and client operations since the last common durable cursor.
4. Verify hashes, tenant boundaries, epochs and idempotency.
5. Replay accepted operations through the same domain handlers and transactions.
6. Produce a reconciliation report for rejected, conflicting or provisional operations.
7. Verify counts, journal balances, stock movement totals and sync cursors.
8. Obtain a new primary lease with a higher epoch and switch routing only after verification.
9. Retain failover data until backup and retention policies confirm safe expiry.

If reconciliation fails, remain in failover or isolated local mode and alert operators. Never overwrite the CCS database with a blind snapshot.

## 8. Device and network health monitoring

Signed heartbeats report device identity, app version, last successful sync cursor, connectivity mode, local database health, queue depth and last observed epoch. Do not include passwords or unnecessary business payloads.

CCS monitors LAN peers and device heartbeats. MG Master monitors CCS and registered devices when reachable. Events:
- Degraded: delayed heartbeat or growing queue.
- Disconnected: heartbeat threshold exceeded.
- ServerUnavailable: CCS health/readiness failed.
- SyncBlocked: authorization, schema or conflict failure.
- Recovered: connection restored and sync resumed.

Alert routing:
- Manager UI/notification through CCS when LAN is available.
- MG Master alert ingestion when internet is available.
- Durable queued alerts on nodes while all paths are down.
- Recovery notice with outage duration, affected devices, queued operations and unresolved conflicts.

Thresholds are customer-configurable. Initial suggested defaults: warning after 60 seconds and disconnected after 3 missed 30-second heartbeats. These are defaults to validate, not fixed guarantees.

## 9. Offline license and suspension

MG Master issues signed tenant/server/device-scoped license leases. CCS distributes short-lived signed device authorization tickets. Maximum offline grace is 30 days from last successful trusted validation, subject to subscription and contract.

Warn before expiry and display last successful validation. If grace expires or tenant is suspended, block new operational actions according to policy but preserve databases, journals, exports and recovery access. A disconnected device cannot receive an immediate suspension command; enforce at next trusted contact or lease expiry. Reconnection revalidates tenant, subscription, server binding, device and modules.

Never delete, corrupt, destructively encrypt or remotely destroy customer data as a licensing mechanism.

## 10. Security and privacy

- TLS on all network links; mutual TLS or device-bound credentials for node authentication.
- Tenant authorization and database isolation at every API boundary.
- Revocable device credentials stored using platform secure storage.
- Passwords are one-way hashed; creator staff can reset but never read them.
- Signed leases and software releases; private signing keys never enter source control.
- Encrypt failover replication and backups at rest with tenant-scoped keys and tested recovery.
- Creator access to business records is disabled by default or explicitly scoped, time-limited, approved and audit-logged.
- Audit role changes, server rebinding, suspension, support access and conflict resolutions.
- Rate limits, replay protection, secret rotation, backup restore tests and security review are release gates.

## 11. Observability

Track per tenant/node: last heartbeat and sync, outbox/inbox depth, oldest pending operation, replication lag, failover duration, active epoch/lease expiry, conflict count, duplicate/rejected operations, database health, disk capacity and backup age. Provide liveness, readiness and sync-readiness endpoints. Do not expose sensitive tenant payloads in public metrics.

## 12. Required failure tests

1. Stop CCS while clients share LAN; local workflows continue.
2. Restore CCS; queued operations sync without duplication.
3. Stop CCS with internet available; failover workspace activates.
4. Restore CCS during failover; stale writes are fenced and failback reconciles.
5. Disconnect one device, several devices, LAN, WAN and both; verify distinct alerts and recovery notices.
6. Restart a client with pending operations; journal survives.
7. Deliver same operation repeatedly/out of order; no duplicate financial effect.
8. Introduce concurrent inventory, accounting, customer and game-session changes; verify domain conflict policy.
9. Move local clock backwards/forwards; offline grace cannot be extended.
10. Suspend/expire tenant while devices are offline; preserve data and enforce on next validation.
11. Upgrade schema while devices are offline; gate incompatible clients safely.
12. Restore CCS and failover backups into clean environments and verify integrity.

## 13. Production gate

Do not call this production-ready until durable journal/outbox/inbox, idempotent APIs, SQLite and PostgreSQL migrations, fencing and failback, domain conflict policies, alert delivery, backup restore drills, security/load tests and operator reconciliation tools are implemented and tested.

# Browser offline journal

The browser client has a durable IndexedDB operation store, a sync agent and a service worker that caches the application shell and same-origin static assets. API, authentication and sync routes are explicitly excluded from service-worker caching.

## Transport and device boundaries

- A stable browser device ID is stored per authenticated actor; another account's queued operations cannot be pushed or counted as the active actor's backlog.
- Commands carry tenant/branch, actor-owned device identity, schema version, authority epoch, operation ID, payload JSON and SHA-256 hash.
- The client currently permits authority epoch zero only. Non-zero epochs fail closed until a signed lease verifier and fencing authority exist.
- Pending operations survive page reloads and browser restarts. Network failures leave the outbox unchanged for retry.
- Push batches are bounded and branch-aware. Tenant-wide commands are drained only for Owner/Manager roles. The server rejects more than one new mutation for the same customer aggregate in a batch; dependent commands must be sent sequentially so each one is checked against the committed base version.
- Pull cursors are independent per tenant/branch. A page that claims more data must advance its cursor; a stalled cursor is rejected before persistence or acknowledgement. Server acknowledgement watermarks are also branch-scoped; a separate null-branch watermark is reserved for manager all-branches visibility. Neither acknowledgement nor receipt cursor proves that a business projection has been applied.
- Tenant-wide inbox reads require explicit manager authority. Branch views contain only their branch and permitted tenant-wide rows.
- Incoming pages are validated as a whole: operation envelope, JSON payload shape, payload size and SHA-256 are checked before the inbox/cursor transaction commits. Persisted payload integrity is rechecked immediately before projection replay.

## Implemented customer vertical slice

Customer is currently the only business aggregate with an end-to-end local-first command workflow:

1. Customer creation writes a tenant-scoped local projection and branch-originated outbox command in one IndexedDB transaction.
2. Customer contact updates require a positive baseVersion, enforce one pending edit per customer, and atomically update the local projection with its outbox command.
3. The server validates tenant, actor, device, branch, role, schema and payload identity. Customer updates use database-enforced optimistic concurrency; stale versions return conflict without overwriting newer data.
4. Server replay and append-only journal insertion are committed together. Exact retries are idempotent; reusing an operation ID with a changed envelope is rejected.
5. Other devices apply customer create/update projections and inbox acknowledgement atomically. Origin-device echoes are not applied twice. Stale versions and invalid identity transitions are retained as durable conflicts.
6. Local acknowledgement verifies the pending customer baseVersion again inside the IndexedDB transaction; a version drift aborts acknowledgement instead of silently marking an unapplied edit as synced.
7. Authoritative online customer reads are cached for offline browsing. Cache refreshes never overwrite pending edits or downgrade a newer local confirmed version. Cache persistence is best-effort so browser storage failure does not break the online customer view.

The workspace surfaces the customer conflict count and provides a role-restricted, read-only conflict review page for Owner, Manager and Accountant roles. It shows branch-scoped unresolved conflicts and their recorded decision details; it does not resolve, discard or replay a conflict. A governed operator resolution workflow with explicit field-level reconciliation and audit history is still required. The current conflict policy remains fail-closed for unknown commands.

## Not yet offline-ready

Sales/POS, payments, purchasing, inventory, accounting, café orders and kitchen tickets, Game Net sessions, membership, licensing and configuration do not yet have complete local command handlers, transactional projections, server replay, inbound application and domain-specific conflict resolution. Generic HTTP POST requests are deliberately not queued automatically.

The platform still needs cryptographic device enrollment, signed authority leases and fencing, local durable databases on every target device, conflict review/resolution workflows, peer health monitoring independent of an open browser session, alert relay, CCS/MG Master failover/failback and multi-node partition/recovery tests.

Do not interpret the existence of this module or the customer vertical slice as a complete offline mode or production replication protocol.


## Cryptographic device request authentication

- Browser enrollment creates a Web Crypto ECDSA P-256 key pair. The private key is non-extractable and persisted as a structured-clone CryptoKey in IndexedDB; the server stores the public SPKI key and only a SHA-256 digest of the one-time-returned device credential.
- Enrollment challenges are random, short-lived, single-use and bound to the authenticated tenant, actor, branch context and device ID. A proof-of-possession signature is required to complete enrollment.
- Authenticated sync heartbeat, pull, push and acknowledgement requests are signed over method, path/query, tenant, branch, actor, device, key version, timestamp, UUID nonce and exact serialized body SHA-256. The server checks credential digest, active status, actor scope, key version, ECDSA signature and a two-minute timestamp window, then persists the nonce under a unique key before executing the endpoint.
- Device credentials are tenant-scoped so one browser identity can be used in the tenant's authorized branches; enrollment proof itself remains branch-context-bound. A future branch-restricted credential can set an explicit branch scope.
- Production startup now fails unless `SyncSecurity:RequireDeviceSignatures=true`. Enrollment bootstrap routes are excluded from request-signature middleware but still require authenticated tenant membership and challenge proof.
- This closes device request authentication and replay protection at the API boundary; key rotation/revocation APIs, expired nonce cleanup, browser key recovery, signed authority leases/fencing and end-to-end multi-node recovery tests remain open release gates.

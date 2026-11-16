# Browser offline journal

The browser client now has a durable IndexedDB operation store, a push agent and a production service worker that caches the application shell and same-origin static assets. API/auth/sync routes are explicitly excluded from caching.

- A stable browser device ID is created and persisted per authenticated actor in IndexedDB, so switching accounts in the same browser cannot reuse a device registration owned by another user. Offline enqueue also requires that actor identity and stamps the corresponding actor-owned device ID into the durable operation envelope; it never creates an anonymous device registration.
- Business commands can be explicitly appended with tenant/branch, actor-independent device identity, schema version, authority epoch, payload JSON and SHA-256 hash.
- The client currently permits only authority epoch zero. Non-zero epochs are rejected locally and by the API until a signed lease verifier and fencing authority are deployed; a client cannot promote itself by choosing an epoch.
- Pending operations survive page reloads and browser restarts.
- The sync agent sends bounded batches to the authenticated tenant sync API and marks records acknowledged only after a complete, matching server acknowledgement.
- Network failures leave the local queue untouched for a later retry.

## Branch-isolated journal cursors

The server scans the tenant journal in sequence order but returns only tenant-wide and active-branch operations to a branch-scoped member. Its scan cursor can advance past rows hidden by branch permissions; those rows are not included in the response. The browser therefore stores a separate pull cursor for each tenant/branch pair. This is required so switching from one branch to another and later returning does not skip that branch's older operations. Server acknowledgement cursors remain monotonic telemetry and do not replace the browser's branch-specific pull cursors.

Push batches are selected from the active branch queue in global FIFO order; commands from other branches are skipped rather than blocking the selected branch. Tenant-wide queued commands are drained only for Owner/Manager roles. Sync HTTP requests carry an explicit tenant/branch context captured by the sync cycle, preventing a branch switch during an in-flight request from storing another branch's response under the wrong cursor.

Before any incoming page is committed, the sync agent validates each operation envelope, parses its payload as a JSON object, enforces the payload size bound, and recomputes SHA-256 over the exact UTF-8 payload bytes. A malformed or tampered operation rejects the entire page; the local inbox cursor and device acknowledgement are left unchanged.

A non-manager session with no selected branch has no cross-branch outbox or inbox view. The API also rejects branch-targeted writes from that tenant-wide context; cross-branch writes require an Owner or Manager context, while ordinary members must operate within their selected branch.

Pending outbox reads are additionally filtered to the authenticated browser device ID. If another account has unsynced operations in the same browser profile, those rows remain queued for that account's next login and cannot block or be submitted by the current actor.

Incoming inbox reads and replay batches are branch-scoped: a branch receives its own operations plus tenant-wide operations, while the tenant-wide view can inspect all branches. Repeated delivery of a shared tenant-wide sequence preserves its applied/conflict state only when the immutable operation envelope matches; a reused sequence with different content aborts the local transaction rather than silently discarding data.

## Integration boundary

This is infrastructure only. Existing demo screens and current live business forms are not yet routed through this journal, and generic POST requests are deliberately not auto-queued. Each business workflow must be converted to an explicit local command handler that validates and applies the local projection in the same transaction as enqueueing the operation.

The current server pull endpoint exposes an append-only operation stream. Applying inbound commands to local domain projections, per-device pull cursors, cryptographic device enrollment, signed leases/fencing, conflict review, service-worker lifecycle and end-to-end multi-node failover are subsequent work. Do not interpret the presence of this module as a complete offline mode.

## Conflict classification foundation

`conflict-policy.js` provides a deliberately fail-closed classifier for incoming operation types:
- accounting, sales, payment, inventory, purchase, cafe-order and game-session operations are routed to manual review;
- customer profile/contact updates are eligible for application only when their explicit base version matches the current version;
- unknown operation types and missing version preconditions are never auto-applied.

This classifier is an isolated policy primitive with unit tests. It is not yet wired to an inbound projection applier or business forms, so no incoming business operation is automatically applied by this change.

## Incoming operation processor

`incoming-operation-processor.js` reads a bounded ordered inbox batch, obtains the current conflict state, invokes the fail-closed classifier, and routes eligible commands through an adapter contract. The adapter's `applyAndAcknowledge` method is required to atomically commit the local projection mutation and inbox acknowledgement. Conflicts are recorded separately and are never silently discarded.

The processor validates and sorts each batch by journal sequence and stops at the first unresolved conflict so later commands cannot be applied across a missing prerequisite. Conflict recording must be idempotent by tenant and sequence. The processor is infrastructure scaffolding only: no production business projection adapter is registered yet, so the current application does not automatically apply inbound sales, accounting, inventory, cafe or game-net commands. The adapter contract must be implemented against the chosen durable local database before enabling this in production.

Pending-operation dashboard counts use the same authenticated device boundary as push selection. Other accounts' queued rows remain stored but are not included in the active actor's pending count.

Tenant-wide incoming inbox reads and replay are fail-closed: callers must explicitly pass manager authority when no branch is selected. Branch-scoped reads include only the selected branch and tenant-wide rows; the processor independently requires the same explicit authority before requesting an unscoped batch.

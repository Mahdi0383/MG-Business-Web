# Browser offline journal

The browser client now has a durable IndexedDB operation store, a push agent and a production service worker that caches the application shell and same-origin static assets. API/auth/sync routes are explicitly excluded from caching.

- A stable browser profile device ID is created and persisted in IndexedDB.
- Business commands can be explicitly appended with tenant/branch, actor-independent device identity, schema version, authority epoch, payload JSON and SHA-256 hash.
- The client currently permits only authority epoch zero. Non-zero epochs are rejected locally and by the API until a signed lease verifier and fencing authority are deployed; a client cannot promote itself by choosing an epoch.
- Pending operations survive page reloads and browser restarts.
- The sync agent sends bounded batches to the authenticated tenant sync API and marks records acknowledged only after a complete, matching server acknowledgement.
- Network failures leave the local queue untouched for a later retry.

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

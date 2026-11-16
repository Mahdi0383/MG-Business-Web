# Browser offline journal

The browser client now has a durable IndexedDB operation store, a push agent and a production service worker that caches the application shell and same-origin static assets. API/auth/sync routes are explicitly excluded from caching.

- A stable browser profile device ID is created and persisted in IndexedDB.
- Business commands can be explicitly appended with tenant/branch, actor-independent device identity, schema version, authority epoch, payload JSON and SHA-256 hash.
- Pending operations survive page reloads and browser restarts.
- The sync agent sends bounded batches to the authenticated tenant sync API and marks records acknowledged only after a complete, matching server acknowledgement.
- Network failures leave the local queue untouched for a later retry.

## Integration boundary

This is infrastructure only. Existing demo screens and current live business forms are not yet routed through this journal, and generic POST requests are deliberately not auto-queued. Each business workflow must be converted to an explicit local command handler that validates and applies the local projection in the same transaction as enqueueing the operation.

The current server pull endpoint exposes an append-only operation stream. Applying inbound commands to local domain projections, per-device pull cursors, device enrollment/cryptographic identity, leases/fencing, conflict review, service-worker lifecycle and end-to-end multi-node failover are subsequent work. Do not interpret the presence of this module as a complete offline mode.

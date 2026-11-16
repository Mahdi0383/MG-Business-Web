# ADR-008 — Customer optimistic concurrency and offline edit contract

- Status: Accepted as the implementation contract; implementation is pending.
- Date: 2026-11-16
- Scope: Customer profile/contact edits across browser devices, the tenant API and the sync journal.

## Context

The current customer API supports an online PUT that updates contact fields without an expected version. The browser inbound processor supports customer creation only; customer update command types deliberately remain manual-review. Enabling offline edits before both sides share a concurrency contract would permit last-writer-wins data loss and ambiguous replay.

## Decision

Customer edits use an integer aggregate version scoped to the tenant/customer pair. The server is authoritative for advancing the version. A newly created customer starts at version 1. Every accepted state-changing update advances it by exactly one. No-op updates do not advance the version.

Every update command carries:
- `customerId`: stable customer ID;
- `baseVersion`: positive integer version the editor actually read;
- `operationId`: stable idempotency identity, reused for every retry;
- `schemaVersion`: explicit command schema version;
- `branchId`, `actorId`, `deviceId`, `occurredAtUtc`: authenticated/auditable origin context;
- `payload`: only the permitted changed customer fields.

The server must atomically persist the customer mutation, incremented version, sync journal row and idempotency outcome. The update predicate must enforce the expected version at the database boundary (EF concurrency token or equivalent conditional update), not merely compare a value read earlier in application code. A stale base version returns a stable 409 conflict and must not mutate the customer or append a successful update event.

Idempotency is checked before applying the business mutation. Repeating the same operation ID with the same canonical envelope returns the original durable result; reusing it with different content or ownership is rejected. The same operation ID must never create a second business effect.

The API response and all customer projections expose the canonical version. The browser local projection records both the server-confirmed version and any local pending edit state. A local edit and its outbox command are committed in one IndexedDB transaction. The command's baseVersion is the last known server version; the local projection must not pretend that a pending edit is server-confirmed.

Inbound update application is atomic with inbox acknowledgement and is idempotent. The originating device's echo is recognized by operation ID and acknowledged without reapplying its own local mutation. Other devices apply only when their current server-confirmed projection version equals the command's baseVersion; otherwise they persist a durable branch-scoped conflict and stop ordered replay pending governed reconciliation.

## Merge and field policy

No implicit last-writer-wins merge is allowed. Initial release treats a stale whole-profile update as a conflict. A later field-level merge may be introduced only for explicitly enumerated non-sensitive fields, with per-field base values/version evidence and deterministic tests. Code, account balances, sales, payment, tax, inventory and other financial or scarce-resource facts are never merged through this customer profile command.

## Required implementation sequence

1. Add a non-null customer version column with a safe migration default and initialize existing records to version 1.
2. Include version in customer API responses and local customer projections.
3. Require baseVersion on the online update API; use a database-enforced concurrency token/conditional update and map stale writes to 409.
4. Add server-side update-command replay with atomic journal/idempotency/business mutation.
5. Add atomic local edit-plus-outbox enqueue with the last confirmed baseVersion.
6. Add inbound projection update, origin echo handling and durable stale-version conflict recording.
7. Keep update commands fail-closed until all preceding steps are implemented and the release tests pass.

## Mandatory verification

- Two devices edit version N; first commit succeeds and second receives a durable conflict without overwriting the first.
- Same operation ID and same envelope retried before/after response loss creates one business effect and returns the same result.
- Same operation ID with changed payload, actor, device or branch is rejected.
- Origin-device echo acknowledges without double-applying or creating a self-conflict.
- Other-device inbound update applies only at the expected base version.
- Stale inbound baseVersion records a durable conflict and blocks later ordered operations.
- Crash/failure between local projection and outbox writes leaves neither partially committed.
- Crash/failure between server customer mutation and journal insert leaves neither partially committed.
- Invalid input, unauthorized tenant/branch and unsupported schema do not mutate data.
- SQLite and PostgreSQL migration/concurrency behavior are both verified.

## Consequences

This contract intentionally delays offline customer editing until the complete path exists. It prevents silent data loss and makes conflict behavior observable and testable. It does not claim that customer updates are currently implemented.

# ADR: Cryptographic device enrollment, request authentication and authority leases

Status: **Implemented foundation; production release gate remains open**

Date: 2026-11-17

## Implemented device enrollment

The browser no longer treats a UUID as proof of device identity. The current protocol uses Web Crypto ECDSA P-256 with SHA-256:

1. An authenticated tenant member requests `POST /api/sync/enrollment/challenges`. The server derives tenant, actor and optional branch from authenticated context, creates a random 256-bit challenge and persists only its SHA-256 digest. Challenges expire within five minutes and are single-use.
2. The browser creates a non-extractable P-256 private CryptoKey, stores it in IndexedDB, and sends its SPKI public key plus a proof signature to `POST /api/sync/enrollment/complete`.
3. The server validates canonical P-256 SPKI, verifies the proof, consumes the challenge transactionally and issues a random 256-bit credential. Only the SHA-256 digest of that credential is persisted; the raw credential is returned once.
4. Credentials are bound to tenant, actor, branch and device. Browser device IDs and key records are isolated by tenant/branch/actor scope.
5. Recovery with the same enrolled key rotates the opaque credential and increments the key version. Explicit key rotation is available through `POST /api/sync/enrollment/rotate`; the new public key must prove possession against a fresh challenge.
6. Authenticated self-service/Owner/Manager revocation is available through `POST /api/sync/enrollment/revoke`. Revocation also revokes leases held by that device.

This is software key protection, not hardware attestation. Same-origin script compromise can still invoke a non-extractable key. Browser-profile cloning and compromised endpoints remain in the threat model.

## Enrollment and rotation canonical messages

UTF-8, newline-delimited fields are signed with ECDSA P-256 / SHA-256. Signatures use the 64-byte IEEE P1363 representation.

```text
MG-DEVICE-ENROLLMENT-V1
{tenantId}
{branchId-or-empty}
{actorId}
{deviceId}
{challengeId}
{challengeBase64}
```

Rotation uses the same field order with domain separator `MG-DEVICE-ROTATION-V1`.

## Signed request authentication

When `SyncSecurity:RequireDeviceSignatures=true`, middleware verifies every `/api/sync/*` request except the enrollment lifecycle routes. The browser's `MgApiClient` automatically attaches:

- `X-MG-Device-Id`
- `X-MG-Device-Key-Version`
- `X-MG-Device-Credential`
- `X-MG-Device-Timestamp` (Unix milliseconds)
- `X-MG-Device-Nonce` (UUID)
- `X-MG-Device-Signature` (Base64 P1363 signature)

The canonical request is:

```text
MG-DEVICE-REQUEST-V1
{HTTP_METHOD_UPPERCASE}
{PATH_BASE + PATH + QUERY}
{tenantId}
{branchId-or-empty}
{actorId}
{deviceId}
{keyVersion}
{unixTimestampMilliseconds}
{nonce}
{sha256(exact UTF-8 request body bytes)}
```

The server independently resolves authenticated tenant/actor/branch context; validates active credential, branch, actor and key version; enforces a two-minute clock-skew window; verifies body hash and ECDSA signature; and inserts a unique tenant/device/nonce replay record before dispatching the request. Duplicate nonces and invalid credentials fail closed. Request idempotency keys remain independently required for business mutations.

## Signed authority lease and fencing

A separate `DeviceAuthorityLease` aggregate and persistence table now provide tenant/branch scoped authority state.

- `POST /api/sync/authority-lease` acquires or renews a short-lived (two-minute) lease. Acquisition is serialized using a serializable database transaction.
- Only Owner/Manager can acquire; authenticated tenant members can read the current signed lease using `GET /api/sync/authority-lease`.
- The issuer signs a canonical lease using ECDSA P-256 / SHA-256. Lease fields include tenant, branch, scope, device, lease ID, monotonic epoch, issued-at and expiry.
- An expired/revoked lease advances the fencing epoch. A competing device cannot take over an unexpired lease.
- When `SyncSecurity:RequireAuthorityLeases=true`, push rejects zero/stale epochs, wrong device, wrong branch, revoked leases and operations outside the lease validity window.
- Browser verification uses a pinned SPKI key from `VITE_AUTHORITY_LEASE_PUBLIC_KEY_SPKI_BASE64`; offline commands obtain the epoch only after re-verifying the signature, device/scope binding and expiry from IndexedDB.
- Credential rotation/revocation invalidates the device's active authority leases.

## Required production configuration

Production startup fails closed unless all of the following are configured:

- `SyncSecurity:RequireDeviceSignatures=true`
- `SyncSecurity:RequireAuthorityLeases=true`
- `SyncSecurity:AuthoritySigningPrivateKeyPkcs8Base64` — a securely provisioned ECDSA P-256 PKCS#8 private key
- `VITE_REQUIRE_AUTHORITY_LEASES=true`
- `VITE_AUTHORITY_LEASE_PUBLIC_KEY_SPKI_BASE64` — the matching public SPKI key pinned in the built client
- exact HTTPS CORS origins and trusted reverse-proxy IP addresses

Never commit private keys, credentials, or production secrets. Define an operational signing-key rotation procedure before live use; the current signer uses one configured key and does not yet implement multi-key key-ID overlap.

## Remaining release blockers

- Add PostgreSQL concurrency/race tests for simultaneous lease acquisition and nonce replay, beyond the existing integration coverage.
- Add an explicit signing-key rotation/key-ID overlap protocol and operational secret rotation runbook.
- Add lease renewal/recovery and stale-operation reconciliation UX.
- Complete transactional offline command handlers for inventory, sales, accounting, café and GameNet; currently Customer is the end-to-end local-first business slice.
- Add multi-node partition, clock-skew, stale-leader, restart and disaster-recovery tests.
- Complete security review/penetration testing and production deployment configuration validation.

The implemented foundation does not mean the overall MG Business Platform is production-ready.

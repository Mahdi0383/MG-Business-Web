# ADR: Cryptographic device enrollment and request authentication

Status: **Proposed — not implemented; release blocker**

Date: 2026-11-17

## Decision

Replace browser-generated device UUID trust with a server-issued enrollment lifecycle. A UUID is a label only and MUST NOT authorize heartbeat, journal push/pull, acknowledgement, or authority-lease use.

The initial browser implementation uses Web Crypto ECDSA P-256 with SHA-256. The browser generates a non-extractable signing private key and exports only the public JWK. This proves possession of the enrolled key, but it is not hardware attestation: same-origin script compromise can still invoke the key, and browser-profile cloning may copy usable key material through platform mechanisms. Do not describe it as hardware-bound identity.

## Enrollment state machine

PendingChallenge -> Active -> Rotating -> Active; terminal states are Revoked and Expired.

1. An authenticated TenantMember requests an enrollment challenge. The server derives tenant, actor, and branch from the authenticated context; client-supplied tenant/actor values are ignored.
2. Server creates a cryptographically random 256-bit challenge, stores only its SHA-256 digest, binds it to tenant/actor/branch, device label, creation/expiry times (maximum five minutes), and a unique challenge ID. Enforce per-actor and per-IP rate limits.
3. Browser generates an ECDSA P-256 key pair with extractable=false for the private key, stores the private CryptoKey in IndexedDB, and submits the public JWK plus challenge ID.
4. Browser signs the UTF-8 canonical enrollment message defined below. Server validates JWK curve/key type/coordinates, rejects private JWK members, verifies the signature, and atomically consumes the unexpired challenge exactly once.
5. Server issues a random 256-bit opaque credential. Persist only a keyed HMAC-SHA-256 digest (server-held pepper) of the credential, never the raw credential. Return the raw credential once; browser stores it in IndexedDB. Bind credential to device, tenant, actor, and current key version.
6. Revocation, credential rotation, actor removal, tenant suspension, and device reset invalidate credentials immediately. Rotation requires proof from the current key and a fresh challenge; old credentials are revoked in the same transaction that activates the replacement.

Enrollment is not silently performed by /heartbeat. Existing un-enrolled browser IDs must receive an explicit DEVICE_ENROLLMENT_REQUIRED response and remain unable to sync until enrolled.

## Canonical signing

Use UTF-8 JSON canonicalization (RFC 8785/JCS) and reject duplicate JSON object keys before canonicalization. Sign a domain-separated string with newline separators:

    MG-SYNC-REQUEST-V1
    {HTTP_METHOD}
    {NORMALIZED_PATH_AND_QUERY}
    {tenantId}
    {branchId-or-empty}
    {deviceId}
    {keyVersion}
    {unixTimestampSeconds}
    {requestNonce}
    {sha256(canonical_request_body_bytes)}

Transmit credential, key version, timestamp, nonce, and base64url signature in dedicated headers. The server:

- authenticates the bearer user and resolves tenant/branch independently;
- verifies credential digest, active status, actor/tenant/branch binding, and key version;
- enforces a small timestamp skew window;
- verifies ECDSA signature against the stored public key;
- atomically inserts a unique (deviceId, requestNonce) replay record with expiry;
- verifies the body hash over the exact canonical bytes;
- rejects replay, stale key versions, revoked credentials, and mismatched context before any business mutation.

Nonce consumption and business mutation must share a transaction where possible. If the request is rejected after nonce consumption, the client retries with a new nonce while retaining the same operation idempotency IDs. Request authentication is not a replacement for per-command idempotency.

## Authority leases and fencing

Device authentication alone does not grant write authority. A separate lease authority signs short-lived leases containing tenant ID, branch ID, device ID, monotonically increasing epoch, lease ID, issued-at, not-before, expires-at, and permitted command scopes.

- Lease issuance is serialized per tenant/branch; epoch increments atomically and never decreases.
- Lease signatures use a server-held asymmetric signing key; public verification keys are distributed to clients with key IDs and a rotation overlap window.
- Every authoritative write validates signature, tenant/branch/device binding, expiry, scope, and current fencing epoch in the same database transaction as the mutation.
- A higher epoch fences every lower epoch immediately. A lease expiry alone never permits two writers: promotion requires a committed epoch increment by the authoritative coordinator.
- Offline clients may queue commands but cannot claim server authority. They reconcile after reconnect; financial and stock commands remain fail-closed until command handlers and conflict rules are complete.
- Clock skew is bounded; leases are short-lived. The server clock is authoritative.

## Required persistence and constraints

Introduce separate records rather than overloading DevicePresence:

- DeviceEnrollmentChallenge: challenge digest, challenge ID, tenant/actor/optional branch, expiry, consumed timestamp, attempt count.
- DeviceCredential: tenant/device/actor, public JWK or normalized public coordinates, key version, credential digest, created/rotated/revoked timestamps, status, last-used timestamp.
- DeviceRequestNonce: device, nonce digest, expiry; unique device+nonce constraint and bounded cleanup.
- AuthorityLease: tenant/branch, device, lease ID, epoch, scope, issued/expiry, signature/key ID, revoked timestamp; unique tenant+branch+epoch and tenant+branch+lease ID.
- AuthorityEpoch: tenant/branch and current epoch with concurrency token.

Use tenant-qualified foreign keys and unique constraints. Avoid logging raw credentials, challenge secrets, signatures containing personal payloads, or private key material. Retain audit events for enrollment, rotation, revocation, lease issue, and fencing.

## Required adversarial tests

- Challenge expires, is consumed twice, belongs to another actor/tenant/branch, or is guessed/replayed.
- Invalid curve, malformed JWK, private-key fields, invalid signature, wrong domain separator, changed method/path/query/body/context, stale timestamp, duplicate nonce.
- Credential revoked, rotated, wrong key version, credential copied to another tenant/device, actor membership disabled.
- Concurrent enrollment consumption and concurrent key rotation.
- Heartbeat cannot auto-enroll; every sync route rejects an un-enrolled or revoked device.
- Two simultaneous lease issuances serialize; old epoch is rejected after promotion; expired, wrong-scope, wrong-branch, forged, and revoked leases fail closed.
- Process crash between nonce reservation and command commit cannot create an accepted command without a durable idempotency outcome.
- Browser IndexedDB loss requires re-enrollment; it must never silently fall back to UUID trust.

## Rollout and release gates

1. Add schema/entities/migrations and security-focused domain tests.
2. Add challenge/complete/rotate/revoke endpoints and rate limits.
3. Add browser key generation/storage and signed-request middleware; migrate UI to enrollment-required states.
4. Require signatures on heartbeat, push, pull, and acknowledgement. Do not keep an insecure compatibility bypass in production.
5. Implement signed leases and transactionally enforced fencing.
6. Run SQLite and PostgreSQL integration suites, multi-instance concurrency tests, key rotation/revocation tests, and failover/split-brain chaos tests.
7. Update operator docs and recovery procedures; only then remove the release blocker.

Until all gates pass, this ADR is a design contract only. The current browser UUID heartbeat implementation remains explicitly untrusted and this branch is not production-ready.

const DATABASE_NAME = "mg-device-enrollment";
const DATABASE_VERSION = 1;
const STORE_NAME = "credentials";

function openCredentialDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE_NAME))
        request.result.createObjectStore(STORE_NAME, { keyPath: "scope" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("Unable to open device credential storage."));
  });
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("Device credential storage request failed."));
  });
}

function transactionDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error || new Error("Device credential transaction failed."));
    transaction.onabort = () => reject(transaction.error || new Error("Device credential transaction was aborted."));
  });
}

function toBase64(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function fromBase64(value) {
  return Uint8Array.from(atob(value), character => character.charCodeAt(0));
}

function hex(bytes) {
  return [...bytes].map(value => value.toString(16).padStart(2, "0")).join("");
}

async function sha256(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return hex(new Uint8Array(digest));
}

export class DeviceEnrollmentClient {
  constructor(apiClient, operationStore) {
    this.apiClient = apiClient;
    this.operationStore = operationStore;
    this.enrollmentInFlight = new Map();
  }

  scope(tenantId, actorId) {
    return `${tenantId}:${actorId}`;
  }

  async readCredential(scope) {
    const database = await openCredentialDatabase();
    const transaction = database.transaction(STORE_NAME, "readonly");
    const done = transactionDone(transaction);
    const record = await requestResult(transaction.objectStore(STORE_NAME).get(scope));
    await done;
    database.close();
    return record || null;
  }

  async writeCredential(record) {
    const database = await openCredentialDatabase();
    const transaction = database.transaction(STORE_NAME, "readwrite");
    const done = transactionDone(transaction);
    transaction.objectStore(STORE_NAME).put(record);
    await done;
    database.close();
  }

  async ensureEnrolled({ tenantId, branchId = null, actorId }) {
    if (typeof tenantId !== "string" || !tenantId.trim()
      || typeof actorId !== "string" || !actorId.trim())
      throw new TypeError("Tenant and actor identity are required for device enrollment.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("Branch identity must be a non-empty string or null.");
    if (!globalThis.crypto?.subtle || !globalThis.indexedDB)
      throw new Error("Secure browser cryptography and IndexedDB are required for device enrollment.");

    const scope = this.scope(tenantId, actorId);
    const existing = await this.readCredential(scope);
    if (existing?.privateKey && existing?.credential && existing?.deviceId)
      return existing;
    if (this.enrollmentInFlight.has(scope))
      return this.enrollmentInFlight.get(scope);

    const enrollment = this.enroll({ tenantId, branchId, actorId, scope, existing });
    this.enrollmentInFlight.set(scope, enrollment);
    try {
      return await enrollment;
    } finally {
      this.enrollmentInFlight.delete(scope);
    }
  }

  async enroll({ tenantId, branchId, actorId, scope, existing }) {
    const deviceId = await this.operationStore.getDeviceId(actorId);
    let privateKey = existing?.privateKey || null;
    let publicKeySpkiBase64 = existing?.publicKeySpkiBase64 || null;
    if (!privateKey || !publicKeySpkiBase64) {
      const pair = await crypto.subtle.generateKey(
        { name: "ECDSA", namedCurve: "P-256" },
        false,
        ["sign", "verify"]
      );
      privateKey = pair.privateKey;
      const publicKeyBytes = new Uint8Array(await crypto.subtle.exportKey("spki", pair.publicKey));
      publicKeySpkiBase64 = toBase64(publicKeyBytes);
    }

    // Persist the non-extractable key before the server can commit enrollment.
    // A lost completion response can then be recovered with the same proof key.
    const pendingRecord = {
      scope,
      tenantId,
      actorId,
      deviceId,
      privateKey,
      publicKeySpkiBase64,
      credential: null,
      keyVersion: existing?.keyVersion || 0,
      enrolledAtUtc: existing?.enrolledAtUtc || null
    };
    await this.writeCredential(pendingRecord);
    const challengeResponse = await this.apiClient.post(
      "/api/sync/enrollment/challenges",
      { deviceId },
      { tenantId, branchId }
    );
    if (!challengeResponse?.challengeId || typeof challengeResponse.challenge !== "string"
      || challengeResponse.deviceId !== deviceId || challengeResponse.actorId !== actorId)
      throw new Error("MG server returned an invalid device enrollment challenge.");

    const branchText = branchId || "";
    const canonical = [
      "MG-DEVICE-ENROLLMENT-V1",
      tenantId,
      branchText,
      actorId,
      deviceId,
      challengeResponse.challengeId,
      challengeResponse.challenge
    ].join("\n");
    const signature = await crypto.subtle.sign(
      { name: "ECDSA", hash: "SHA-256" },
      privateKey,
      new TextEncoder().encode(canonical)
    );

    const completed = await this.apiClient.post(
      "/api/sync/enrollment/complete",
      {
        challengeId: challengeResponse.challengeId,
        deviceId,
        challenge: challengeResponse.challenge,
        publicKeySpkiBase64,
        signatureBase64: toBase64(new Uint8Array(signature))
      },
      { tenantId, branchId }
    );
    if (completed?.deviceId !== deviceId || !Number.isInteger(completed?.keyVersion)
      || completed.keyVersion < 1 || typeof completed?.credential !== "string")
      throw new Error("MG server returned an invalid device enrollment credential.");

    const record = {
      ...pendingRecord,
      credential: completed.credential,
      keyVersion: completed.keyVersion,
      enrolledAtUtc: completed.enrolledAtUtc
    };
    await this.writeCredential(record);
    return record;
  }

  async ensureAuthorityLease({ tenantId, branchId = null, actorId, canAcquire = false }) {
    const record = await this.ensureEnrolled({ tenantId, branchId, actorId });
    const now = Date.now();
    const cached = record.authorityLease;
    if (cached?.verified === true && cached.tenantId === tenantId && cached.deviceId === record.deviceId
      && (cached.branchId || null) === (branchId || null)
      && Date.parse(cached.expiresAtUtc) > now + 30_000) {
      await this.operationStore.setAuthorityLease({ tenantId, branchId, actorId, lease: cached });
      return cached;
    }

    let response;
    if (canAcquire) {
      try {
        response = await this.apiClient.post("/api/sync/authority-lease", {}, { tenantId, branchId, actorId });
      } catch {
        response = await this.apiClient.get("/api/sync/authority-lease", { tenantId, branchId, actorId });
      }
    } else {
      response = await this.apiClient.get("/api/sync/authority-lease", { tenantId, branchId, actorId });
    }

    const pinnedPublicKey = import.meta.env?.VITE_AUTHORITY_LEASE_PUBLIC_KEY_SPKI_BASE64;
    if (!pinnedPublicKey || response?.publicKeySpkiBase64 !== pinnedPublicKey)
      throw new Error("The authority lease signing key does not match the configured trusted public key.");
    if (response.tenantId !== tenantId || (response.branchId || null) !== (branchId || null)
      || response.deviceId !== record.deviceId
      || !Number.isSafeInteger(response.epoch) || response.epoch < 1
      || typeof response.leaseId !== "string" || typeof response.deviceId !== "string"
      || typeof response.scopeKey !== "string" || typeof response.signatureBase64 !== "string")
      throw new Error("MG server returned an invalid authority lease.");

    const expectedScope = branchId ? branchId.replaceAll("-", "").toLowerCase() : "tenant-wide";
    if (response.scopeKey !== expectedScope)
      throw new Error("The authority lease belongs to a different branch scope.");

    const issuedAt = Date.parse(response.issuedAtUtc);
    const expiresAt = Date.parse(response.expiresAtUtc);
    if (!Number.isFinite(issuedAt) || !Number.isFinite(expiresAt)
      || issuedAt > now + 30_000 || expiresAt <= now || expiresAt <= issuedAt
      || expiresAt - issuedAt > 5 * 60_000)
      throw new Error("The authority lease is expired or has an invalid validity window.");

    const publicKey = await crypto.subtle.importKey(
      "spki",
      fromBase64(pinnedPublicKey),
      { name: "ECDSA", namedCurve: "P-256" },
      false,
      ["verify"]
    );
    const canonical = [
      "MG-AUTHORITY-LEASE-V1",
      response.tenantId,
      response.branchId || "",
      response.scopeKey,
      response.deviceId,
      response.leaseId,
      String(response.epoch),
      String(issuedAt),
      String(expiresAt)
    ].join("\n");
    const verified = await crypto.subtle.verify(
      { name: "ECDSA", hash: "SHA-256" },
      publicKey,
      fromBase64(response.signatureBase64),
      new TextEncoder().encode(canonical)
    );
    if (!verified)
      throw new Error("The authority lease signature could not be verified.");

    const lease = { ...response, verified: true };
    await this.writeCredential({ ...record, authorityLease: lease });
    await this.operationStore.setAuthorityLease({ tenantId, branchId, actorId, lease });
    return lease;
  }

  async createRequestHeaders({ method, path, tenantId, branchId = null, actorId, bodyJson = "" }) {
    if (typeof path !== "string" || !path.startsWith("/api/sync/"))
      throw new TypeError("A sync API path is required for device request signing.");
    const record = await this.ensureEnrolled({ tenantId, branchId, actorId });
    const timestamp = String(Date.now());
    const nonce = crypto.randomUUID().toLowerCase();
    const bodyHash = await sha256(bodyJson);
    const canonical = [
      "MG-DEVICE-REQUEST-V1",
      String(method || "GET").toUpperCase(),
      path,
      tenantId,
      branchId || "",
      actorId,
      record.deviceId,
      String(record.keyVersion),
      timestamp,
      nonce,
      bodyHash
    ].join("\n");
    const signature = await crypto.subtle.sign(
      { name: "ECDSA", hash: "SHA-256" },
      record.privateKey,
      new TextEncoder().encode(canonical)
    );

    return {
      "X-MG-Device-Id": record.deviceId,
      "X-MG-Device-Key-Version": String(record.keyVersion),
      "X-MG-Device-Credential": record.credential,
      "X-MG-Device-Timestamp": timestamp,
      "X-MG-Device-Nonce": nonce,
      "X-MG-Device-Signature": toBase64(new Uint8Array(signature))
    };
  }
}

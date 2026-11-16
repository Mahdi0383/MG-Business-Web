const DATABASE_NAME = "mg-business-local";
const DATABASE_VERSION = 1;
const OPERATIONS_STORE = "operations";
const META_STORE = "metadata";

let databasePromise;

function openDatabase() {
  if (typeof indexedDB === "undefined")
    return Promise.reject(new Error("IndexedDB is unavailable in this environment."));

  if (!databasePromise) {
    databasePromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
      request.onupgradeneeded = () => {
        const database = request.result;
        if (!database.objectStoreNames.contains(OPERATIONS_STORE)) {
          const operations = database.createObjectStore(OPERATIONS_STORE, { keyPath: "operationId" });
          operations.createIndex("byStateCreatedAt", ["state", "createdAtUtc"]);
          operations.createIndex("byTenantState", ["tenantId", "state"]);
        }
        if (!database.objectStoreNames.contains(META_STORE))
          database.createObjectStore(META_STORE, { keyPath: "key" });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("Could not open local MG database."));
      request.onblocked = () => reject(new Error("Local MG database upgrade is blocked by another open tab."));
    }).catch(error => {
      databasePromise = null;
      throw error;
    });
  }

  return databasePromise;
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("IndexedDB request failed."));
  });
}

function transactionDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error || new Error("IndexedDB transaction failed."));
    transaction.onabort = () => reject(transaction.error || new Error("IndexedDB transaction was aborted."));
  });
}

async function sha256(value) {
  if (!globalThis.crypto?.subtle)
    throw new Error("Secure cryptography is unavailable. Open MG Business over HTTPS or localhost.");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("").toUpperCase();
}

export class LocalOperationStore {
  async getDeviceId() {
    const database = await openDatabase();
    const transaction = database.transaction(META_STORE, "readwrite");
    const done = transactionDone(transaction);
    const store = transaction.objectStore(META_STORE);
    const existing = await requestResult(store.get("device-id"));
    if (existing?.value) {
      await done;
      return existing.value;
    }
    const deviceId = crypto.randomUUID();
    store.put({ key: "device-id", value: deviceId });
    await done;
    return deviceId;
  }

  async enqueue({ tenantId, branchId = null, operationType, payload, schemaVersion = 1, authorityEpoch = 0 }) {
    if (!tenantId) throw new TypeError("tenantId is required.");
    if (!operationType || operationType.length > 100) throw new TypeError("operationType must be 1–100 characters.");
    if (!Number.isInteger(schemaVersion) || schemaVersion < 1) throw new TypeError("schemaVersion must be a positive integer.");
    if (!Number.isSafeInteger(authorityEpoch) || authorityEpoch < 0) throw new TypeError("authorityEpoch must be a non-negative integer.");

    const payloadJson = JSON.stringify(payload);
    if (new TextEncoder().encode(payloadJson).byteLength > 64 * 1024)
      throw new RangeError("Offline operation payload exceeds 64 KiB.");

    const operation = {
      operationId: crypto.randomUUID(),
      deviceId: await this.getDeviceId(),
      tenantId,
      branchId,
      operationType,
      schemaVersion,
      authorityEpoch,
      occurredAtUtc: new Date().toISOString(),
      payloadJson,
      payloadSha256: await sha256(payloadJson),
      state: "pending",
      attempts: 0,
      createdAtUtc: new Date().toISOString(),
      acknowledgedAtUtc: null,
      serverSequence: null
    };

    const database = await openDatabase();
    const transaction = database.transaction(OPERATIONS_STORE, "readwrite");
    const done = transactionDone(transaction);
    transaction.objectStore(OPERATIONS_STORE).add(operation);
    await done;
    return operation;
  }

  async getPending(tenantId, limit = 100) {
    const database = await openDatabase();
    const transaction = database.transaction(OPERATIONS_STORE, "readonly");
    const done = transactionDone(transaction);
    const index = transaction.objectStore(OPERATIONS_STORE).index("byTenantState");
    const rows = await requestResult(index.getAll(IDBKeyRange.only([tenantId, "pending"]), Math.max(1, Math.min(limit, 100))));
    await done;
    return rows.sort((a, b) => a.createdAtUtc.localeCompare(b.createdAtUtc));
  }

  async acknowledge(acknowledgements) {
    if (!Array.isArray(acknowledgements) || acknowledgements.length === 0) return;
    const database = await openDatabase();
    const transaction = database.transaction(OPERATIONS_STORE, "readwrite");
    const done = transactionDone(transaction);
    const store = transaction.objectStore(OPERATIONS_STORE);

    for (const acknowledgement of acknowledgements) {
      const operation = await requestResult(store.get(acknowledgement.operationId));
      if (!operation) continue;
      operation.state = "acknowledged";
      operation.serverSequence = acknowledgement.sequence;
      operation.acknowledgedAtUtc = new Date().toISOString();
      store.put(operation);
    }

    await done;
  }

  async getPendingCount(tenantId) {
    const database = await openDatabase();
    const transaction = database.transaction(OPERATIONS_STORE, "readonly");
    const done = transactionDone(transaction);
    const count = await requestResult(
      transaction.objectStore(OPERATIONS_STORE).index("byTenantState")
        .count(IDBKeyRange.only([tenantId, "pending"]))
    );
    await done;
    return count;
  }

  async getCursor(tenantId) {
    const database = await openDatabase();
    const transaction = database.transaction(META_STORE, "readonly");
    const done = transactionDone(transaction);
    const row = await requestResult(transaction.objectStore(META_STORE).get(`cursor:${tenantId}`));
    await done;
    return Number.isSafeInteger(row?.value) && row.value >= 0 ? row.value : 0;
  }

  async setCursor(tenantId, sequence) {
    if (!Number.isSafeInteger(sequence) || sequence < 0)
      throw new TypeError("Sync cursor must be a non-negative safe integer.");
    const database = await openDatabase();
    const transaction = database.transaction(META_STORE, "readwrite");
    const done = transactionDone(transaction);
    transaction.objectStore(META_STORE).put({ key: `cursor:${tenantId}`, value: sequence });
    await done;
  }
}

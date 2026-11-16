const DATABASE_NAME = "mg-business-local";
const DATABASE_VERSION = 4;
const OPERATIONS_STORE = "operations";
const META_STORE = "metadata";
const INBOX_STORE = "inbox";

let databasePromise;

function openDatabase() {
  if (typeof indexedDB === "undefined")
    return Promise.reject(new Error("IndexedDB is unavailable in this environment."));

  if (!databasePromise) {
    databasePromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
      request.onupgradeneeded = event => {
        const database = request.result;
        let operations;
        if (!database.objectStoreNames.contains(OPERATIONS_STORE)) {
          operations = database.createObjectStore(OPERATIONS_STORE, { keyPath: "operationId" });
        } else {
          operations = event.target.transaction.objectStore(OPERATIONS_STORE);
        }
        if (!operations.indexNames.contains("byStateCreatedAt"))
          operations.createIndex("byStateCreatedAt", ["state", "createdAtUtc"]);
        if (!operations.indexNames.contains("byTenantState"))
          operations.createIndex("byTenantState", ["tenantId", "state"]);
        if (!operations.indexNames.contains("byTenantStateCreatedAt"))
          operations.createIndex("byTenantStateCreatedAt", ["tenantId", "state", "createdAtUtc"]);
        if (!database.objectStoreNames.contains(META_STORE))
          database.createObjectStore(META_STORE, { keyPath: "key" });
        let inbox;
        if (!database.objectStoreNames.contains(INBOX_STORE))
          inbox = database.createObjectStore(INBOX_STORE, { keyPath: ["tenantId", "sequence"] });
        else
          inbox = event.target.transaction.objectStore(INBOX_STORE);
        if (inbox.indexNames.contains("byTenantApplied"))
          inbox.deleteIndex("byTenantApplied");
        inbox.createIndex("byTenantApplied", ["tenantId", "applied"]);
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
    if (typeof tenantId !== "string" || !tenantId.trim()) throw new TypeError("tenantId is required.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    if (typeof operationType !== "string" || !operationType.trim() || operationType.trim().length > 100)
      throw new TypeError("operationType must be 1–100 characters.");
    if (!Number.isInteger(schemaVersion) || schemaVersion < 1) throw new TypeError("schemaVersion must be a positive integer.");
    if (authorityEpoch !== 0) throw new TypeError("Non-zero authority epochs require a verified signed lease; failover is not enabled yet.");
    if (!payload || typeof payload !== "object" || Array.isArray(payload))
      throw new TypeError("Offline operation payload must be a JSON object.");

    let payloadJson;
    try { payloadJson = JSON.stringify(payload); }
    catch { throw new TypeError("Offline operation payload must be JSON serializable."); }
    if (typeof payloadJson !== "string")
      throw new TypeError("Offline operation payload must be JSON serializable.");
    if (new TextEncoder().encode(payloadJson).byteLength > 64 * 1024)
      throw new RangeError("Offline operation payload exceeds 64 KiB.");

    const operation = {
      operationId: crypto.randomUUID(),
      deviceId: await this.getDeviceId(),
      tenantId,
      branchId,
      operationType: operationType.trim(),
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

  async getPending(tenantId, limit = 100, branchId = null, allowTenantWide = false) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required.");
    const boundedLimit = Number.isInteger(limit) ? Math.max(1, Math.min(limit, 100)) : 100;
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");

    const database = await openDatabase();
    const transaction = database.transaction(OPERATIONS_STORE, "readonly");
    const done = transactionDone(transaction);
    const index = transaction.objectStore(OPERATIONS_STORE).index("byTenantStateCreatedAt");
    const range = IDBKeyRange.bound(
      [tenantId, "pending", ""],
      [tenantId, "pending", "\uffff"]
    );

    if (branchId === null && allowTenantWide) {
      const rows = await requestResult(index.getAll(range, boundedLimit));
      await done;
      return rows;
    }

    // Keep global FIFO order while skipping other branches' queued work,
    // so one branch's offline backlog cannot starve another branch's sync.
    const rows = await new Promise((resolve, reject) => {
      const matching = [];
      const request = index.openCursor(range);
      request.onerror = () => reject(request.error || new Error("Could not read pending sync operations."));
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor || matching.length >= boundedLimit) {
          resolve(matching);
          return;
        }
        const operation = cursor.value;
        const matchesBranch = branchId === null
          ? operation.branchId != null
          : operation.branchId === branchId;
        if (matchesBranch || (allowTenantWide && operation.branchId == null))
          matching.push(operation);
        cursor.continue();
      };
    });
    await done;
    return rows;
  }

  async acknowledge(acknowledgements) {
    if (!Array.isArray(acknowledgements) || acknowledgements.length === 0) return;
    const database = await openDatabase();
    const readTransaction = database.transaction(OPERATIONS_STORE, "readonly");
    const readDone = transactionDone(readTransaction);
    const readStore = readTransaction.objectStore(OPERATIONS_STORE);
    const existing = await Promise.all(acknowledgements.map(item => requestResult(readStore.get(item.operationId))));
    await readDone;

    const writeTransaction = database.transaction(OPERATIONS_STORE, "readwrite");
    const writeDone = transactionDone(writeTransaction);
    const writeStore = writeTransaction.objectStore(OPERATIONS_STORE);
    existing.forEach((operation, index) => {
      if (!operation) return;
      const acknowledgement = acknowledgements[index];
      if (!Number.isSafeInteger(acknowledgement.sequence) || acknowledgement.sequence < 1)
        throw new TypeError("Server acknowledgement sequence is invalid.");
      operation.state = "acknowledged";
      operation.serverSequence = acknowledgement.sequence;
      operation.acknowledgedAtUtc = new Date().toISOString();
      writeStore.put(operation);
    });
    await writeDone;
  }

  async getPendingCount(tenantId, branchId = null, allowTenantWide = false) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    if (typeof allowTenantWide !== "boolean")
      throw new TypeError("allowTenantWide must be a boolean.");
    const database = await openDatabase();
    const transaction = database.transaction(OPERATIONS_STORE, "readonly");
    const done = transactionDone(transaction);
    const index = transaction.objectStore(OPERATIONS_STORE).index("byTenantState");

    if (branchId === null && allowTenantWide) {
      const count = await requestResult(index.count(IDBKeyRange.only([tenantId, "pending"])));
      await done;
      return count;
    }

    const count = await new Promise((resolve, reject) => {
      let matching = 0;
      const request = index.openCursor(IDBKeyRange.only([tenantId, "pending"]));
      request.onerror = () => reject(request.error || new Error("Could not count pending sync operations."));
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) {
          resolve(matching);
          return;
        }
        const operation = cursor.value;
        const matchesBranch = branchId === null
          ? operation.branchId != null
          : operation.branchId === branchId;
        if (matchesBranch || (allowTenantWide && operation.branchId == null))
          matching += 1;
        cursor.continue();
      };
    });
    await done;
    return count;
  }

  async storeIncomingPage(tenantId, operations, nextSequence, branchId = null) {
    if (!tenantId) throw new TypeError("tenantId is required.");
    if (!Array.isArray(operations)) throw new TypeError("operations must be an array.");
    if (!Number.isSafeInteger(nextSequence) || nextSequence < 0)
      throw new TypeError("nextSequence must be a non-negative safe integer.");

    let previousSequence = await this.getCursor(tenantId, branchId);
    if (nextSequence < previousSequence)
      throw new Error("Incoming sync cursor cannot move backwards.");

    for (const operation of operations) {
      if (!Number.isSafeInteger(operation.sequence) || operation.sequence <= previousSequence
        || typeof operation.operationId !== "string" || !operation.operationId
        || typeof operation.payloadJson !== "string" || typeof operation.payloadSha256 !== "string"
        || !Number.isInteger(operation.schemaVersion) || operation.schemaVersion < 1)
        throw new TypeError("Incoming operation envelope is invalid.");
      JSON.parse(operation.payloadJson);
      const actualHash = await sha256(operation.payloadJson);
      if (actualHash !== operation.payloadSha256.toUpperCase())
        throw new Error("Incoming operation payload integrity check failed.");
      previousSequence = operation.sequence;
    }

    // The server may advance across journal rows hidden by branch permissions.
    // The cursor therefore represents the scanned journal head, not the last visible row.
    if (nextSequence < previousSequence)
      throw new Error("Incoming sync cursor cannot precede the last received operation.");

    const database = await openDatabase();
    const transaction = database.transaction([INBOX_STORE, META_STORE], "readwrite");
    const done = transactionDone(transaction);
    const inbox = transaction.objectStore(INBOX_STORE);
    const metadata = transaction.objectStore(META_STORE);
    const cursorKey = `cursor:${tenantId}:${branchId || "all"}`;
    const cursorRequest = metadata.get(cursorKey);
    let integrityConflict = null;
    cursorRequest.onsuccess = () => {
      const storedCursor = Number.isSafeInteger(cursorRequest.result?.value) ? cursorRequest.result.value : 0;
      if (nextSequence < storedCursor) {
        transaction.abort();
        return;
      }
      for (const operation of operations) {
        const existingRequest = inbox.get([tenantId, operation.sequence]);
        existingRequest.onsuccess = () => {
          const existing = existingRequest.result;
          if (existing) {
            // A tenant-wide operation can be encountered from more than one branch
            // cursor. Preserve its state only when the immutable envelope is identical.
            if (existing.operationId !== operation.operationId
              || existing.payloadSha256 !== operation.payloadSha256
              || existing.branchId !== operation.branchId
              || existing.operationType !== operation.operationType
              || existing.schemaVersion !== operation.schemaVersion) {
              integrityConflict = new Error("A journal sequence was reused with a different incoming operation envelope.");
              transaction.abort();
            }
            return;
          }
          inbox.add({
            ...operation,
            tenantId,
            applied: false,
            appliedAtUtc: null,
            receivedLocallyAtUtc: new Date().toISOString()
          });
        };
      }
      metadata.put({ key: cursorKey, value: nextSequence });
    };
    try {
      await done;
    } catch (error) {
      if (integrityConflict) throw integrityConflict;
      throw error;
    }
    return operations.length;
  }

  async getInboxCount(tenantId, branchId = null) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    const database = await openDatabase();
    const transaction = database.transaction(INBOX_STORE, "readonly");
    const done = transactionDone(transaction);
    const index = transaction.objectStore(INBOX_STORE).index("byTenantApplied");

    if (branchId === null) {
      const count = await requestResult(index.count(IDBKeyRange.only([tenantId, false])));
      await done;
      return count;
    }

    const count = await new Promise((resolve, reject) => {
      let matching = 0;
      const request = index.openCursor(IDBKeyRange.only([tenantId, false]));
      request.onerror = () => reject(request.error || new Error("Could not count incoming sync operations."));
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) {
          resolve(matching);
          return;
        }
        if (cursor.value.branchId == null || cursor.value.branchId === branchId)
          matching += 1;
        cursor.continue();
      };
    });
    await done;
    return count;
  }

  async getUnappliedIncoming(tenantId, limit = 100, branchId = null) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    const boundedLimit = Number.isInteger(limit) ? Math.max(1, Math.min(limit, 200)) : 100;
    const database = await openDatabase();
    const transaction = database.transaction(INBOX_STORE, "readonly");
    const done = transactionDone(transaction);
    const index = transaction.objectStore(INBOX_STORE).index("byTenantApplied");
    const range = IDBKeyRange.only([tenantId, false]);

    if (branchId === null) {
      const rows = await requestResult(index.getAll(range, boundedLimit));
      await done;
      return rows.sort((a, b) => a.sequence - b.sequence);
    }

    const rows = await new Promise((resolve, reject) => {
      const matching = [];
      const request = index.openCursor(range);
      request.onerror = () => reject(request.error || new Error("Could not read unapplied incoming operations."));
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor || matching.length >= boundedLimit) {
          resolve(matching);
          return;
        }
        const row = cursor.value;
        if (row.branchId == null || row.branchId === branchId)
          matching.push(row);
        cursor.continue();
      };
    });
    await done;
    return rows.sort((a, b) => a.sequence - b.sequence);
  }

  async markIncomingApplied(tenantId, sequences) {
    if (!Array.isArray(sequences) || sequences.length === 0) return;
    const database = await openDatabase();
    const readTransaction = database.transaction(INBOX_STORE, "readonly");
    const readDone = transactionDone(readTransaction);
    const readStore = readTransaction.objectStore(INBOX_STORE);
    const existing = await Promise.all(sequences.map(sequence =>
      requestResult(readStore.get([tenantId, sequence]))));
    await readDone;

    const writeTransaction = database.transaction(INBOX_STORE, "readwrite");
    const writeDone = transactionDone(writeTransaction);
    const writeStore = writeTransaction.objectStore(INBOX_STORE);
    existing.forEach(row => {
      if (!row) return;
      row.applied = true;
      row.appliedAtUtc = new Date().toISOString();
      writeStore.put(row);
    });
    await writeDone;
  }

  async getCursor(tenantId, branchId = null) {
    const database = await openDatabase();
    const transaction = database.transaction(META_STORE, "readonly");
    const done = transactionDone(transaction);
    const row = await requestResult(transaction.objectStore(META_STORE).get(`cursor:${tenantId}:${branchId || "all"}`));
    await done;
    return Number.isSafeInteger(row?.value) && row.value >= 0 ? row.value : 0;
  }

  async setCursor(tenantId, sequence, branchId = null) {
    if (!tenantId) throw new TypeError("tenantId is required.");
    if (!Number.isSafeInteger(sequence) || sequence < 0)
      throw new TypeError("Sync cursor must be a non-negative safe integer.");
    const database = await openDatabase();
    const transaction = database.transaction(META_STORE, "readwrite");
    const done = transactionDone(transaction);
    const metadata = transaction.objectStore(META_STORE);
    const key = `cursor:${tenantId}:${branchId || "all"}`;
    const request = metadata.get(key);
    request.onsuccess = () => {
      const current = Number.isSafeInteger(request.result?.value) && request.result.value >= 0
        ? request.result.value : 0;
      if (sequence < current) {
        transaction.abort();
        return;
      }
      metadata.put({ key, value: sequence });
    };
    await done;
  }
}

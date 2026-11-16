const DATABASE_NAME = "mg-business-local";
const DATABASE_VERSION = 3;
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
        if (!database.objectStoreNames.contains(OPERATIONS_STORE)) {
          const operations = database.createObjectStore(OPERATIONS_STORE, { keyPath: "operationId" });
          operations.createIndex("byStateCreatedAt", ["state", "createdAtUtc"]);
          operations.createIndex("byTenantState", ["tenantId", "state"]);
        }
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


  async storeIncomingPage(tenantId, operations, nextSequence) {
    if (!tenantId) throw new TypeError("tenantId is required.");
    if (!Array.isArray(operations)) throw new TypeError("operations must be an array.");
    if (!Number.isSafeInteger(nextSequence) || nextSequence < 0)
      throw new TypeError("nextSequence must be a non-negative safe integer.");

    let previousSequence = await this.getCursor(tenantId);
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

    if (operations.length > 0 && nextSequence !== previousSequence)
      throw new Error("Incoming sync cursor must equal the last received sequence.");
    if (operations.length === 0 && nextSequence !== previousSequence)
      throw new Error("An empty incoming page cannot advance the sync cursor.");

    const database = await openDatabase();
    const transaction = database.transaction([INBOX_STORE, META_STORE], "readwrite");
    const done = transactionDone(transaction);
    const inbox = transaction.objectStore(INBOX_STORE);
    const metadata = transaction.objectStore(META_STORE);
    const cursorRequest = metadata.get(`cursor:${tenantId}`);
    cursorRequest.onsuccess = () => {
      const storedCursor = Number.isSafeInteger(cursorRequest.result?.value) ? cursorRequest.result.value : 0;
      if (nextSequence < storedCursor) {
        transaction.abort();
        return;
      }
      for (const operation of operations) {
        inbox.put({
          ...operation,
          tenantId,
          applied: false,
          appliedAtUtc: null,
          receivedLocallyAtUtc: new Date().toISOString()
        });
      }
      metadata.put({ key: `cursor:${tenantId}`, value: nextSequence });
    };
    await done;
    return operations.length;
  }

  async getInboxCount(tenantId) {
    const database = await openDatabase();
    const transaction = database.transaction(INBOX_STORE, "readonly");
    const done = transactionDone(transaction);
    const count = await requestResult(
      transaction.objectStore(INBOX_STORE).index("byTenantApplied")
        .count(IDBKeyRange.only([tenantId, false]))
    );
    await done;
    return count;
  }

  async getUnappliedIncoming(tenantId, limit = 100) {
    const database = await openDatabase();
    const transaction = database.transaction(INBOX_STORE, "readonly");
    const done = transactionDone(transaction);
    const rows = await requestResult(
      transaction.objectStore(INBOX_STORE).index("byTenantApplied")
        .getAll(IDBKeyRange.only([tenantId, false]), Math.max(1, Math.min(limit, 200)))
    );
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

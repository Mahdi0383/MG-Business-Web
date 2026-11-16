import "fake-indexeddb/auto";
import test from "node:test";
import assert from "node:assert/strict";
import { LocalOperationStore, isIncomingRowVisible, isPendingOperationVisible, isSameIncomingEnvelope, isJsonObjectPayload, normalizeLocalCustomer, isConflictVisible } from "./local-operation-store.js";
import { IncomingOperationProcessor } from "./incoming-operation-processor.js";
import { classifyIncomingOperation } from "./conflict-policy.js";



test("requires an authenticated actor before creating a browser device identity", async () => {
  const store = new LocalOperationStore();
  await assert.rejects(store.getDeviceId(), /actorId/);
  await assert.rejects(store.getDeviceId(" "), /actorId/);
});


test("does not expose tenant-wide pending or inbox counts without manager authority", async () => {
  const store = new LocalOperationStore();
  assert.equal(await store.getPendingCount("tenant-1", null, false, "device-1"), 0);
  assert.equal(await store.getInboxCount("tenant-1", null, false), 0);
  assert.deepEqual(
    await store.getPending("tenant-1", 100, null, false, "device-1"),
    []
  );
});

test("validates branch context before reading local sync counters", async () => {
  const store = new LocalOperationStore();
  await assert.rejects(store.getPendingCount("tenant-1", " "), /branchId/);
  await assert.rejects(store.getPendingCount("tenant-1", null, "yes"), /allowTenantWide/);
  await assert.rejects(store.getInboxCount("tenant-1", 123), /branchId/);
});

test("requires a device identity before counting pending outbox operations", async () => {
  const store = new LocalOperationStore();
  await assert.rejects(
    store.getPendingCount("tenant-1", "branch-a", false),
    /deviceId/
  );
});


test("validates branch scope before reading the incoming inbox", async () => {
  const store = new LocalOperationStore();
  await assert.rejects(store.getUnappliedIncoming("tenant-1", 100, " "), /branchId/);
});


test("requires explicit boolean authority when reading tenant-wide outbox entries", async () => {
  const store = new LocalOperationStore();
  await assert.rejects(
    store.getPending("tenant-1", 100, "branch-a", "true"),
    /allowTenantWide/
  );
});



test("requires a device identity before reading the pending outbox", async () => {
  const store = new LocalOperationStore();
  await assert.rejects(
    store.getPending("tenant-1", 100, "branch-a", false),
    /deviceId/
  );
});

test("validates tenant and branch before accepting an incoming page", async () => {
  const store = new LocalOperationStore();
  await assert.rejects(store.storeIncomingPage(" ", [], 0), /tenantId/);
  await assert.rejects(store.storeIncomingPage("tenant-1", [], 0, " "), /branchId/);
});


test("requires the authenticated actor before creating an offline operation", async () => {
  const store = new LocalOperationStore();
  await assert.rejects(
    store.enqueue({ tenantId: "tenant-1", operationType: "customer.updated", payload: {} }),
    /actorId is required/
  );
});

test("rejects non-object payloads before attempting IndexedDB access", async () => {
  const store = new LocalOperationStore();
  for (const payload of [undefined, null, "not-an-object", 42, [], false]) {
    await assert.rejects(
      store.enqueue({ tenantId: "tenant-1", operationType: "customer.profile.updated", payload }),
      /payload must be a JSON object/
    );
  }
});

test("rejects circular and oversized payloads before enqueue", async () => {
  const store = new LocalOperationStore();
  const circular = {};
  circular.self = circular;
  await assert.rejects(
    store.enqueue({ tenantId: "tenant-1", operationType: "customer.profile.updated", payload: circular }),
    /JSON serializable/
  );
  await assert.rejects(
    store.enqueue({
      tenantId: "tenant-1",
      operationType: "customer.profile.updated",
      payload: { note: "x".repeat(65 * 1024) }
    }),
    /exceeds 64 KiB/
  );
});


test("rejects invalid branch IDs before opening local storage", async () => {
  const store = new LocalOperationStore();
  for (const branchId of ["", "   ", 17, {}]) {
    await assert.rejects(
      store.enqueue({
        tenantId: "tenant-1",
        branchId,
        operationType: "customer.profile.updated",
        payload: { name: "MG" }
      }),
      /branchId/
    );
  }
});

test("requires a normalized non-empty operation type", async () => {
  const store = new LocalOperationStore();
  await assert.rejects(
    store.enqueue({ tenantId: "tenant-1", operationType: "   ", payload: { name: "MG" } }),
    /operationType/
  );
  await assert.rejects(
    store.enqueue({ tenantId: "tenant-1", operationType: "x".repeat(101), payload: { name: "MG" } }),
    /operationType/
  );
});

test("rejects missing tenant IDs and invalid schema versions", async () => {
  const store = new LocalOperationStore();
  await assert.rejects(
    store.enqueue({ tenantId: "  ", operationType: "customer.profile.updated", payload: {} }),
    /tenantId/
  );
  await assert.rejects(
    store.enqueue({ tenantId: "tenant-1", operationType: "customer.profile.updated", payload: {}, schemaVersion: 0 }),
    /schemaVersion/
  );
});

test("rejects unverified authority epochs before accessing local storage", async () => {
  const store = new LocalOperationStore();
  await assert.rejects(
    store.enqueue({
      tenantId: "tenant-1",
      operationType: "inventory.stock.adjusted",
      payload: { quantity: 2 },
      authorityEpoch: 1
    }),
    /signed lease/
  );
});

import { SyncAgent } from "./sync-agent.js";

function createSyncAgent({ cursor = 0, page, deviceId = "device-1" } = {}) {
  const calls = { stored: null, acknowledged: null };
  const operationStore = {
    getCursor: async () => cursor,
    storeIncomingPage: async (...args) => { calls.stored = args; },
    getDeviceId: async () => deviceId
  };
  const apiClient = {
    get: async () => page,
    post: async (requestPath, body) => {
      if (requestPath === "/api/sync/acknowledgements") calls.acknowledged = body;
      return {};
    }
  };
  return { agent: new SyncAgent(apiClient, operationStore), calls };
}

test("branch-filtered pull acknowledges the scanned journal cursor", async () => {
  const operation = {
    sequence: 3,
    operationId: "operation-visible",
    operationType: "customer.updated",
    payloadJson: JSON.stringify({ name: "MG" }),
    payloadSha256: "b618f9a65827379fd2f866d0ca039122397e99c4d3f7d37052083c4095339f8a",
    schemaVersion: 1
  };
  const { agent, calls } = createSyncAgent({
    cursor: 1,
    page: { operations: [operation], nextSequence: 5, hasMore: false }
  });
  const result = await agent.pullIncoming("tenant-1", { actorId: "actor-1" });
  assert.equal(result.status, "pulled");
  assert.equal(result.received, 1);
  assert.deepEqual(calls.stored, ["tenant-1", [operation], 5, null]);
  assert.deepEqual(calls.acknowledged, { deviceId: "device-1", sequence: 5, branchId: null });
});

test("branch-filtered pull advances across pages containing only hidden rows", async () => {
  const { agent, calls } = createSyncAgent({
    cursor: 4,
    page: { operations: [], nextSequence: 8, hasMore: false }
  });
  const result = await agent.pullIncoming("tenant-1", { actorId: "actor-1" });
  assert.equal(result.status, "pulled");
  assert.equal(result.received, 0);
  assert.deepEqual(calls.stored, ["tenant-1", [], 8, null]);
  assert.deepEqual(calls.acknowledged, { deviceId: "device-1", sequence: 8, branchId: null });
});

test("pull rejects a scan cursor preceding a visible operation", async () => {
  const { agent, calls } = createSyncAgent({
    cursor: 1,
    page: {
      operations: [{
        sequence: 3,
        operationId: "operation-visible",
        operationType: "customer.updated",
        payloadJson: "{}",
        payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        schemaVersion: 1
      }],
      nextSequence: 2,
      hasMore: false
    }
  });
  await assert.rejects(agent.pullIncoming("tenant-1", { actorId: "actor-1" }), /precedes the last returned operation/);
  assert.equal(calls.acknowledged, null);
});


test("does not expose tenant-wide incoming rows without manager authority", async () => {
  const store = new LocalOperationStore();
  assert.deepEqual(await store.getUnappliedIncoming("tenant-1", 100, null, false), []);
  await assert.rejects(
    store.getUnappliedIncoming("tenant-1", 100, null, "true"),
    /allowTenantWide/
  );
});


test("incoming inbox visibility is restricted to the selected branch unless manager-authorized", () => {
  const branchRow = { branchId: "branch-a" };
  const otherBranchRow = { branchId: "branch-b" };
  const tenantWideRow = { branchId: null };

  assert.equal(isIncomingRowVisible(branchRow, "branch-a", false), true);
  assert.equal(isIncomingRowVisible(otherBranchRow, "branch-a", false), false);
  assert.equal(isIncomingRowVisible(tenantWideRow, "branch-a", false), false);
  assert.equal(isIncomingRowVisible(tenantWideRow, "branch-a", true), true);
  assert.equal(isIncomingRowVisible(otherBranchRow, "branch-a", true), false);
  assert.equal(isIncomingRowVisible(tenantWideRow, null, false), false);
  assert.equal(isIncomingRowVisible(tenantWideRow, null, true), true);
  assert.equal(isIncomingRowVisible(branchRow, null, true), false);
  assert.equal(isIncomingRowVisible(otherBranchRow, null, true), false);
});


test("pending outbox visibility matches both device and branch authority", () => {
  const selected = { deviceId: "device-a", branchId: "branch-a" };
  const otherDevice = { deviceId: "device-b", branchId: "branch-a" };
  const otherBranch = { deviceId: "device-a", branchId: "branch-b" };
  const tenantWide = { deviceId: "device-a", branchId: null };

  assert.equal(isPendingOperationVisible(selected, "branch-a", false, "device-a"), true);
  assert.equal(isPendingOperationVisible(otherDevice, "branch-a", true, "device-a"), false);
  assert.equal(isPendingOperationVisible(otherBranch, "branch-a", true, "device-a"), false);
  assert.equal(isPendingOperationVisible(tenantWide, "branch-a", false, "device-a"), false);
  assert.equal(isPendingOperationVisible(tenantWide, "branch-a", true, "device-a"), true);
  assert.equal(isPendingOperationVisible(tenantWide, null, true, "device-a"), true);
  assert.equal(isPendingOperationVisible(tenantWide, null, false, "device-a"), false);
});

test("incoming sequence reuse requires the complete immutable journal envelope to match", () => {
  const envelope = {
    tenantId: "tenant-1",
    sequence: 42,
    operationId: "operation-42",
    actorId: "actor-1",
    deviceId: "device-1",
    branchId: "branch-a",
    operationType: "customer.profile.updated",
    schemaVersion: 1,
    authorityEpoch: 0,
    occurredAtUtc: "2026-11-16T10:00:00.000Z",
    receivedAtUtc: "2026-11-16T10:00:01.000Z",
    payloadJson: JSON.stringify({ name: "MG" }),
    payloadSha256: "b618f9a65827379fd2f866d0ca039122397e99c4d3f7d37052083c4095339f8a"
  };
  assert.equal(isSameIncomingEnvelope(envelope, { ...envelope }), true);
  for (const [field, value] of [
    ["actorId", "actor-2"],
    ["deviceId", "device-2"],
    ["authorityEpoch", 1],
    ["occurredAtUtc", "2026-11-16T10:01:00.000Z"],
    ["receivedAtUtc", "2026-11-16T10:01:01.000Z"],
    ["payloadJson", "{}"]
  ]) {
    assert.equal(isSameIncomingEnvelope(envelope, { ...envelope, [field]: value }), false, field);
  }
  assert.equal(isSameIncomingEnvelope(envelope, { ...envelope, actorId: undefined }), false);
});


test("incoming payload validation accepts only JSON objects", () => {
  for (const payload of ['{}', '{"name":"MG"}', '{"nested":{"value":1}}']) {
    assert.equal(isJsonObjectPayload(payload), true, payload);
  }
  for (const payload of ['null', '[]', '42', '"text"', 'true', '', '{invalid']) {
    assert.equal(isJsonObjectPayload(payload), false, payload);
  }
});


test("normalizes and validates local customer command data", () => {
  assert.deepEqual(normalizeLocalCustomer({
    code: " c-001 ", name: "  MG Customer ", phone: " 09120000000 ",
    email: " TEST@EXAMPLE.COM ", address: " Main Street ", notes: " VIP "
  }), {
    code: "C-001", name: "MG Customer", phone: "09120000000",
    email: "test@example.com", address: "Main Street", notes: "VIP"
  });
});

test("rejects invalid local customer fields before opening IndexedDB", () => {
  for (const customer of [
    null, [], { code: "", name: "Valid" },
    { code: "C".repeat(41), name: "Valid" }, { code: "C-1", name: " " },
    { code: "C-1", name: "N".repeat(161) },
    { code: "C-1", name: "Valid", email: "not-an-email" },
    { code: "C-1", name: "Valid", phone: 123 }
  ]) assert.throws(() => normalizeLocalCustomer(customer));
});


test("scopes unresolved conflicts to the active branch and tenant-wide authority", () => {
  const branchConflict = { branchId: "branch-a" };
  const otherBranchConflict = { branchId: "branch-b" };
  const tenantWideConflict = { branchId: null };
  assert.equal(isConflictVisible(branchConflict, "branch-a", false), true);
  assert.equal(isConflictVisible(otherBranchConflict, "branch-a", false), false);
  assert.equal(isConflictVisible(tenantWideConflict, "branch-a", false), false);
  assert.equal(isConflictVisible(tenantWideConflict, "branch-a", true), true);
  assert.equal(isConflictVisible(branchConflict, null, true), false);
  assert.equal(isConflictVisible(tenantWideConflict, null, true), true);
  assert.equal(isConflictVisible(branchConflict, null, false), false);
});

test("commits customer edits with their outbox command and advances version only after acknowledgement", async () => {
  const store = new LocalOperationStore();
  const tenantId = "tenant-" + crypto.randomUUID();
  const branchId = "branch-" + crypto.randomUUID();
  const actorId = "actor-" + crypto.randomUUID();
  const created = await store.createCustomer({
    tenantId, branchId, actorId,
    customer: { code: "C-EDIT-1", name: "Before", phone: "09120000000" }
  });

  await store.acknowledge([{ operationId: created.originOperationId, sequence: 1 }]);
  const deviceId = await store.getDeviceId(actorId);
  const updated = await store.updateCustomer({
    tenantId, branchId, actorId, customerId: created.id,
    customer: { code: "C-EDIT-CHANGED", name: "After", phone: "09121111111", email: "after@example.com", address: "New", notes: "Updated" }
  });

  assert.equal(updated.name, "After");
  assert.equal(updated.code, "C-EDIT-1");
  assert.equal(updated.version, 1);
  assert.equal(updated.syncStatus, "pending");
  const pending = await store.getPending(tenantId, 100, branchId, false, deviceId);
  assert.equal(pending.length, 1);
  assert.equal(pending[0].operationType, "customer.profile.updated");
  const payload = JSON.parse(pending[0].payloadJson);
  assert.equal(payload.baseVersion, 1);
  assert.equal(payload.customerId, created.id);

  await assert.rejects(store.updateCustomer({
    tenantId, branchId, actorId, customerId: created.id,
    customer: { name: "Overlapping edit" }
  }), /pending update/);

  await store.acknowledge([{ operationId: pending[0].operationId, sequence: 2 }]);
  const rows = await store.getCustomers(tenantId);
  const confirmed = rows.find(row => row.id === created.id);
  assert.equal(confirmed.version, 2);
  assert.equal(confirmed.syncStatus, "synced");
  assert.equal(confirmed.name, "After");
});

test("serializes competing local customer edits and keeps only one pending command", async () => {
  const store = new LocalOperationStore();
  const tenantId = "tenant-" + crypto.randomUUID();
  const branchId = "branch-" + crypto.randomUUID();
  const actorId = "actor-" + crypto.randomUUID();
  const created = await store.createCustomer({
    tenantId, branchId, actorId,
    customer: { code: "C-EDIT-2", name: "Original" }
  });
  await store.acknowledge([{ operationId: created.originOperationId, sequence: 1 }]);

  const results = await Promise.allSettled([
    store.updateCustomer({ tenantId, branchId, actorId, customerId: created.id, customer: { name: "First" } }),
    store.updateCustomer({ tenantId, branchId, actorId, customerId: created.id, customer: { name: "Second" } })
  ]);
  assert.equal(results.filter(result => result.status === "fulfilled").length, 1);
  assert.equal(results.filter(result => result.status === "rejected").length, 1);
  const deviceId = await store.getDeviceId(actorId);
  const pending = await store.getPending(tenantId, 100, branchId, false, deviceId);
  assert.equal(pending.length, 1);
});

test("applies a remote customer update atomically and records stale versions as conflicts", async () => {
  const store = new LocalOperationStore();
  const tenantId = "tenant-" + crypto.randomUUID();
  const branchId = "branch-" + crypto.randomUUID();
  const actorId = "actor-local-" + crypto.randomUUID();
  const created = await store.createCustomer({
    tenantId, branchId, actorId,
    customer: { code: "C-INBOUND-1", name: "Before" }
  });
  await store.acknowledge([{ operationId: created.originOperationId, sequence: 1 }]);

  const makeRow = async (sequence, operationId, baseVersion, name) => {
    const payloadJson = JSON.stringify({
      customerId: created.id, baseVersion, name,
      phone: null, email: null, address: null, notes: null
    });
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(payloadJson));
    const payloadSha256 = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
    return {
      tenantId, sequence, operationId, actorId: "actor-remote",
      deviceId: "device-remote", branchId,
      operationType: "customer.profile.updated", schemaVersion: 1,
      authorityEpoch: 0, occurredAtUtc: new Date().toISOString(),
      receivedAtUtc: new Date().toISOString(), payloadJson, payloadSha256
    };
  };

  const processor = new IncomingOperationProcessor({
    store, adapter: store, classify: classifyIncomingOperation
  });
  const update = await makeRow(2, "remote-update-1", 1, "Remote update");
  await store.storeIncomingPage(tenantId, [update], 2, branchId);
  const applied = await processor.processBatch(tenantId, 20, branchId, false);
  assert.equal(applied.applied, 1);
  let rows = await store.getCustomers(tenantId);
  let customer = rows.find(row => row.id === created.id);
  assert.equal(customer.version, 2);
  assert.equal(customer.name, "Remote update");

  const stale = await makeRow(3, "remote-update-stale", 1, "Stale overwrite");
  await store.storeIncomingPage(tenantId, [stale], 3, branchId);
  const blocked = await processor.processBatch(tenantId, 20, branchId, false);
  assert.equal(blocked.blockedByConflict, true);
  assert.equal(blocked.applied, 0);
  const conflicts = await store.getOpenConflicts(tenantId, branchId, false);
  assert.equal(conflicts.length, 1);
  assert.equal(conflicts[0].reason, "stale-customer-base-version");
  rows = await store.getCustomers(tenantId);
  customer = rows.find(row => row.id === created.id);
  assert.equal(customer.version, 2);
  assert.equal(customer.name, "Remote update");
});

test("acknowledged customer update journal echo does not reapply the originating device mutation", async () => {
  const store = new LocalOperationStore();
  const tenantId = "tenant-" + crypto.randomUUID();
  const branchId = "branch-" + crypto.randomUUID();
  const actorId = "actor-origin-" + crypto.randomUUID();
  const created = await store.createCustomer({
    tenantId, branchId, actorId,
    customer: { code: "C-ECHO-1", name: "Before" }
  });
  await store.acknowledge([{ operationId: created.originOperationId, sequence: 1 }]);
  const updated = await store.updateCustomer({
    tenantId, branchId, actorId, customerId: created.id,
    customer: { name: "Local edit" }
  });
  const deviceId = await store.getDeviceId(actorId);
  const pending = await store.getPending(tenantId, 10, branchId, false, deviceId);
  const operation = pending[0];
  await store.acknowledge([{ operationId: operation.operationId, sequence: 2 }]);

  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(operation.payloadJson));
  const payloadSha256 = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
  await store.storeIncomingPage(tenantId, [{
    tenantId, sequence: 2, operationId: operation.operationId,
    actorId, deviceId, branchId,
    operationType: operation.operationType, schemaVersion: 1, authorityEpoch: 0,
    occurredAtUtc: operation.occurredAtUtc, receivedAtUtc: new Date().toISOString(),
    payloadJson: operation.payloadJson, payloadSha256
  }], 2, branchId);

  const processor = new IncomingOperationProcessor({
    store, adapter: store, classify: classifyIncomingOperation
  });
  const result = await processor.processBatch(tenantId, 10, branchId, false);
  assert.equal(result.applied, 1);
  assert.equal(result.blockedByConflict, false);
  const rows = await store.getCustomers(tenantId);
  const customer = rows.find(row => row.id === created.id);
  assert.equal(customer.version, 2);
  assert.equal(customer.name, "Local edit");
  assert.equal((await store.getOpenConflicts(tenantId, branchId, false)).length, 0);
});

test("does not acknowledge an outbound customer update if its local version precondition changed", async () => {
  const store = new LocalOperationStore();
  const tenantId = "tenant-" + crypto.randomUUID();
  const branchId = "branch-" + crypto.randomUUID();
  const actorId = "actor-ack-" + crypto.randomUUID();
  const created = await store.createCustomer({
    tenantId, branchId, actorId,
    customer: { code: "C-ACK-1", name: "Before" }
  });
  await store.acknowledge([{ operationId: created.originOperationId, sequence: 1 }]);
  await store.updateCustomer({
    tenantId, branchId, actorId, customerId: created.id,
    customer: { name: "Pending edit" }
  });
  const deviceId = await store.getDeviceId(actorId);
  const pending = await store.getPending(tenantId, 10, branchId, false, deviceId);
  assert.equal(pending.length, 1);

  const database = await new Promise((resolve, reject) => {
    const request = indexedDB.open("mg-business-local");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const transaction = database.transaction("customers", "readwrite");
  const done = new Promise((resolve, reject) => {
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error);
  });
  const customers = transaction.objectStore("customers");
  const request = customers.get([tenantId, created.id]);
  request.onsuccess = () => {
    const row = request.result;
    row.version = 7;
    customers.put(row);
  };
  await done;
  database.close();

  await assert.rejects(
    store.acknowledge([{ operationId: pending[0].operationId, sequence: 2 }]),
    /version no longer matches/
  );
  const stillPending = await store.getPending(tenantId, 10, branchId, false, deviceId);
  assert.equal(stillPending.length, 1);
  const rows = await store.getCustomers(tenantId);
  const customer = rows.find(row => row.id === created.id);
  assert.equal(customer.syncStatus, "pending");
  assert.equal(customer.version, 7);
});

test("caches authoritative server customers for offline access without overwriting pending local edits", async () => {
  const store = new LocalOperationStore();
  const tenantId = "tenant-" + crypto.randomUUID();
  const branchId = "branch-" + crypto.randomUUID();
  const actorId = "actor-cache-" + crypto.randomUUID();
  const customerId = crypto.randomUUID();

  await store.cacheServerCustomers(tenantId, [{
    id: customerId, code: "SERVER-001", name: "Server Original",
    phone: "09120000000", email: "server@example.com",
    address: "First", notes: null, isActive: true, version: 5
  }]);
  let rows = await store.getCustomers(tenantId);
  let customer = rows.find(row => row.id === customerId);
  assert.equal(customer.version, 5);
  assert.equal(customer.syncStatus, "synced");
  assert.equal(customer.name, "Server Original");

  await store.updateCustomer({
    tenantId, branchId, actorId, customerId,
    customer: { name: "Local Offline Edit" }
  });
  await store.cacheServerCustomers(tenantId, [{
    id: customerId, code: "SERVER-001", name: "Server Newer",
    phone: "09129999999", email: "new@example.com",
    address: "Second", notes: "Server refresh", isActive: true, version: 6
  }]);

  rows = await store.getCustomers(tenantId);
  customer = rows.find(row => row.id === customerId);
  assert.equal(customer.version, 5);
  assert.equal(customer.syncStatus, "pending");
  assert.equal(customer.name, "Local Offline Edit");
  const deviceId = await store.getDeviceId(actorId);
  assert.equal((await store.getPending(tenantId, 10, branchId, false, deviceId)).length, 1);
});

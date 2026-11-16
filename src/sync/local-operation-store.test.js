import test from "node:test";
import assert from "node:assert/strict";
import { LocalOperationStore } from "./local-operation-store.js";



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
    payloadJson: "{\"name\":\"MG\"}",
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
  assert.deepEqual(calls.acknowledged, { deviceId: "device-1", sequence: 5 });
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
  assert.deepEqual(calls.acknowledged, { deviceId: "device-1", sequence: 8 });
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

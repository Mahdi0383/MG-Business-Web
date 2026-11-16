import test from "node:test";
import assert from "node:assert/strict";
import { LocalOperationStore } from "./local-operation-store.js";

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
    payloadJson: "{\"name\":\"MG\"}",
    payloadSha256: "A".repeat(64),
    schemaVersion: 1
  };
  const { agent, calls } = createSyncAgent({
    cursor: 1,
    page: { operations: [operation], nextSequence: 5, hasMore: false }
  });
  const result = await agent.pullIncoming("tenant-1");
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
  const result = await agent.pullIncoming("tenant-1");
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
        payloadJson: "{}",
        payloadSha256: "A".repeat(64),
        schemaVersion: 1
      }],
      nextSequence: 2,
      hasMore: false
    }
  });
  await assert.rejects(agent.pullIncoming("tenant-1"), /precedes the last returned operation/);
  assert.equal(calls.acknowledged, null);
});

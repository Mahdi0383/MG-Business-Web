import test from "node:test";
import assert from "node:assert/strict";
import { SyncAgent } from "./sync-agent.js";

test("pushes the durable pending batch and acknowledges only matching operation IDs", async () => {
  const sent = [];
  const acknowledged = [];
  const operations = [
    { operationId: "op-1", deviceId: "device-1", tenantId: "tenant-1", branchId: null,
      operationType: "test.created", schemaVersion: 1, authorityEpoch: 0,
      occurredAtUtc: "2026-11-16T10:00:00.000Z", payloadJson: "{\"value\":1}", payloadSha256: "A".repeat(64) }
  ];
  const api = {
    async post(path, body) {
      sent.push({ path, body });
      return { accepted: [{ operationId: "op-1", sequence: 17, duplicate: false }] };
    }
  };
  const store = {
    async getPending(tenantId, limit) { assert.equal(tenantId, "tenant-1"); assert.equal(limit, 100); return operations; },
    async acknowledge(items) { acknowledged.push(...items); }
  };

  const result = await new SyncAgent(api, store).pushPending("tenant-1");

  assert.deepEqual(result, { status: "synced", acknowledged: 1 });
  assert.equal(sent[0].path, "/api/sync/operations");
  assert.equal(sent[0].body.operations[0].operationId, "op-1");
  assert.equal(acknowledged[0].sequence, 17);
});

test("keeps pending operations untouched when the network is unavailable", async () => {
  let acknowledgeCalled = false;
  const api = { async post() { throw new TypeError("Failed to fetch"); } };
  const store = {
    async getPending() { return [{ operationId: "offline-op" }]; },
    async acknowledge() { acknowledgeCalled = true; }
  };

  const result = await new SyncAgent(api, store).pushPending("tenant-1");

  assert.equal(result.status, "offline");
  assert.equal(result.acknowledged, 0);
  assert.equal(acknowledgeCalled, false);
});

test("rejects acknowledgements that do not match the submitted batch", async () => {
  const api = { async post() { return { accepted: [{ operationId: "unexpected", sequence: 2 }] }; } };
  const store = {
    async getPending() { return [{ operationId: "expected" }]; },
    async acknowledge() { assert.fail("invalid acknowledgements must not be committed"); }
  };

  await assert.rejects(
    new SyncAgent(api, store).pushPending("tenant-1"),
    /acknowledgement does not match/
  );
});

test("pulls remote operations and persists the page with its cursor", async () => {
  const saved = [];
  const requests = [];
  const api = {
    async get(path) {
      requests.push(path);
      return {
        operations: [
          { sequence: 4, operationId: "remote-4", operationType: "test.created" },
          { sequence: 5, operationId: "remote-5", operationType: "test.updated" }
        ],
        nextSequence: 5,
        hasMore: false
      };
    }
  };
  const store = {
    async getCursor(tenantId) { assert.equal(tenantId, "tenant-1"); return 3; },
    async storeIncomingPage(tenantId, operations, cursor) { saved.push({ tenantId, operations, cursor }); }
  };

  const result = await new SyncAgent(api, store).pullIncoming("tenant-1");

  assert.equal(result.status, "pulled");
  assert.equal(result.received, 2);
  assert.equal(result.nextSequence, 5);
  assert.equal(requests[0], "/api/sync/operations?afterSequence=3&limit=100");
  assert.equal(saved[0].cursor, 5);
  assert.equal(saved[0].operations.length, 2);
});

test("does not persist a non-monotonic pull page", async () => {
  let persisted = false;
  const api = {
    async get() {
      return {
        operations: [{ sequence: 7, operationId: "seven" }, { sequence: 6, operationId: "six" }],
        nextSequence: 6,
        hasMore: false
      };
    }
  };
  const store = {
    async getCursor() { return 5; },
    async storeIncomingPage() { persisted = true; }
  };

  await assert.rejects(new SyncAgent(api, store).pullIncoming("tenant-1"), /non-monotonic/);
  assert.equal(persisted, false);
});

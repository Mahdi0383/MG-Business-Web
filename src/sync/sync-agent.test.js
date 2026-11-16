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

import test from "node:test";
import assert from "node:assert/strict";
import { IncomingOperationProcessor } from "./incoming-operation-processor.js";

function setup(rows) {
  const calls = [];
  const store = { async getUnappliedIncoming(_tenantId, limit) { return rows.slice(0, limit); } };
  const adapter = {
    async readConflictState() { return { version: 4 }; },
    async recordConflict(value) { calls.push(["conflict", value.sequence]); },
    async applyAndAcknowledge(value) { calls.push(["apply", value.sequence, value.operation.payload]); }
  };
  const classify = operation => operation.operationType === "customer.profile.updated"
    ? { disposition: "apply", reason: "version-matches" }
    : { disposition: "manual-review", reason: "sensitive-operation" };
  return { processor: new IncomingOperationProcessor({ store, adapter, classify }), calls };
}

test("applies eligible incoming operations through the atomic adapter contract", async () => {
  const { processor, calls } = setup([{
    sequence: 1, operationId: "op-1", operationType: "customer.profile.updated",
    payloadJson: JSON.stringify({ name: "MG" })
  }]);
  const result = await processor.processBatch("tenant-1");
  assert.equal(result.applied, 1);
  assert.deepEqual(calls, [["apply", 1, { name: "MG" }]]);
});

test("records sensitive operations as conflicts without applying them", async () => {
  const { processor, calls } = setup([{
    sequence: 2, operationId: "op-2", operationType: "accounting.entry.posted",
    payloadJson: JSON.stringify({ amount: 100 })
  }]);
  const result = await processor.processBatch("tenant-1");
  assert.equal(result.applied, 0);
  assert.equal(result.conflicts.length, 1);
  assert.deepEqual(calls, [["conflict", 2]]);
});

test("stops on adapter failure and does not process later operations", async () => {
  const rows = [1, 2].map(sequence => ({
    sequence, operationId: `op-${sequence}`,
    operationType: "customer.profile.updated",
    payloadJson: JSON.stringify({ name: "MG" })
  }));
  const calls = [];
  const adapter = {
    async readConflictState() { return { version: 1 }; },
    async recordConflict() {},
    async applyAndAcknowledge({ sequence }) {
      calls.push(sequence);
      if (sequence === 1) throw new Error("durable transaction failed");
    }
  };
  const processor = new IncomingOperationProcessor({
    store: { async getUnappliedIncoming() { return rows; } },
    adapter,
    classify: () => ({ disposition: "apply", reason: "test" })
  });
  await assert.rejects(processor.processBatch("tenant-1"), /durable transaction failed/);
  assert.deepEqual(calls, [1]);
});

test("rejects incomplete dependencies", () => {
  assert.throws(() => new IncomingOperationProcessor({}), /required/);
});

test("stops ordered replay at the first unresolved conflict", async () => {
  const rows = [
    { sequence: 2, operationId: "op-2", operationType: "customer.profile.updated",
      payloadJson: JSON.stringify({ name: "must-wait" }) },
    { sequence: 1, operationId: "op-1", operationType: "accounting.entry.posted",
      payloadJson: JSON.stringify({ amount: 100 }) }
  ];
  const calls = [];
  const adapter = {
    async readConflictState() { return { version: 1 }; },
    async recordConflict({ sequence }) { calls.push(["conflict", sequence]); },
    async applyAndAcknowledge({ sequence }) { calls.push(["apply", sequence]); }
  };
  const processor = new IncomingOperationProcessor({
    store: { async getUnappliedIncoming() { return rows; } },
    adapter,
    classify: operation => operation.operationType.startsWith("accounting.")
      ? { disposition: "manual-review", reason: "sensitive-operation" }
      : { disposition: "apply", reason: "eligible" }
  });

  const result = await processor.processBatch("tenant-1");

  assert.equal(result.applied, 0);
  assert.deepEqual(calls, [["conflict", 1]]);
  assert.equal(result.results[0].sequence, 1);
});

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
    sequence: 1, schemaVersion: 1, operationId: "op-1", operationType: "customer.profile.updated",
    payloadJson: JSON.stringify({ name: "MG" })
  }]);
  const result = await processor.processBatch("tenant-1", 100, null, true);
  assert.equal(result.applied, 1);
  assert.equal(result.processed, 1);
  assert.equal(result.blockedByConflict, false);
  assert.deepEqual(calls, [["apply", 1, { name: "MG" }]]);
});

test("records sensitive operations as conflicts without applying them", async () => {
  const { processor, calls } = setup([{
    sequence: 2, schemaVersion: 1, operationId: "op-2", operationType: "accounting.entry.posted",
    payloadJson: JSON.stringify({ amount: 100 })
  }]);
  const result = await processor.processBatch("tenant-1", 100, null, true);
  assert.equal(result.applied, 0);
  assert.equal(result.conflicts.length, 1);
  assert.deepEqual(calls, [["conflict", 2]]);
});

test("stops on adapter failure and does not process later operations", async () => {
  const rows = [1, 2].map(sequence => ({
    sequence, schemaVersion: 1, operationId: `op-${sequence}`,
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
  await assert.rejects(processor.processBatch("tenant-1", 100, null, true), /durable transaction failed/);
  assert.deepEqual(calls, [1]);
});

test("rejects incomplete dependencies", () => {
  assert.throws(() => new IncomingOperationProcessor({}), /required/);
});

test("stops ordered replay at the first unresolved conflict", async () => {
  const rows = [
    { sequence: 2, schemaVersion: 1, operationId: "op-2", operationType: "customer.profile.updated",
      payloadJson: JSON.stringify({ name: "must-wait" }) },
    { sequence: 1, schemaVersion: 1, operationId: "op-1", operationType: "accounting.entry.posted",
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

  const result = await processor.processBatch("tenant-1", 100, null, true);

  assert.equal(result.applied, 0);
  assert.equal(result.processed, 1);
  assert.equal(result.blockedByConflict, true);
  assert.deepEqual(calls, [["conflict", 1]]);
  assert.equal(result.results[0].sequence, 1);
});

test("passes the active branch into the durable inbox query", async () => {
  let query;
  const processor = new IncomingOperationProcessor({
    store: {
      async getUnappliedIncoming(...args) { query = args; return []; }
    },
    adapter: {
      async readConflictState() { return null; },
      async recordConflict() {},
      async applyAndAcknowledge() {}
    },
    classify: () => ({ disposition: "apply", reason: "test" })
  });

  await processor.processBatch("tenant-1", 40, "branch-a");
  assert.deepEqual(query, ["tenant-1", 40, "branch-a", false]);
});

test("rejects invalid branch scope before reading the inbox", async () => {
  let queried = false;
  const processor = new IncomingOperationProcessor({
    store: { async getUnappliedIncoming() { queried = true; return []; } },
    adapter: {
      async readConflictState() { return null; },
      async recordConflict() {},
      async applyAndAcknowledge() {}
    },
    classify: () => ({ disposition: "apply", reason: "test" })
  });

  await assert.rejects(processor.processBatch("tenant-1", 100, " "), /branchId/);
  assert.equal(queried, false);
});

test("refuses to replay an inbox row from another branch", async () => {
  let applied = false;
  const processor = new IncomingOperationProcessor({
    store: {
      async getUnappliedIncoming() {
        return [{
          sequence: 1,
          branchId: "branch-b",
          schemaVersion: 1,
          operationId: "foreign-op",
          operationType: "customer.profile.updated",
          payloadJson: JSON.stringify({ name: "foreign" })
        }];
      }
    },
    adapter: {
      async readConflictState() { return { version: 1 }; },
      async recordConflict() {},
      async applyAndAcknowledge() { applied = true; }
    },
    classify: () => ({ disposition: "apply", reason: "test" })
  });

  await assert.rejects(
    processor.processBatch("tenant-1", 100, "branch-a"),
    /outside the active branch/
  );
  assert.equal(applied, false);
});


test("does not query a tenant-wide inbox without explicit manager authority", async () => {
  let queried = false;
  const processor = new IncomingOperationProcessor({
    store: { async getUnappliedIncoming() { queried = true; return []; } },
    adapter: {
      async readConflictState() { return null; },
      async recordConflict() {},
      async applyAndAcknowledge() {}
    },
    classify: () => ({ disposition: "apply", reason: "test" })
  });

  const result = await processor.processBatch("tenant-1");
  assert.equal(result.received, 0);
  assert.equal(queried, false);
});


test("rejects malformed or non-object persisted payloads before touching projections", async () => {
  let projectionRead = false;
  let applied = false;
  const processor = new IncomingOperationProcessor({
    store: {
      async getUnappliedIncoming() {
        return [{
          sequence: 1,
          branchId: "branch-a",
          schemaVersion: 1,
          operationId: "invalid-payload",
          operationType: "customer.profile.updated",
          payloadJson: "[]"
        }];
      }
    },
    adapter: {
      async readConflictState() { projectionRead = true; return null; },
      async recordConflict() {},
      async applyAndAcknowledge() { applied = true; }
    },
    classify: () => ({ disposition: "apply" })
  });

  await assert.rejects(
    processor.processBatch("tenant-1", 100, "branch-a"),
    /payload must be a JSON object/
  );
  assert.equal(projectionRead, false);
  assert.equal(applied, false);
});

test("rejects malformed JSON persisted in the incoming inbox", async () => {
  const processor = new IncomingOperationProcessor({
    store: {
      async getUnappliedIncoming() {
        return [{
          sequence: 1,
          branchId: "branch-a",
          schemaVersion: 1,
          operationId: "malformed-payload",
          operationType: "customer.profile.updated",
          payloadJson: "{"
        }];
      }
    },
    adapter: {
      async readConflictState() { throw new Error("must not read projections"); },
      async recordConflict() {},
      async applyAndAcknowledge() {}
    },
    classify: () => ({ disposition: "apply" })
  });

  await assert.rejects(
    processor.processBatch("tenant-1", 100, "branch-a"),
    /malformed JSON payload/
  );
});


test("rejects invalid classifier decisions without acknowledging the operation", async () => {
  let applied = false;
  const processor = new IncomingOperationProcessor({
    store: { async getUnappliedIncoming() { return [{
      sequence: 1, schemaVersion: 1, operationId: "op-invalid-decision",
      operationType: "customer.profile.updated", payloadJson: "{}"
    }]; } },
    adapter: {
      async readConflictState() { return null; },
      async recordConflict() {},
      async applyAndAcknowledge() { applied = true; }
    },
    classify: () => ({ disposition: "skip-and-continue" })
  });
  await assert.rejects(processor.processBatch("tenant-1", 10, null, true), /invalid decision/);
  assert.equal(applied, false);
});

test("rejects oversized persisted payloads before reading projection state", async () => {
  let projectionRead = false;
  const processor = new IncomingOperationProcessor({
    store: { async getUnappliedIncoming() { return [{
      sequence: 1, schemaVersion: 1, operationId: "op-oversized",
      operationType: "customer.profile.updated", payloadJson: JSON.stringify({ value: "x".repeat(65 * 1024) })
    }]; } },
    adapter: {
      async readConflictState() { projectionRead = true; return null; },
      async recordConflict() {},
      async applyAndAcknowledge() {}
    },
    classify: () => ({ disposition: "apply", reason: "test" })
  });
  await assert.rejects(processor.processBatch("tenant-1", 10, null, true), /invalid or non-monotonic/);
  assert.equal(projectionRead, false);
});

test("requires all transactional adapter methods at construction", () => {
  assert.throws(() => new IncomingOperationProcessor({
    store: { async getUnappliedIncoming() { return []; } },
    adapter: { async applyAndAcknowledge() {} },
    classify: () => ({ disposition: "apply", reason: "test" })
  }), /complete transactional adapter/);
});


test("rejects tenant-wide rows returned by an untrusted store for a branch member", async () => {
  const { processor, calls } = setup([{
    tenantId: "tenant-1",
    branchId: null,
    sequence: 1,
    schemaVersion: 1,
    operationId: "tenant-wide",
    operationType: "customer.profile.updated",
    payloadJson: JSON.stringify({ name: "MG" })
  }]);
  await assert.rejects(
    processor.processBatch("tenant-1", 100, "branch-a", false),
    /tenant-wide operation without manager authority/
  );
  assert.deepEqual(calls, []);
});

test("rejects inbox rows belonging to another tenant before reading or applying", async () => {
  const { processor, calls } = setup([{
    tenantId: "tenant-2",
    branchId: "branch-a",
    sequence: 1,
    schemaVersion: 1,
    operationId: "foreign-tenant",
    operationType: "customer.profile.updated",
    payloadJson: JSON.stringify({ name: "MG" })
  }]);
  await assert.rejects(
    processor.processBatch("tenant-1", 100, "branch-a", false),
    /outside the active tenant/
  );
  assert.deepEqual(calls, []);
});

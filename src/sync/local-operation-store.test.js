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

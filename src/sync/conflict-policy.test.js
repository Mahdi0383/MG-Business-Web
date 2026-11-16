import test from "node:test";
import assert from "node:assert/strict";
import { classifyIncomingOperation } from "./conflict-policy.js";

test("routes financial and inventory operations to manual review", () => {
  for (const operationType of [
    "accounting.entry.posted",
    "sales.invoice.created",
    "payments.received",
    "inventory.stock-moved",
    "game-session.settled",
    "cafe.order.completed",
    "purchase.invoice.posted"
  ]) {
    const result = classifyIncomingOperation({ operationId: "op-1", operationType, schemaVersion: 1 });
    assert.equal(result.disposition, "manual-review", operationType);
    assert.equal(result.reason, "business-integrity-sensitive", operationType);
  }
});

test("applies customer updates only when the base version matches or the operation is its own echo", () => {
  const operation = {
    operationId: "profile-update-1",
    operationType: "customer.profile.updated",
    schemaVersion: 1,
    payload: { customerId: "customer-1", baseVersion: 8 }
  };
  const current = { version: 8, lastOperationId: "previous-operation" };
  assert.equal(classifyIncomingOperation(operation, current).disposition, "apply");
  assert.equal(classifyIncomingOperation(operation, current).reason, "customer-version-matches");
  assert.equal(classifyIncomingOperation(operation, {
    version: 9, lastOperationId: "profile-update-1"
  }).reason, "originating-device-echo");
  const stale = classifyIncomingOperation(operation, { version: 9, lastOperationId: "another-operation" });
  assert.equal(stale.disposition, "conflict");
  assert.equal(stale.reason, "stale-customer-base-version");
});


test("fails closed for unknown commands and invalid customer update versions", () => {
  const unknown = classifyIncomingOperation({
    operationId: "unknown-1",
    operationType: "custom.unregistered-command",
    schemaVersion: 1
  });
  assert.equal(unknown.disposition, "manual-review");
  assert.equal(unknown.reason, "unknown-operation-type");

  const missingVersion = classifyIncomingOperation({
    operationId: "profile-3",
    operationType: "customer.profile.updated",
    schemaVersion: 1,
    payload: {}
  }, { version: 1 });
  assert.equal(missingVersion.disposition, "manual-review");
  assert.equal(missingVersion.reason, "invalid-customer-update-version-precondition");
});

test("rejects malformed operation envelopes", () => {
  assert.throws(() => classifyIncomingOperation(null), /operation is required/);
  assert.throws(() => classifyIncomingOperation({ operationType: "x" }), /schema version/);
});


test("applies a customer create only when the ID is absent or it is the originating-device echo", () => {
  const operation = {
    operationId: "customer-create-1",
    operationType: "customer.profile.created",
    schemaVersion: 1,
    payload: { customerId: "customer-1", baseVersion: 0 }
  };
  assert.equal(classifyIncomingOperation(operation, null).disposition, "apply");
  assert.equal(classifyIncomingOperation(operation, {
    version: 1, originOperationId: "customer-create-1"
  }).reason, "originating-device-echo");
  const duplicate = classifyIncomingOperation(operation, {
    version: 1, originOperationId: "another-operation"
  });
  assert.equal(duplicate.disposition, "conflict");
  assert.equal(duplicate.reason, "customer-already-exists");
});

test("routes customer create commands with a non-zero base version to manual review", () => {
  const result = classifyIncomingOperation({
    operationId: "customer-create-invalid",
    operationType: "customer.profile.created",
    schemaVersion: 1,
    payload: { baseVersion: 1 }
  }, null);
  assert.equal(result.disposition, "manual-review");
  assert.equal(result.reason, "invalid-create-version-precondition");
});

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

test("allows a profile operation only when its base version matches", () => {
  const result = classifyIncomingOperation({
    operationId: "profile-1",
    operationType: "customer.profile.updated",
    schemaVersion: 1,
    payload: { baseVersion: 8 }
  }, { version: 8 });

  assert.equal(result.disposition, "apply");
  assert.equal(result.expectedVersion, 8);
});

test("detects concurrent profile edits instead of overwriting newer state", () => {
  const result = classifyIncomingOperation({
    operationId: "profile-2",
    operationType: "customer.contact.updated",
    schemaVersion: 1,
    payload: { baseVersion: 8 }
  }, { version: 9 });

  assert.equal(result.disposition, "conflict");
  assert.equal(result.reason, "concurrent-profile-edit");
  assert.equal(result.actualVersion, 9);
});

test("fails closed for unknown commands and missing version preconditions", () => {
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
  assert.equal(missingVersion.reason, "missing-version-precondition");
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

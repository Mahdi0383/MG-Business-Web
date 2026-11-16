const REVIEW_REQUIRED_PREFIXES = [
  "accounting.",
  "sales.",
  "payments.",
  "inventory.",
  "stock.",
  "game-session.",
  "cafe.order.",
  "purchase."
];



/**
 * Classifies an incoming operation before it is applied to local business data.
 * This function deliberately does not mutate projections or resolve financial
 * conflicts. Unknown commands fail closed and require explicit review.
 */
export function classifyIncomingOperation(operation, currentState = null) {
  if (!operation || typeof operation !== "object")
    throw new TypeError("An incoming operation is required.");

  const type = operation.operationType;
  if (typeof type !== "string" || type.length === 0)
    throw new TypeError("Incoming operation type is required.");

  if (!Number.isInteger(operation.schemaVersion) || operation.schemaVersion < 1)
    throw new TypeError("A valid operation schema version is required.");

  if (type === "inventory.stock.movement.created") {
    const payload = operation.payload;
    const valid = operation.schemaVersion === 1
      && typeof operation.branchId === "string" && operation.branchId.length > 0
      && typeof payload?.productId === "string" && payload.productId.length > 0
      && Number.isInteger(Number(payload.type)) && Number(payload.type) >= 1 && Number(payload.type) <= 7
      && Number.isFinite(Number(payload.quantity)) && Number(payload.quantity) > 0
      && Math.round(Number(payload.quantity) * 1000) === Number(payload.quantity) * 1000
      && Number.isFinite(Number(payload.baseQuantityOnHand)) && Number(payload.baseQuantityOnHand) >= 0
      && Number.isFinite(Number(payload.baseInventoryValue)) && Number(payload.baseInventoryValue) >= 0
      && Math.round(Number(payload.baseInventoryValue) * 100) === Number(payload.baseInventoryValue) * 100
      && (payload.reference == null || (typeof payload.reference === "string" && payload.reference.length <= 250))
      && ([1, 5, 6].includes(Number(payload.type))
        ? Number.isFinite(Number(payload.unitCost)) && Number(payload.unitCost) >= 0
          && Math.round(Number(payload.unitCost) * 100) === Number(payload.unitCost) * 100
        : payload.unitCost == null);
    if (!valid) {
      return {
        disposition: "manual-review",
        reason: "invalid-inventory-movement-envelope",
        operationId: operation.operationId ?? null
      };
    }
    if (!currentState) {
      return {
        disposition: "manual-review",
        reason: "inventory-baseline-not-cached",
        operationId: operation.operationId ?? null
      };
    }
    if (currentState.lastOperationId === operation.operationId) {
      return {
        disposition: "apply",
        reason: "originating-device-inventory-echo",
        operationId: operation.operationId ?? null
      };
    }
    const baseValue = Math.round(Number(payload.baseInventoryValue) * 100) / 100;
    if (Number(currentState.quantityOnHand) === Number(payload.baseQuantityOnHand)
      && Number(currentState.inventoryValue) === baseValue) {
      return {
        disposition: "apply",
        reason: "inventory-baseline-matches",
        operationId: operation.operationId ?? null
      };
    }
    return {
      disposition: "conflict",
      reason: "stale-inventory-base-balance",
      expectedQuantityOnHand: Number(payload.baseQuantityOnHand),
      actualQuantityOnHand: Number(currentState.quantityOnHand),
      expectedInventoryValue: baseValue,
      actualInventoryValue: Number(currentState.inventoryValue),
      operationId: operation.operationId ?? null
    };
  }

  if (REVIEW_REQUIRED_PREFIXES.some(prefix => type.startsWith(prefix))) {
    return {
      disposition: "manual-review",
      reason: "business-integrity-sensitive",
      operationId: operation.operationId ?? null
    };
  }

  if (type === "customer.profile.created") {
    const baseVersion = operation.payload?.baseVersion;
    if (baseVersion !== 0) {
      return {
        disposition: "manual-review",
        reason: "invalid-create-version-precondition",
        operationId: operation.operationId ?? null
      };
    }
    if (!currentState) {
      return {
        disposition: "apply",
        reason: "customer-does-not-exist",
        expectedVersion: 0,
        operationId: operation.operationId ?? null
      };
    }
    if (currentState.originOperationId === operation.operationId) {
      return {
        disposition: "apply",
        reason: "originating-device-echo",
        expectedVersion: 0,
        operationId: operation.operationId ?? null
      };
    }
    return {
      disposition: "conflict",
      reason: "customer-already-exists",
      actualVersion: currentState.version ?? null,
      operationId: operation.operationId ?? null
    };
  }

  if (type === "customer.profile.updated") {
    const baseVersion = operation.payload?.baseVersion;
    if (!Number.isSafeInteger(baseVersion) || baseVersion < 1) {
      return {
        disposition: "manual-review",
        reason: "invalid-customer-update-version-precondition",
        operationId: operation.operationId ?? null
      };
    }
    if (currentState?.lastOperationId === operation.operationId) {
      return {
        disposition: "apply",
        reason: "originating-device-echo",
        expectedVersion: baseVersion,
        operationId: operation.operationId ?? null
      };
    }
    if (currentState && currentState.version === baseVersion) {
      return {
        disposition: "apply",
        reason: "customer-version-matches",
        expectedVersion: baseVersion,
        operationId: operation.operationId ?? null
      };
    }
    return {
      disposition: "conflict",
      reason: "stale-customer-base-version",
      expectedVersion: baseVersion,
      actualVersion: currentState?.version ?? null,
      operationId: operation.operationId ?? null
    };
  }

  if (type === "customer.profile.deactivated") {
    const baseVersion = operation.payload?.baseVersion;
    if (!Number.isSafeInteger(baseVersion) || baseVersion < 1) {
      return {
        disposition: "manual-review",
        reason: "invalid-customer-deactivation-version-precondition",
        operationId: operation.operationId ?? null
      };
    }
    if (currentState?.lastOperationId === operation.operationId) {
      return {
        disposition: "apply",
        reason: "originating-device-echo",
        expectedVersion: baseVersion,
        operationId: operation.operationId ?? null
      };
    }
    if (currentState && currentState.version === baseVersion && currentState.isActive !== false) {
      return {
        disposition: "apply",
        reason: "customer-version-matches",
        expectedVersion: baseVersion,
        operationId: operation.operationId ?? null
      };
    }
    return {
      disposition: "conflict",
      reason: currentState?.isActive === false
        ? "customer-already-deactivated"
        : "stale-customer-base-version",
      expectedVersion: baseVersion,
      actualVersion: currentState?.version ?? null,
      operationId: operation.operationId ?? null
    };
  }

  if (type.startsWith("customer.profile.") || type.startsWith("customer.contact.")) {
    return {
      disposition: "manual-review",
      reason: "customer-update-command-not-enabled",
      operationId: operation.operationId ?? null
    };
  }

  return {
    disposition: "manual-review",
    reason: "unknown-operation-type",
    operationId: operation.operationId ?? null
  };
}

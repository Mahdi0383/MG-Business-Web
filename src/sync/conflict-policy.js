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

const FIELD_MERGE_PREFIXES = [
  "customer.profile.",
  "customer.contact."
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

  if (REVIEW_REQUIRED_PREFIXES.some(prefix => type.startsWith(prefix))) {
    return {
      disposition: "manual-review",
      reason: "business-integrity-sensitive",
      operationId: operation.operationId ?? null
    };
  }

  if (FIELD_MERGE_PREFIXES.some(prefix => type.startsWith(prefix))) {
    const payload = operation.payload && typeof operation.payload === "object"
      ? operation.payload
      : null;
    const baseVersion = payload?.baseVersion;
    const currentVersion = currentState?.version;

    if (!Number.isSafeInteger(baseVersion) || !Number.isSafeInteger(currentVersion)) {
      return {
        disposition: "manual-review",
        reason: "missing-version-precondition",
        operationId: operation.operationId ?? null
      };
    }

    if (baseVersion !== currentVersion) {
      return {
        disposition: "conflict",
        reason: "concurrent-profile-edit",
        expectedVersion: baseVersion,
        actualVersion: currentVersion,
        operationId: operation.operationId ?? null
      };
    }

    return {
      disposition: "apply",
      reason: "profile-version-matches",
      expectedVersion: baseVersion,
      operationId: operation.operationId ?? null
    };
  }

  return {
    disposition: "manual-review",
    reason: "unknown-operation-type",
    operationId: operation.operationId ?? null
  };
}

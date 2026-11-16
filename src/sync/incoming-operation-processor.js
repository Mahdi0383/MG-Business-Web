/**
 * Applies already validated incoming commands in sequence order.
 *
 * The adapter must implement each projection update and inbox acknowledgement
 * in one durable transaction. This module intentionally refuses to mark an
 * operation applied unless the adapter confirms that atomic transaction.
 */
export class IncomingOperationProcessor {
  constructor({ store, adapter, classify }) {
    if (!store || !adapter || typeof adapter.applyAndAcknowledge !== "function" ||
        typeof classify !== "function") {
      throw new TypeError("store, transactional adapter and classifier are required.");
    }
    this.store = store;
    this.adapter = adapter;
    this.classify = classify;
  }

  async processBatch(tenantId, limit = 100, branchId = null) {
    if (typeof tenantId !== "string" || !tenantId.trim()) throw new TypeError("tenantId is required.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    const boundedLimit = Math.max(1, Math.min(Number.isInteger(limit) ? limit : 100, 200));
    const rows = await this.store.getUnappliedIncoming(tenantId, boundedLimit, branchId);
    const orderedRows = [...rows].sort((left, right) => left.sequence - right.sequence);
    const results = [];
    let previousSequence = 0;

    for (const row of orderedRows) {
      if (branchId !== null && row.branchId != null && row.branchId !== branchId)
        throw new Error("Incoming inbox returned an operation outside the active branch.");
      if (!Number.isSafeInteger(row.sequence) || row.sequence <= previousSequence
        || typeof row.operationId !== "string" || !row.operationId
        || typeof row.operationType !== "string" || !row.operationType
        || !Number.isInteger(row.schemaVersion) || row.schemaVersion < 1
        || typeof row.payloadJson !== "string")
        throw new TypeError("Incoming inbox contains an invalid or non-monotonic operation envelope.");
      previousSequence = row.sequence;

      const currentState = await this.adapter.readConflictState(tenantId, row);
      const decision = this.classify({
        ...row,
        payload: JSON.parse(row.payloadJson)
      }, currentState);

      if (decision.disposition !== "apply") {
        await this.adapter.recordConflict({
          tenantId,
          sequence: row.sequence,
          operationId: row.operationId,
          disposition: decision.disposition,
          reason: decision.reason,
          details: decision
        });
        results.push({ sequence: row.sequence, ...decision });
        // A later operation may depend on this unresolved state. Do not apply
        // subsequent journal entries out of order until the conflict is resolved.
        break;
      }

      // Contract: projection mutation + durable inbox acknowledgement + conflict
      // state update must commit atomically in the adapter's local database.
      await this.adapter.applyAndAcknowledge({
        tenantId,
        sequence: row.sequence,
        operation: { ...row, payload: JSON.parse(row.payloadJson) },
        decision
      });
      results.push({ sequence: row.sequence, disposition: "applied" });
    }

    return {
      tenantId,
      received: orderedRows.length,
      processed: results.length,
      blockedByConflict: results.some(item => item.disposition !== "applied"),
      applied: results.filter(item => item.disposition === "applied").length,
      conflicts: results.filter(item => item.disposition !== "applied"),
      results
    };
  }
}

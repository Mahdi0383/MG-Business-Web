/**
 * Applies already validated incoming commands in sequence order.
 *
 * The adapter must implement each projection update and inbox acknowledgement
 * in one durable transaction. This module intentionally refuses to mark an
 * operation applied unless the adapter confirms that atomic transaction.
 */
export class IncomingOperationProcessor {
  constructor({ store, adapter, classify }) {
    if (!store || typeof store.getUnappliedIncoming !== "function" || !adapter
      || typeof adapter.readConflictState !== "function"
      || typeof adapter.recordConflict !== "function"
      || typeof adapter.applyAndAcknowledge !== "function"
      || typeof classify !== "function") {
      throw new TypeError("store, complete transactional adapter and classifier are required.");
    }
    this.store = store;
    this.adapter = adapter;
    this.classify = classify;
  }

  async processBatch(tenantId, limit = 100, branchId = null, allowTenantWide = false) {
    if (typeof tenantId !== "string" || !tenantId.trim()) throw new TypeError("tenantId is required.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    if (typeof allowTenantWide !== "boolean")
      throw new TypeError("allowTenantWide must be a boolean.");
    if (branchId === null && !allowTenantWide)
      return { tenantId, received: 0, processed: 0, blockedByConflict: false, applied: 0, conflicts: [], results: [] };
    const boundedLimit = Math.max(1, Math.min(Number.isInteger(limit) ? limit : 100, 200));
    const rows = await this.store.getUnappliedIncoming(tenantId, boundedLimit, branchId, allowTenantWide);
    const orderedRows = [...rows].sort((left, right) => left.sequence - right.sequence);
    const results = [];
    let previousSequence = 0;

    for (const row of orderedRows) {
      if (row.tenantId != null && row.tenantId !== tenantId)
        throw new Error("Incoming inbox returned an operation outside the active tenant.");
      if (row.branchId == null && !allowTenantWide)
        throw new Error("Incoming inbox returned a tenant-wide operation without manager authority.");
      if (branchId !== null && row.branchId != null && row.branchId !== branchId)
        throw new Error("Incoming inbox returned an operation outside the active branch.");
      if (!Number.isSafeInteger(row.sequence) || row.sequence <= previousSequence
        || typeof row.operationId !== "string" || !row.operationId.trim()
        || typeof row.operationType !== "string" || !row.operationType.trim()
        || (row.branchId != null && (typeof row.branchId !== "string" || !row.branchId.trim()))
        || !Number.isInteger(row.schemaVersion) || row.schemaVersion < 1
        || typeof row.payloadJson !== "string"
        || new TextEncoder().encode(row.payloadJson).byteLength > 64 * 1024)
        throw new TypeError("Incoming inbox contains an invalid or non-monotonic operation envelope.");
      previousSequence = row.sequence;

      let payload;
      try {
        payload = JSON.parse(row.payloadJson);
      } catch {
        throw new TypeError("Incoming inbox contains malformed JSON payload data.");
      }
      if (!payload || typeof payload !== "object" || Array.isArray(payload))
        throw new TypeError("Incoming inbox payload must be a JSON object.");

      const currentState = await this.adapter.readConflictState(tenantId, row);
      const decision = this.classify({ ...row, payload }, currentState);
      if (!decision || typeof decision !== "object" || Array.isArray(decision)
        || !["apply", "conflict", "manual-review"].includes(decision.disposition)
        || typeof decision.reason !== "string" || !decision.reason.trim())
        throw new TypeError("Incoming operation classifier returned an invalid decision.");

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
        operation: { ...row, payload },
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

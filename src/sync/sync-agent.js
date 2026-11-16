export class SyncAgent {
  constructor(apiClient, operationStore) {
    this.apiClient = apiClient;
    this.operationStore = operationStore;
    this.running = false;
  }

  async pullIncoming(tenantId, { pageSize = 100, branchId = null } = {}) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required for sync pull.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    const boundedPageSize = Number.isInteger(pageSize) ? Math.max(1, Math.min(pageSize, 200)) : 100;
    if (this.running) return { status: "already-running", received: 0 };
    this.running = true;
    try {
      const afterSequence = await this.operationStore.getCursor(tenantId, branchId);
      const response = await this.apiClient.get(
        `/api/sync/operations?afterSequence=${afterSequence}&limit=${boundedPageSize}`
      );
      if (!Array.isArray(response?.operations) || !Number.isSafeInteger(response?.nextSequence) || response.nextSequence < afterSequence)
        throw new Error("MG Sync API returned an invalid pull page.");
      let previousSequence = afterSequence;
      for (const operation of response.operations) {
        if (!Number.isSafeInteger(operation.sequence) || operation.sequence <= previousSequence)
          throw new Error("MG Sync API returned a non-monotonic operation sequence.");
        previousSequence = operation.sequence;
      }
      // A branch-scoped pull may omit other branches' rows while still advancing
      // over them in the tenant journal. The scan cursor can exceed the last visible row.
      if (response.nextSequence < previousSequence)
        throw new Error("MG Sync API cursor precedes the last returned operation.");
      await this.operationStore.storeIncomingPage(tenantId, response.operations, response.nextSequence, branchId);
      const deviceId = await this.operationStore.getDeviceId();
      await this.apiClient.post("/api/sync/acknowledgements", {
        deviceId,
        sequence: response.nextSequence
      });
      return { status: response.hasMore ? "more-available" : "pulled", received: response.operations.length, nextSequence: response.nextSequence, hasMore: Boolean(response.hasMore) };
    } catch (error) {
      if (error instanceof TypeError || error?.name === "NetworkError") return { status: "offline", received: 0, error };
      throw error;
    } finally {
      this.running = false;
    }
  }
  async pushPending(tenantId, { batchSize = 100 } = {}) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required for sync push.");
    const boundedBatchSize = Number.isInteger(batchSize) ? Math.max(1, Math.min(batchSize, 100)) : 100;
    if (this.running) return { status: "already-running", acknowledged: 0 };
    this.running = true;
    try {
      const pending = await this.operationStore.getPending(tenantId, boundedBatchSize);
      if (pending.length === 0) return { status: "idle", acknowledged: 0 };

      const response = await this.apiClient.post("/api/sync/operations", {
        operations: pending.map(operation => ({
          operationId: operation.operationId,
          deviceId: operation.deviceId,
          branchId: operation.branchId,
          operationType: operation.operationType,
          schemaVersion: operation.schemaVersion,
          authorityEpoch: operation.authorityEpoch,
          occurredAtUtc: operation.occurredAtUtc,
          payloadJson: operation.payloadJson,
          payloadSha256: operation.payloadSha256
        }))
      });

      const acknowledgements = response?.accepted;
      if (!Array.isArray(acknowledgements))
        throw new Error("MG Sync API returned an invalid acknowledgement.");

      const expected = new Set(pending.map(operation => operation.operationId));
      const acknowledgedIds = new Set(acknowledgements.map(item => item?.operationId));
      if (acknowledgements.length !== pending.length
        || acknowledgedIds.size !== acknowledgements.length
        || acknowledgements.some(item => !expected.has(item?.operationId)
          || !Number.isSafeInteger(item?.sequence) || item.sequence < 1))
        throw new Error("MG Sync API acknowledgement does not match the submitted batch.");

      await this.operationStore.acknowledge(acknowledgements);
      return { status: "synced", acknowledged: acknowledgements.length };
    } catch (error) {
      if (error instanceof TypeError || error?.name === "NetworkError")
        return { status: "offline", acknowledged: 0, error };
      throw error;
    } finally {
      this.running = false;
    }
  }
}

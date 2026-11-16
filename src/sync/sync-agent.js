export class SyncAgent {
  constructor(apiClient, operationStore) {
    this.apiClient = apiClient;
    this.operationStore = operationStore;
    this.running = false;
  }

  async pushPending(tenantId, { batchSize = 100 } = {}) {
    if (this.running) return { status: "already-running", acknowledged: 0 };
    this.running = true;
    try {
      const pending = await this.operationStore.getPending(tenantId, batchSize);
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

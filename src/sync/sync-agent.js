function isNetworkFailure(error) {
  if (error?.name === "NetworkError") return true;
  return error instanceof TypeError
    && /fetch|network|load failed|connection/i.test(error.message || "");
}

export class SyncAgent {
  constructor(apiClient, operationStore) {
    this.apiClient = apiClient;
    this.operationStore = operationStore;
    this.runningPull = false;
    this.runningPush = false;
  }

  async pullIncoming(tenantId, { pageSize = 100, branchId = null, actorId } = {}) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required for sync pull.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    if (typeof actorId !== "string" || !actorId.trim())
      throw new TypeError("actorId is required for sync pull.");
    const boundedPageSize = Number.isInteger(pageSize) ? Math.max(1, Math.min(pageSize, 200)) : 100;
    if (this.runningPull) return { status: "already-running", received: 0 };
    this.runningPull = true;
    try {
      const afterSequence = await this.operationStore.getCursor(tenantId, branchId);
      const response = await this.apiClient.get(
        `/api/sync/operations?afterSequence=${afterSequence}&limit=${boundedPageSize}`,
        { tenantId, branchId, actorId }
      );
      if (!Array.isArray(response?.operations)
        || response.operations.length > boundedPageSize
        || !Number.isSafeInteger(response?.nextSequence)
        || response.nextSequence < afterSequence
        || typeof response.hasMore !== "boolean"
        || (response.hasMore && response.nextSequence <= afterSequence))
        throw new Error("MG Sync API returned an invalid pull page.");
      let previousSequence = afterSequence;
      for (const operation of response.operations) {
        if (!operation || typeof operation !== "object" || Array.isArray(operation)
          || !Number.isSafeInteger(operation.sequence) || operation.sequence <= previousSequence)
          throw new Error("MG Sync API returned a non-monotonic operation sequence.");
        if (operation.branchId != null
          && (typeof operation.branchId !== "string" || !operation.branchId.trim()))
          throw new Error("MG Sync API returned an invalid branch identity.");
        if (branchId !== null && operation.branchId != null && operation.branchId !== branchId)
          throw new Error("MG Sync API returned an operation outside the active branch.");
        if (typeof operation.operationId !== "string" || !operation.operationId.trim()
          || typeof operation.operationType !== "string" || !operation.operationType.trim()
          || typeof operation.payloadJson !== "string"
          || new TextEncoder().encode(operation.payloadJson).byteLength > 64 * 1024
          || typeof operation.payloadSha256 !== "string" || !/^[0-9a-f]{64}$/i.test(operation.payloadSha256)
          || !Number.isInteger(operation.schemaVersion) || operation.schemaVersion < 1)
          throw new Error("MG Sync API returned an invalid operation envelope.");
        let payload;
        try { payload = JSON.parse(operation.payloadJson); }
        catch { throw new Error("MG Sync API returned invalid JSON in an operation payload."); }
        if (!payload || typeof payload !== "object" || Array.isArray(payload))
          throw new Error("MG Sync API returned a non-object operation payload.");
        if (!globalThis.crypto?.subtle)
          throw new Error("Secure cryptography is unavailable; incoming sync payloads cannot be verified.");
        const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(operation.payloadJson));
        const actualHash = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
        if (actualHash !== operation.payloadSha256.toLowerCase())
          throw new Error("MG Sync API returned an operation whose payload hash does not match.");
        previousSequence = operation.sequence;
      }
      // A branch-scoped pull may omit other branches' rows while still advancing
      // over them in the tenant journal. The scan cursor can exceed the last visible row.
      if (response.nextSequence < previousSequence)
        throw new Error("MG Sync API cursor precedes the last returned operation.");
      await this.operationStore.storeIncomingPage(tenantId, response.operations, response.nextSequence, branchId);
      const deviceId = await this.operationStore.getDeviceId(actorId);
      await this.apiClient.post("/api/sync/acknowledgements", {
        deviceId,
        sequence: response.nextSequence,
        branchId
      }, { tenantId, branchId, actorId });
      return { status: response.hasMore ? "more-available" : "pulled", received: response.operations.length, nextSequence: response.nextSequence, hasMore: Boolean(response.hasMore) };
    } catch (error) {
      if (isNetworkFailure(error)) return { status: "offline", received: 0, error };
      throw error;
    } finally {
      this.runningPull = false;
    }
  }
  async pushPending(tenantId, { batchSize = 100, branchId = null, allowTenantWide = false, actorId } = {}) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required for sync push.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    if (typeof allowTenantWide !== "boolean")
      throw new TypeError("allowTenantWide must be a boolean.");
    if (typeof actorId !== "string" || !actorId.trim())
      throw new TypeError("actorId is required for sync push.");
    const boundedBatchSize = Number.isInteger(batchSize) ? Math.max(1, Math.min(batchSize, 100)) : 100;
    if (this.runningPush) return { status: "already-running", acknowledged: 0 };
    this.runningPush = true;
    try {
      const deviceId = await this.operationStore.getDeviceId(actorId);
      const pending = await this.operationStore.getPending(tenantId, boundedBatchSize, branchId, allowTenantWide, deviceId);
      if (pending.length === 0) return { status: "idle", acknowledged: 0 };

      // Never transmit a locally persisted command whose identity, scope or
      // payload integrity no longer matches the durable envelope.
      for (const operation of pending) {
        if (!operation || typeof operation !== "object" || Array.isArray(operation)
          || operation.tenantId !== tenantId
          || operation.deviceId !== deviceId
          || (operation.branchId != null && (typeof operation.branchId !== "string" || !operation.branchId.trim()))
          || (branchId !== null && operation.branchId !== branchId
            && !(allowTenantWide && operation.branchId == null))
          || typeof operation.operationId !== "string" || !operation.operationId.trim()
          || typeof operation.operationType !== "string" || !operation.operationType.trim()
          || !Number.isInteger(operation.schemaVersion) || operation.schemaVersion < 1
          || operation.authorityEpoch !== 0
          || typeof operation.occurredAtUtc !== "string" || !Number.isFinite(Date.parse(operation.occurredAtUtc))
          || typeof operation.payloadJson !== "string"
          || new TextEncoder().encode(operation.payloadJson).byteLength > 64 * 1024
          || typeof operation.payloadSha256 !== "string" || !/^[0-9a-f]{64}$/i.test(operation.payloadSha256))
          throw new Error("Local sync queue contains an invalid or unauthorized operation envelope.");
        let payload;
        try { payload = JSON.parse(operation.payloadJson); }
        catch { throw new Error("Local sync queue contains malformed JSON payload data."); }
        if (!payload || typeof payload !== "object" || Array.isArray(payload))
          throw new Error("Local sync queue payload must be a JSON object.");
        if (!globalThis.crypto?.subtle)
          throw new Error("Secure cryptography is unavailable; outgoing sync payloads cannot be verified.");
        const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(operation.payloadJson));
        const actualHash = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
        if (actualHash !== operation.payloadSha256.toLowerCase())
          throw new Error("Local sync queue payload hash does not match its persisted content.");
      }

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
      }, { tenantId, branchId });

      const acknowledgements = response?.accepted;
      if (!Array.isArray(acknowledgements))
        throw new Error("MG Sync API returned an invalid acknowledgement.");

      const expected = new Set(pending.map(operation => operation.operationId));
      const acknowledgedIds = new Set(acknowledgements.map(item => item?.operationId));
      let previousAcknowledgedSequence = 0;
      const acknowledgementOrderInvalid = acknowledgements.some((item, index) => {
        const valid = item?.operationId === pending[index]?.operationId
          && expected.has(item.operationId)
          && Number.isSafeInteger(item.sequence)
          && item.sequence > previousAcknowledgedSequence;
        if (valid) previousAcknowledgedSequence = item.sequence;
        return !valid;
      });
      if (acknowledgements.length !== pending.length
        || acknowledgedIds.size !== acknowledgements.length
        || acknowledgementOrderInvalid)
        throw new Error("MG Sync API acknowledgement does not match the submitted batch or journal order.");

      await this.operationStore.acknowledge(acknowledgements);
      return { status: "synced", acknowledged: acknowledgements.length };
    } catch (error) {
      if (isNetworkFailure(error))
        return { status: "offline", acknowledged: 0, error };
      throw error;
    } finally {
      this.runningPush = false;
    }
  }
}

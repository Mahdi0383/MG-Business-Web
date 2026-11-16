import test from "node:test";
import assert from "node:assert/strict";
import { SyncAgent } from "./sync-agent.js";

test("pushes the durable pending batch and acknowledges only matching operation IDs", async () => {
  const sent = [];
  const acknowledged = [];
  const operations = [
    { operationId: "op-1", deviceId: "device-1", tenantId: "tenant-1", branchId: null,
      operationType: "test.created", schemaVersion: 1, authorityEpoch: 0,
      occurredAtUtc: "2026-11-16T10:00:00.000Z", payloadJson: "{\"value\":1}", payloadSha256: "48208f9428d64634bd8e28ff345bf0eab60d53c18fa2fbdb0b9bc1e84df2b5f6" }
  ];
  const api = {
    async post(path, body) {
      sent.push({ path, body });
      return { accepted: [{ operationId: "op-1", sequence: 17, duplicate: false }] };
    }
  };
  const store = {
    async getDeviceId(actorId) { assert.equal(actorId, "actor-1"); return "device-1"; },
    async getPending(tenantId, limit, branchId, allowTenantWide, deviceId) { assert.equal(tenantId, "tenant-1"); assert.equal(limit, 100); assert.equal(deviceId, "device-1"); return operations; },
    async acknowledge(items) { acknowledged.push(...items); }
  };

  const result = await new SyncAgent(api, store).pushPending("tenant-1", { actorId: "actor-1" });

  assert.deepEqual(result, { status: "synced", acknowledged: 1 });
  assert.equal(sent[0].path, "/api/sync/operations");
  assert.equal(sent[0].body.operations[0].operationId, "op-1");
  assert.equal(acknowledged[0].sequence, 17);
});

test("keeps pending operations untouched when the network is unavailable", async () => {
  let acknowledgeCalled = false;
  const api = { async post() { throw new TypeError("Failed to fetch"); } };
  const store = {
    async getDeviceId(actorId) { assert.equal(actorId, "actor-1"); return "device-1"; },
    async getPending(tenantId, limit, branchId, allowTenantWide, deviceId) { assert.equal(deviceId, "device-1"); return [{ operationId: "offline-op", tenantId: "tenant-1", deviceId: "device-1", branchId: null, operationType: "test.created", schemaVersion: 1, authorityEpoch: 0, occurredAtUtc: "2026-11-16T10:00:00.000Z", payloadJson: "{}", payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a" }]; },
    async acknowledge() { acknowledgeCalled = true; }
  };

  const result = await new SyncAgent(api, store).pushPending("tenant-1", { actorId: "actor-1" });

  assert.equal(result.status, "offline");
  assert.equal(result.acknowledged, 0);
  assert.equal(acknowledgeCalled, false);
});

test("rejects acknowledgements that do not match the submitted batch", async () => {
  const api = { async post() { return { accepted: [{ operationId: "unexpected", sequence: 2 }] }; } };
  const store = {
    async getDeviceId(actorId) { assert.equal(actorId, "actor-1"); return "device-1"; },
    async getPending(tenantId, limit, branchId, allowTenantWide, deviceId) { assert.equal(deviceId, "device-1"); return [{ operationId: "expected", tenantId: "tenant-1", deviceId: "device-1", branchId: null, operationType: "test.created", schemaVersion: 1, authorityEpoch: 0, occurredAtUtc: "2026-11-16T10:00:00.000Z", payloadJson: "{}", payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a" }]; },
    async acknowledge() { assert.fail("invalid acknowledgements must not be committed"); }
  };

  await assert.rejects(
    new SyncAgent(api, store).pushPending("tenant-1", { actorId: "actor-1" }),
    /acknowledgement does not match/
  );
});

test("pulls remote operations and persists the page with its cursor", async () => {
  const saved = [];
  const requests = [];
  const acknowledgements = [];
  const api = {
    async get(path) {
      requests.push(path);
      return {
        operations: [
          { sequence: 4, operationId: "remote-4", operationType: "test.created", schemaVersion: 1, payloadJson: "{}", payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a" },
          { sequence: 5, operationId: "remote-5", operationType: "test.updated", schemaVersion: 1, payloadJson: "{}", payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a" }
        ],
        nextSequence: 5,
        hasMore: false
      };
    },
    async post(path, body) { acknowledgements.push({ path, body }); return { sequence: body.sequence }; }
  };
  const store = {
    async getCursor(tenantId) { assert.equal(tenantId, "tenant-1"); return 3; },
    async getDeviceId(actorId) { assert.equal(actorId, "actor-1"); return "device-1"; },
    async storeIncomingPage(tenantId, operations, cursor) { saved.push({ tenantId, operations, cursor }); }
  };

  const result = await new SyncAgent(api, store).pullIncoming("tenant-1", { actorId: "actor-1" });

  assert.equal(result.status, "pulled");
  assert.equal(result.received, 2);
  assert.equal(result.nextSequence, 5);
  assert.equal(requests[0], "/api/sync/operations?afterSequence=3&limit=100");
  assert.equal(saved[0].cursor, 5);
  assert.equal(saved[0].operations.length, 2);
  assert.equal(acknowledgements[0].path, "/api/sync/acknowledgements");
  assert.deepEqual(acknowledgements[0].body, { deviceId: "device-1", sequence: 5, branchId: null });
});

test("does not persist a non-monotonic pull page", async () => {
  let persisted = false;
  const api = {
    async get() {
      return {
        operations: [
          { sequence: 7, operationId: "seven", operationType: "test.created", schemaVersion: 1, payloadJson: "{}", payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a" },
          { sequence: 6, operationId: "six", operationType: "test.created", schemaVersion: 1, payloadJson: "{}", payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a" }
        ],
        nextSequence: 6,
        hasMore: false
      };
    },
    async post() { assert.fail("invalid pull pages must not be acknowledged"); }
  };
  const store = {
    async getCursor() { return 5; },
    async storeIncomingPage() { persisted = true; }
  };

  await assert.rejects(new SyncAgent(api, store).pullIncoming("tenant-1", { actorId: "actor-1" }), /non-monotonic/);
  assert.equal(persisted, false);
});

test("uses an independent pull cursor for each selected branch", async () => {
  const cursorReads = [];
  const saved = [];
  const api = {
    async get() {
      return { operations: [], nextSequence: 12, hasMore: false };
    },
    async post() { return {}; }
  };
  const store = {
    async getCursor(tenantId, branchId) {
      cursorReads.push({ tenantId, branchId });
      return branchId === "branch-a" ? 4 : 9;
    },
    async getDeviceId(actorId) { assert.equal(actorId, "actor-1"); return "device-1"; },
    async storeIncomingPage(tenantId, operations, sequence, branchId) {
      saved.push({ tenantId, sequence, branchId });
    }
  };

  const agent = new SyncAgent(api, store);
  await agent.pullIncoming("tenant-1", { branchId: "branch-a", actorId: "actor-1" });
  await agent.pullIncoming("tenant-1", { branchId: "branch-b", actorId: "actor-1" });

  assert.deepEqual(cursorReads, [
    { tenantId: "tenant-1", branchId: "branch-a" },
    { tenantId: "tenant-1", branchId: "branch-b" }
  ]);
  assert.deepEqual(saved, [
    { tenantId: "tenant-1", sequence: 12, branchId: "branch-a" },
    { tenantId: "tenant-1", sequence: 12, branchId: "branch-b" }
  ]);
});

test("pushes only the active branch queue with an explicit tenant context", async () => {
  let pendingContext;
  let requestContext;
  const operation = {
    operationId: "branch-op",
    tenantId: "tenant-1",
    deviceId: "device-1",
    branchId: "branch-a",
    operationType: "customer.profile.updated",
    schemaVersion: 1,
    authorityEpoch: 0,
    occurredAtUtc: "2026-11-16T10:00:00.000Z",
    payloadJson: "{}",
    payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
  };
  const api = {
    async post(path, body, options) {
      requestContext = { path, options, operation: body.operations[0] };
      return { accepted: [{ operationId: "branch-op", sequence: 22, duplicate: false }] };
    }
  };
  const store = {
    async getDeviceId(actorId) { assert.equal(actorId, "actor-1"); return "device-1"; },
    async getPending(tenantId, limit, branchId, allowTenantWide, deviceId) {
      pendingContext = { tenantId, limit, branchId, allowTenantWide, deviceId };
      return [operation];
    },
    async acknowledge() {}
  };

  const result = await new SyncAgent(api, store).pushPending("tenant-1", { branchId: "branch-a", actorId: "actor-1" });
  assert.equal(result.status, "synced");
  assert.deepEqual(pendingContext, {
    tenantId: "tenant-1",
    limit: 100,
    branchId: "branch-a",
    allowTenantWide: false,
    deviceId: "device-1"
  });
  assert.equal(requestContext.path, "/api/sync/operations");
  assert.deepEqual(requestContext.options, { tenantId: "tenant-1", branchId: "branch-a" });
  assert.equal(requestContext.operation.branchId, "branch-a");
});

test("rejects a pull response containing another branch's operation", async () => {
  let persisted = false;
  const api = {
    async get() {
      return {
        operations: [{
          sequence: 4,
          operationId: "foreign-branch-operation",
          branchId: "branch-b",
          operationType: "customer.updated",
          schemaVersion: 1,
          payloadJson: "{}",
          payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        }],
        nextSequence: 4,
        hasMore: false
      };
    },
    async post() { assert.fail("cross-branch data must not be acknowledged"); }
  };
  const store = {
    async getCursor() { return 3; },
    async storeIncomingPage() { persisted = true; }
  };

  await assert.rejects(
    new SyncAgent(api, store).pullIncoming("tenant-1", { branchId: "branch-a", actorId: "actor-1" }),
    /outside the active branch/
  );
  assert.equal(persisted, false);
});

test("rejects malformed incoming operation envelopes before persistence", async () => {
  let persisted = false;
  const api = {
    async get() {
      return {
        operations: [{
          sequence: 4,
          operationId: "malformed-operation",
          operationType: "customer.updated",
          schemaVersion: 1
        }],
        nextSequence: 4,
        hasMore: false
      };
    },
    async post() { assert.fail("malformed data must not be acknowledged"); }
  };
  const store = {
    async getCursor() { return 3; },
    async storeIncomingPage() { persisted = true; }
  };

  await assert.rejects(
    new SyncAgent(api, store).pullIncoming("tenant-1", { actorId: "actor-1" }),
    /invalid operation envelope/
  );
  assert.equal(persisted, false);
});

test("does not misreport local validation errors as network outages", async () => {
  const api = {
    async get() {
      return { operations: [], nextSequence: 4, hasMore: false };
    },
    async post() { assert.fail("invalid local state must not be acknowledged"); }
  };
  const store = {
    async getCursor() { return 3; },
    async storeIncomingPage() { throw new TypeError("tenantId is required."); }
  };

  await assert.rejects(
    new SyncAgent(api, store).pullIncoming("tenant-1", { actorId: "actor-1" }),
    /tenantId is required/
  );
});

test("rejects malformed branch identities and hashes before inbox persistence", async () => {
  let persisted = false;
  const api = {
    async get() {
      return {
        operations: [{
          sequence: 4,
          operationId: "bad-envelope",
          branchId: 42,
          operationType: "customer.updated",
          schemaVersion: 1,
          payloadJson: "{}",
          payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        }],
        nextSequence: 4,
        hasMore: false
      };
    },
    async post() { assert.fail("invalid pull page must not be acknowledged"); }
  };
  const store = {
    async getCursor() { return 3; },
    async storeIncomingPage() { persisted = true; }
  };

  await assert.rejects(
    new SyncAgent(api, store).pullIncoming("tenant-1", { actorId: "actor-1" }),
    /invalid branch identity/
  );
  assert.equal(persisted, false);
});

test("requires an authenticated actor before starting sync pull", async () => {
  const api = { async get() { assert.fail("missing actor must be rejected before network access"); } };
  const store = { async getCursor() { assert.fail("missing actor must be rejected before storage access"); } };

  await assert.rejects(
    new SyncAgent(api, store).pullIncoming("tenant-1"),
    /actorId is required/
  );
});

test("rejects an incoming payload whose SHA-256 does not match before persistence", async () => {
  let persisted = false;
  const api = {
    async get() {
      return {
        operations: [{
          sequence: 1,
          operationId: "tampered-op",
          operationType: "customer.updated",
          schemaVersion: 1,
          payloadJson: JSON.stringify({ name: "tampered" }),
          payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        }],
        nextSequence: 1,
        hasMore: false
      };
    }
  };
  const store = {
    async getCursor() { return 0; },
    async storeIncomingPage() { persisted = true; }
  };

  await assert.rejects(
    new SyncAgent(api, store).pullIncoming("tenant-1", { actorId: "actor-1" }),
    /payload hash does not match/
  );
  assert.equal(persisted, false);
});

test("requires an authenticated actor before starting sync push", async () => {
  const api = { async post() { assert.fail("missing actor must be rejected before network access"); } };
  const store = { async getPending() { assert.fail("missing actor must be rejected before storage access"); } };
  await assert.rejects(new SyncAgent(api, store).pushPending("tenant-1"), /actorId is required/);
});


test("acknowledges a pulled page within the same branch cursor scope", async () => {
  let acknowledgement = null;
  const api = {
    async get() { return { operations: [], nextSequence: 7, hasMore: false }; },
    async post(path, body) {
      assert.equal(path, "/api/sync/acknowledgements");
      acknowledgement = body;
      return {};
    }
  };
  const store = {
    async getCursor() { return 3; },
    async storeIncomingPage() {},
    async getDeviceId() { return "device-1"; }
  };

  const result = await new SyncAgent(api, store).pullIncoming("tenant-1", {
    branchId: "branch-a",
    actorId: "actor-1"
  });
  assert.equal(result.status, "pulled");
  assert.deepEqual(acknowledgement, {
    deviceId: "device-1",
    sequence: 7,
    branchId: "branch-a"
  });
});


test("refuses to transmit a locally tampered pending payload", async () => {
  let transmitted = false;
  let acknowledged = false;
  const api = { async post() { transmitted = true; return { accepted: [] }; } };
  const store = {
    async getDeviceId() { return "device-1"; },
    async getPending() {
      return [{
        operationId: "tampered-outgoing",
        tenantId: "tenant-1",
        deviceId: "device-1",
        branchId: null,
        operationType: "test.created",
        schemaVersion: 1,
        authorityEpoch: 0,
        occurredAtUtc: "2026-11-16T10:00:00.000Z",
        payloadJson: JSON.stringify({ changed: true }),
        payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
      }];
    },
    async acknowledge() { acknowledged = true; }
  };

  await assert.rejects(
    new SyncAgent(api, store).pushPending("tenant-1", { actorId: "actor-1" }),
    /payload hash does not match/
  );
  assert.equal(transmitted, false);
  assert.equal(acknowledged, false);
});

test("refuses to transmit a pending operation belonging to another device", async () => {
  let transmitted = false;
  const api = { async post() { transmitted = true; return { accepted: [] }; } };
  const store = {
    async getDeviceId() { return "device-1"; },
    async getPending() {
      return [{
        operationId: "foreign-device",
        tenantId: "tenant-1",
        deviceId: "device-2",
        branchId: null,
        operationType: "test.created",
        schemaVersion: 1,
        authorityEpoch: 0,
        occurredAtUtc: "2026-11-16T10:00:00.000Z",
        payloadJson: "{}",
        payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
      }];
    },
    async acknowledge() { assert.fail("foreign-device operations must not be acknowledged"); }
  };

  await assert.rejects(
    new SyncAgent(api, store).pushPending("tenant-1", { actorId: "actor-1" }),
    /invalid or unauthorized operation envelope/
  );
  assert.equal(transmitted, false);
});


test("runs pull and push independently without starving inbound sync", async () => {
  let releasePush;
  let pullCompleted = false;
  const pushGate = new Promise(resolve => { releasePush = resolve; });
  const api = {
    async get() {
      pullCompleted = true;
      return { operations: [], nextSequence: 0, hasMore: false };
    },
    async post(path) {
      if (path === "/api/sync/operations") {
        await pushGate;
        return { accepted: [{ operationId: "pending-op", sequence: 1, duplicate: false }] };
      }
      return {};
    }
  };
  const store = {
    async getCursor() { return 0; },
    async getDeviceId() { return "device-1"; },
    async getPending() {
      return [{
        operationId: "pending-op", tenantId: "tenant-1", deviceId: "device-1", branchId: null,
        operationType: "test.created", schemaVersion: 1, authorityEpoch: 0,
        occurredAtUtc: "2026-11-16T10:00:00.000Z", payloadJson: "{}",
        payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
      }];
    },
    async storeIncomingPage() {},
    async acknowledge() {}
  };
  const agent = new SyncAgent(api, store);
  const pushPromise = agent.pushPending("tenant-1", { actorId: "actor-1" });
  await Promise.resolve();
  const pullResult = await agent.pullIncoming("tenant-1", { actorId: "actor-1" });
  assert.equal(pullCompleted, true);
  assert.equal(pullResult.status, "pulled");
  releasePush();
  const pushResult = await pushPromise;
  assert.equal(pushResult.status, "synced");
});


test("coalesces duplicate pull requests while one pull is in flight", async () => {
  let release;
  let requests = 0;
  const gate = new Promise(resolve => { release = resolve; });
  const agent = new SyncAgent({
    async get() { requests += 1; await gate; return { operations: [], nextSequence: 0, hasMore: false }; },
    async post() { return {}; }
  }, {
    async getCursor() { return 0; },
    async getDeviceId() { return "device-1"; },
    async storeIncomingPage() {}
  });
  const first = agent.pullIncoming("tenant-1", { actorId: "actor-1" });
  await Promise.resolve();
  const second = await agent.pullIncoming("tenant-1", { actorId: "actor-1" });
  assert.deepEqual(second, { status: "already-running", received: 0 });
  assert.equal(requests, 1);
  release();
  assert.equal((await first).status, "pulled");
});

test("coalesces duplicate push requests while one push is in flight", async () => {
  let release;
  let markStarted;
  let requests = 0;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { markStarted = resolve; });
  const operation = {
    operationId: "pending-op", tenantId: "tenant-1", deviceId: "device-1", branchId: null,
    operationType: "test.created", schemaVersion: 1, authorityEpoch: 0,
    occurredAtUtc: "2026-11-16T10:00:00.000Z", payloadJson: "{}",
    payloadSha256: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
  };
  const agent = new SyncAgent({
    async post(path) {
      if (path === "/api/sync/operations") {
        requests += 1;
        markStarted();
        await gate;
        return { accepted: [{ operationId: "pending-op", sequence: 1 }] };
      }
      return {};
    }
  }, {
    async getDeviceId() { return "device-1"; },
    async getPending() { return [operation]; },
    async acknowledge() {}
  });
  const first = agent.pushPending("tenant-1", { actorId: "actor-1" });
  await started;
  const second = await agent.pushPending("tenant-1", { actorId: "actor-1" });
  assert.deepEqual(second, { status: "already-running", acknowledged: 0 });
  assert.equal(requests, 1);
  release();
  assert.equal((await first).status, "synced");
});


test("rejects a non-advancing cursor when the server claims more journal pages exist", async () => {
  const calls = { stored: null, acknowledged: null };
  const agent = new SyncAgent({
    async get() { return { operations: [], nextSequence: 5, hasMore: true }; },
    async post(_path, body) { calls.acknowledged = body; return {}; }
  }, {
    async getCursor() { return 5; },
    async getDeviceId() { return "device-1"; },
    async storeIncomingPage(...args) { calls.stored = args; }
  });
  await assert.rejects(
    agent.pullIncoming("tenant-1", { actorId: "actor-1" }),
    /invalid pull page/
  );
  assert.equal(calls.stored, null);
  assert.equal(calls.acknowledged, null);
});


test("rejects duplicate or out-of-order server journal sequences without acknowledging commands", async () => {
  const pending = ["op-a", "op-b"].map((operationId, index) => {
    const payloadJson = JSON.stringify({ index });
    return {
      operationId, deviceId: "device-1", tenantId: "tenant-1", branchId: null,
      operationType: "test.created", schemaVersion: 1, authorityEpoch: 0,
      occurredAtUtc: "2026-11-16T10:00:00.000Z", payloadJson,
      payloadSha256: index === 0
        ? "ffbf81d654b1b5c8d67a4459f25003c9434e10d05ffea0708e825ece8f9976c6"
        : "29219e41f7d3be520df73c53a60c5b096e3984e06f48d6048c5cd44077c27380"
    };
  });
  let acknowledged = false;
  const api = {
    async post() {
      return { accepted: [
        { operationId: "op-a", sequence: 9, duplicate: false },
        { operationId: "op-b", sequence: 9, duplicate: false }
      ] };
    }
  };
  const store = {
    async getDeviceId() { return "device-1"; },
    async getPending() { return pending; },
    async acknowledge() { acknowledged = true; }
  };
  const agent = new SyncAgent(api, store);
  await assert.rejects(agent.pushPending("tenant-1", { actorId: "actor-1" }), /journal order/);
  assert.equal(acknowledged, false);
});

import test from "node:test";
import assert from "node:assert/strict";
import { ApiError, isNetworkUnavailable, MgApiClient } from "./client.js";

const originalFetch = globalThis.fetch;

function response(status, body) {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

test.afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("sends bearer token and selected tenant/branch context", async () => {
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return response(200, [{ id: "p-1" }]);
  };

  const client = new MgApiClient("https://api.example.test/");
  client.setSession({ accessToken: "access-token" });
  client.setTenantContext("tenant-1", "branch-1");

  const result = await client.get("/api/inventory/products");

  assert.deepEqual(result, [{ id: "p-1" }]);
  assert.equal(request.url, "https://api.example.test/api/inventory/products");
  assert.equal(request.options.headers.get("Authorization"), "Bearer access-token");
  assert.equal(request.options.headers.get("X-MG-Tenant"), "tenant-1");
  assert.equal(request.options.headers.get("X-MG-Branch"), "branch-1");
});

test("refreshes an expired access token once and retries the request", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, authorization: options.headers.get("Authorization") });
    if (url.endsWith("/auth/refresh"))
      return response(200, { accessToken: "new-access", refreshToken: "new-refresh" });
    if (calls.filter(call => call.url.endsWith("/api/context")).length === 1)
      return response(401, { title: "Unauthorized" });
    return response(200, { tenantId: "tenant-1" });
  };

  const client = new MgApiClient("https://api.example.test");
  client.setSession({ accessToken: "old-access", refreshToken: "refresh-1" });
  const changes = [];
  client.onSessionChanged = session => changes.push(session);
  const result = await client.get("/api/context");

  assert.deepEqual(result, { tenantId: "tenant-1" });
  assert.equal(calls.length, 3);
  assert.equal(calls[0].authorization, "Bearer old-access");
  assert.equal(calls[1].url, "https://api.example.test/auth/refresh");
  assert.equal(calls[2].authorization, "Bearer new-access");
  assert.equal(client.session.refreshToken, "new-refresh");
  assert.equal(changes.length, 1);
});

test("does not retry a failed refresh and clears the session", async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return response(401, { title: "Unauthorized" });
  };

  const client = new MgApiClient("https://api.example.test");
  client.setSession({ accessToken: "expired", refreshToken: "invalid" });

  await assert.rejects(client.get("/api/context"), error => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 401);
    return true;
  });
  assert.equal(calls, 2);
  assert.equal(client.session, null);
});

test("serializes JSON request bodies and surfaces API validation messages", async () => {
  let request;
  globalThis.fetch = async (url, options) => {
    request = options;
    return response(400, { errors: { Name: ["نام الزامی است."] } });
  };

  const client = new MgApiClient("https://api.example.test");
  await assert.rejects(client.post("/api/customers", { name: " " }), error => {
    assert.equal(error.message, "نام الزامی است.");
    assert.equal(error.status, 400);
    return true;
  });
  assert.equal(request.headers.get("Content-Type"), "application/json");
  assert.deepEqual(JSON.parse(request.body), { name: " " });
});

test("pins a request to its captured branch even after the shared client context changes", async () => {
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push(options.headers);
    return response(200, { ok: true });
  };

  const client = new MgApiClient("https://api.example.test");
  client.setTenantContext("tenant-1", "branch-b");
  await client.get("/api/sync/operations", { tenantId: "tenant-1", branchId: "branch-a" });
  await client.get("/api/sync/operations", { tenantId: "tenant-1", branchId: null });

  assert.equal(requests[0].get("X-MG-Branch"), "branch-a");
  assert.equal(requests[1].get("X-MG-Branch"), "");
  assert.equal(requests[1].get("X-MG-Tenant"), "tenant-1");
});


test("classifies fetch failures as offline without hiding HTTP errors", () => {
  assert.equal(isNetworkUnavailable(new TypeError("Failed to fetch")), true);
  assert.equal(isNetworkUnavailable(new ApiError(503, "Service unavailable")), false);
  assert.equal(isNetworkUnavailable(new Error("Unexpected application error")), false);
});

test("signs sync requests using the exact serialized body and selected tenant branch", async () => {
  let signed;
  let sent;
  const client = new MgApiClient("https://api.example.test");
  client.setTenantContext("tenant-default", "branch-default");
  client.setDeviceSigner({
    async createRequestHeaders(input) {
      signed = input;
      return { "X-MG-Device-Signature": "signed-proof" };
    }
  });
  globalThis.fetch = async (url, options) => {
    sent = { url, options };
    return response(200, { ok: true });
  };

  await client.post("/api/sync/operations?limit=5", { operations: [] }, {
    tenantId: "tenant-1",
    branchId: "branch-1",
    actorId: "actor-1"
  });

  assert.equal(signed.method, "POST");
  assert.equal(signed.path, "/api/sync/operations?limit=5");
  assert.equal(signed.tenantId, "tenant-1");
  assert.equal(signed.branchId, "branch-1");
  assert.equal(signed.actorId, "actor-1");
  assert.equal(signed.bodyJson, '{"operations":[]}');
  assert.equal(sent.options.headers.get("X-MG-Device-Signature"), "signed-proof");
  assert.equal(sent.options.body, signed.bodyJson);
});

test("does not require a device signature for enrollment bootstrap routes", async () => {
  let signed = false;
  const client = new MgApiClient("https://api.example.test");
  client.setDeviceSigner({ async createRequestHeaders() { signed = true; return {}; } });
  globalThis.fetch = async () => response(200, { ok: true });
  await client.post("/api/sync/enrollment/challenges", { deviceId: "device-1" });
  assert.equal(signed, false);
});

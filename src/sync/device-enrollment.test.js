import "fake-indexeddb/auto";
import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { DeviceEnrollmentClient } from "./device-enrollment.js";

if (!globalThis.crypto) globalThis.crypto = webcrypto;

test("enrolls once, persists a non-extractable key and signs canonical sync requests", async () => {
  const actorId = "actor-1";
  const tenantId = "tenant-1";
  const branchId = "branch-1";
  const deviceId = "device-1";
  const calls = [];
  const api = {
    async post(path, body, options) {
      calls.push({ path, body, options });
      if (path.endsWith("/challenges")) return {
        challengeId: "challenge-1",
        challenge: Buffer.alloc(32, 7).toString("base64"),
        tenantId,
        branchId,
        deviceId,
        actorId
      };
      return { deviceId, keyVersion: 1, credential: Buffer.alloc(32, 9).toString("base64") };
    }
  };
  const store = { async getDeviceId(value) { assert.equal(value, actorId); return deviceId; } };
  const enrollment = new DeviceEnrollmentClient(api, store);
  const first = await enrollment.ensureEnrolled({ tenantId, branchId, actorId });
  const second = await enrollment.ensureEnrolled({ tenantId, branchId, actorId });
  assert.equal(first.deviceId, deviceId);
  assert.equal(second.deviceId, deviceId);
  assert.equal(first.privateKey.extractable, false);
  assert.equal(calls.filter(call => call.path.endsWith("/challenges")).length, 1);

  const headers = await enrollment.createRequestHeaders({
    method: "POST",
    path: "/api/sync/operations",
    tenantId,
    branchId,
    actorId,
    bodyJson: '{"operations":[]}'
  });
  assert.equal(headers["X-MG-Device-Id"], deviceId);
  assert.equal(headers["X-MG-Device-Key-Version"], "1");
  assert.ok(headers["X-MG-Device-Signature"]);
  assert.ok(headers["X-MG-Device-Nonce"]);
});

test("rotates the browser device key with proof of possession and clears its old authority lease", async () => {
  const tenantId = "tenant-rotate";
  const branchId = "branch-rotate";
  const actorId = "actor-rotate";
  const deviceId = "device-rotate";
  const calls = [];
  const api = {
    async post(path, body) {
      calls.push({ path, body });
      if (path.endsWith("/challenges")) {
        const challengeNumber = calls.filter(call => call.path.endsWith("/challenges")).length;
        return {
          challengeId: `challenge-${challengeNumber}`,
          challenge: Buffer.alloc(32, challengeNumber).toString("base64"),
          tenantId, branchId, actorId, deviceId
        };
      }
      if (path.endsWith("/complete"))
        return { deviceId, keyVersion: 1, credential: Buffer.alloc(32, 1).toString("base64") };
      if (path.endsWith("/rotate"))
        return { deviceId, keyVersion: 2, credential: Buffer.alloc(32, 2).toString("base64"), rotatedAtUtc: new Date().toISOString() };
      throw new Error(`Unexpected request: ${path}`);
    }
  };
  let cleared = false;
  const store = {
    async getDeviceId(value) { assert.equal(value, actorId); return deviceId; },
    async clearAuthorityLease(scope) {
      assert.deepEqual(scope, { tenantId, branchId });
      cleared = true;
    }
  };
  const enrollment = new DeviceEnrollmentClient(api, store);
  const original = await enrollment.ensureEnrolled({ tenantId, branchId, actorId });
  const rotated = await enrollment.rotateCredential({ tenantId, branchId, actorId });
  assert.equal(rotated.keyVersion, 2);
  assert.equal(rotated.privateKey.extractable, false);
  assert.notEqual(rotated.publicKeySpkiBase64, original.publicKeySpkiBase64);
  assert.equal(cleared, true);

  const rotation = calls.find(call => call.path.endsWith("/rotate"));
  const publicKey = await crypto.subtle.importKey(
    "spki", Buffer.from(rotation.body.publicKeySpkiBase64, "base64"),
    { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]
  );
  const challenge = calls.filter(call => call.path.endsWith("/challenges") && call.body.deviceId === deviceId).at(-1);
  const canonical = [
    "MG-DEVICE-ROTATION-V1", tenantId, branchId, actorId, deviceId,
    challenge.body.challengeId, challenge.body.challenge
  ].join("\n");
  assert.equal(await crypto.subtle.verify(
    { name: "ECDSA", hash: "SHA-256" },
    publicKey,
    Buffer.from(rotation.body.signatureBase64, "base64"),
    new TextEncoder().encode(canonical)
  ), true);
});

test("refuses to sign a non-sync or missing actor request", async () => {
  const enrollment = new DeviceEnrollmentClient({}, {});
  await assert.rejects(enrollment.createRequestHeaders({
    method: "GET", path: "/api/customers", tenantId: "t", actorId: "a"
  }), /sync API path/);
  await assert.rejects(enrollment.ensureEnrolled({ tenantId: "t", actorId: "" }), /Tenant and actor/);
});

test("recovers a lost enrollment response with the same persisted private key", async () => {
  const actorId = "actor-recovery";
  const tenantId = "tenant-recovery";
  const deviceId = "device-recovery";
  let challengeCount = 0;
  let completionCount = 0;
  const api = {
    async post(path) {
      if (path.endsWith("/challenges")) {
        challengeCount += 1;
        return {
          challengeId: `challenge-${challengeCount}`,
          challenge: Buffer.alloc(32, challengeCount).toString("base64"),
          tenantId,
          branchId: null,
          deviceId,
          actorId
        };
      }
      completionCount += 1;
      if (completionCount === 1) throw new Error("simulated lost response after server commit");
      return { deviceId, keyVersion: 2, credential: Buffer.alloc(32, 8).toString("base64") };
    }
  };
  const store = { async getDeviceId() { return deviceId; } };
  const enrollment = new DeviceEnrollmentClient(api, store);
  await assert.rejects(enrollment.ensureEnrolled({ tenantId, actorId }), /lost response/);
  const recovered = await enrollment.ensureEnrolled({ tenantId, actorId });
  assert.equal(challengeCount, 2);
  assert.equal(completionCount, 2);
  assert.equal(recovered.keyVersion, 2);
  assert.equal(recovered.privateKey.extractable, false);
  assert.ok(recovered.publicKeySpkiBase64);
});

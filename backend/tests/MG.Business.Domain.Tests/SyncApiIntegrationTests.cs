using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using MG.Business.Api.Endpoints;
using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;

namespace MG.Business.Domain.Tests;

public sealed class SyncApiIntegrationTests
{
    [Fact]
    public void DevicePresenceCannotBeTakenOverByAnotherTenantMember()
    {
        var presence = new DevicePresence(Guid.NewGuid(), Guid.NewGuid(), Guid.NewGuid(), null,
            "MG Browser", "Web", "1.0", DateTimeOffset.UtcNow);
        var originalActor = presence.ActorId;

        Assert.Throws<UnauthorizedAccessException>(() =>
            presence.Heartbeat(Guid.NewGuid(), null, "Other Browser", "Web", "2.0", DateTimeOffset.UtcNow));
        Assert.Equal(originalActor, presence.ActorId);
        Assert.Equal("MG Browser", presence.DeviceName);
    }

    [Fact]
    public void DeviceCursorCannotBeTakenOverByAnotherTenantMember()
    {
        var tenantId = Guid.NewGuid();
        var deviceId = Guid.NewGuid();
        var enrollingActorId = Guid.NewGuid();
        var otherActorId = Guid.NewGuid();
        var cursor = new SyncDeviceCursor(tenantId, deviceId, enrollingActorId, 12, DateTimeOffset.UtcNow);

        Assert.Throws<UnauthorizedAccessException>(() =>
            cursor.Advance(otherActorId, 13, DateTimeOffset.UtcNow));
        Assert.Equal(enrollingActorId, cursor.ActorId);
        Assert.Equal(12, cursor.LastAcknowledgedSequence);
    }


    [Fact]
    public async Task CustomerCreationCommandProjectsAuthoritativeCustomerAtomicallyWithJournal()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sync-customer-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Customer Sync Store",
            slug = $"customer-sync-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var deviceId = Guid.NewGuid();
        var heartbeat = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId, deviceName = "Customer Sync Device", platform = "Test", clientVersion = "0.1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);

        var customerId = Guid.NewGuid();
        var operationId = Guid.NewGuid();
        var payload = System.Text.Json.JsonSerializer.Serialize(new
        {
            customerId,
            code = "C-LOCAL-001",
            name = "Offline Customer",
            phone = "09120000000",
            email = "CUSTOMER@EXAMPLE.COM",
            address = "Main Street",
            notes = "Created offline",
            baseVersion = 0
        });
        var request = new
        {
            operations = new[]
            {
                new
                {
                    operationId, deviceId, branchId = tenant.BranchId,
                    operationType = "customer.profile.created",
                    schemaVersion = 1, authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = payload, payloadSha256 = (string?)null
                }
            }
        };

        var firstPush = await client.PostAsJsonAsync("/api/sync/operations", request);
        Assert.Equal(HttpStatusCode.Accepted, firstPush.StatusCode);
        var firstAck = await firstPush.Content.ReadFromJsonAsync<SyncPushResponse>();
        Assert.Single(firstAck!.Accepted);
        Assert.False(firstAck.Accepted[0].Duplicate);

        var customerResponse = await client.GetAsync($"/api/customers/{customerId}");
        Assert.Equal(HttpStatusCode.OK, customerResponse.StatusCode);
        var customer = await customerResponse.Content.ReadFromJsonAsync<CustomerEndpoints.CustomerResponse>();
        Assert.NotNull(customer);
        Assert.Equal(customerId, customer!.Id);
        Assert.Equal("C-LOCAL-001", customer.Code);
        Assert.Equal("Offline Customer", customer.Name);
        Assert.Equal("customer@example.com", customer.Email);

        var retry = await client.PostAsJsonAsync("/api/sync/operations", request);
        Assert.Equal(HttpStatusCode.Accepted, retry.StatusCode);
        var retryAck = await retry.Content.ReadFromJsonAsync<SyncPushResponse>();
        Assert.True(retryAck!.Accepted.Single().Duplicate);
        Assert.Equal(firstAck.Accepted[0].Sequence, retryAck.Accepted[0].Sequence);

        var duplicateCodePayload = System.Text.Json.JsonSerializer.Serialize(new
        {
            customerId = Guid.NewGuid(),
            code = "C-LOCAL-001",
            name = "Different Customer",
            baseVersion = 0
        });
        var duplicateCodePush = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(), deviceId, branchId = tenant.BranchId,
                    operationType = "customer.profile.created", schemaVersion = 1, authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = duplicateCodePayload, payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Conflict, duplicateCodePush.StatusCode);
        var customers = await client.GetFromJsonAsync<CustomerEndpoints.CustomerResponse[]>("/api/customers");
        Assert.Single(customers!);
        var afterDuplicate = await client.GetFromJsonAsync<SyncPullResponse>(
            $"/api/sync/operations?afterSequence={firstAck.Accepted[0].Sequence}&limit=20");
        Assert.Empty(afterDuplicate!.Operations);
    }

    [Fact]
    public async Task CustomerUpdateCommandUsesBaseVersionAndIsIdempotent()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sync-customer-update-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Customer Update Store",
            slug = $"customer-update-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var createResponse = await client.PostAsJsonAsync("/api/customers", new
        {
            code = "C-UPD-001", name = "Before", phone = "09120000000",
            email = "before@example.com", address = "Old", notes = "Original"
        });
        Assert.Equal(HttpStatusCode.Created, createResponse.StatusCode);
        var customer = await createResponse.Content.ReadFromJsonAsync<CustomerEndpoints.CustomerResponse>();
        Assert.NotNull(customer);
        Assert.Equal(1, customer!.Version);

        var deviceId = Guid.NewGuid();
        var heartbeat = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId, deviceName = "Customer Update Device", platform = "Test", clientVersion = "0.1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);

        var operationId = Guid.NewGuid();
        var occurredAtUtc = DateTimeOffset.UtcNow;
        var payload = System.Text.Json.JsonSerializer.Serialize(new
        {
            customerId = customer.Id, baseVersion = 1, name = "After",
            phone = "09121111111", email = "after@example.com", address = "New", notes = "Updated"
        });
        var pushRequest = new
        {
            operations = new[]
            {
                new
                {
                    operationId, deviceId, branchId = tenant.BranchId,
                    operationType = "customer.profile.updated", schemaVersion = 1,
                    authorityEpoch = 0, occurredAtUtc, payloadJson = payload, payloadSha256 = (string?)null
                }
            }
        };

        var push = await client.PostAsJsonAsync("/api/sync/operations", pushRequest);
        Assert.Equal(HttpStatusCode.Accepted, push.StatusCode);
        var acknowledgement = await push.Content.ReadFromJsonAsync<SyncPushResponse>();
        Assert.False(acknowledgement!.Accepted.Single().Duplicate);

        var updated = await client.GetFromJsonAsync<CustomerEndpoints.CustomerResponse>($"/api/customers/{customer.Id}");
        Assert.Equal(2, updated!.Version);
        Assert.Equal("After", updated.Name);

        var retry = await client.PostAsJsonAsync("/api/sync/operations", pushRequest);
        Assert.Equal(HttpStatusCode.Accepted, retry.StatusCode);
        var retryAck = await retry.Content.ReadFromJsonAsync<SyncPushResponse>();
        Assert.True(retryAck!.Accepted.Single().Duplicate);
        Assert.Equal(acknowledgement.Accepted.Single().Sequence, retryAck.Accepted.Single().Sequence);

        var stalePayload = System.Text.Json.JsonSerializer.Serialize(new
        {
            customerId = customer.Id, baseVersion = 1, name = "Stale",
            phone = "09129999999", email = "stale@example.com", address = "Wrong", notes = "Must not apply"
        });
        var stale = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(), deviceId, branchId = tenant.BranchId,
                    operationType = "customer.profile.updated", schemaVersion = 1,
                    authorityEpoch = 0, occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = stalePayload, payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Conflict, stale.StatusCode);

        var unchanged = await client.GetFromJsonAsync<CustomerEndpoints.CustomerResponse>($"/api/customers/{customer.Id}");
        Assert.Equal(2, unchanged!.Version);
        Assert.Equal("After", unchanged.Name);
        Assert.Equal("after@example.com", unchanged.Email);
    }

    [Fact]
    public async Task CustomerSyncCommandsRequireCustomerWriteRole()
    {
        using var factory = CreateFactory();
        var ownerEmail = $"sync-owner-{Guid.NewGuid():N}@mg.test";
        using var owner = await RegisterAndLoginAsync(factory, ownerEmail);
        var tenantResponse = await owner.PostAsJsonAsync("/api/tenants", new
        {
            name = "Customer Role Store",
            slug = $"customer-role-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);

        var viewerEmail = $"sync-viewer-{Guid.NewGuid():N}@mg.test";
        using var viewer = await RegisterAndLoginAsync(factory, viewerEmail);
        using (var scope = factory.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<BusinessDbContext>();
            var user = await db.Users.SingleAsync(x => x.Email == viewerEmail);
            db.TenantMemberships.Add(new TenantMembership(tenant!.TenantId, user.Id, TenantRole.Viewer, tenant.BranchId));
            await db.SaveChangesAsync();
        }

        viewer.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        viewer.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());
        var deviceId = Guid.NewGuid();
        var heartbeat = await viewer.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId, deviceName = "Viewer Device", platform = "Test", clientVersion = "0.1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);

        var payload = System.Text.Json.JsonSerializer.Serialize(new
        {
            customerId = Guid.NewGuid(), code = "VIEWER-1", name = "Forbidden", baseVersion = 0
        });
        var push = await viewer.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(), deviceId, branchId = tenant.BranchId,
                    operationType = "customer.profile.created", schemaVersion = 1,
                    authorityEpoch = 0, occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = payload, payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Forbidden, push.StatusCode);
    }

    [Fact]
    public async Task CustomerCreationCommandRejectsInvalidVersionAndTenantWideScope()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sync-customer-invalid-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Invalid Customer Sync Store",
            slug = $"invalid-customer-sync-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());
        var deviceId = Guid.NewGuid();
        var heartbeat = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId, deviceName = "Invalid Customer Device", platform = "Test", clientVersion = "0.1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);

        var invalidVersion = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(), deviceId, branchId = tenant.BranchId,
                    operationType = "customer.profile.created", schemaVersion = 1, authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = System.Text.Json.JsonSerializer.Serialize(new
                    {
                        customerId = Guid.NewGuid(), code = "C-1", name = "Invalid", baseVersion = 1
                    }),
                    payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.BadRequest, invalidVersion.StatusCode);

        var tenantWide = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(), deviceId, branchId = (Guid?)null,
                    operationType = "customer.profile.created", schemaVersion = 1, authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = System.Text.Json.JsonSerializer.Serialize(new
                    {
                        customerId = Guid.NewGuid(), code = "C-2", name = "Tenant Wide", baseVersion = 0
                    }),
                    payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.BadRequest, tenantWide.StatusCode);
    }

    [Fact]
    public async Task PushIsIdempotentAndPullIsTenantScoped()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sync-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Sync Test Store",
            slug = $"sync-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());

        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());
        var operationId = Guid.NewGuid();
        var deviceId = Guid.NewGuid();
        var request = new
        {
            operations = new[]
            {
                new
                {
                    operationId,
                    deviceId,
                    branchId = tenant.BranchId,
                    operationType = "test.local-note.created",
                    schemaVersion = 1,
                    authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = "{\"note\":\"offline-safe\"}",
                    payloadSha256 = (string?)null
                }
            }
        };

        var unregisteredPush = await client.PostAsJsonAsync("/api/sync/operations", request);
        Assert.Equal(HttpStatusCode.Forbidden, unregisteredPush.StatusCode);
        var unregisteredAck = await client.PostAsJsonAsync("/api/sync/acknowledgements", new { deviceId, branchId = tenant.BranchId, sequence = 0 });
        Assert.Equal(HttpStatusCode.Forbidden, unregisteredAck.StatusCode);

        var heartbeat = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId,
            deviceName = "Registered Sync Test Device",
            platform = "Test",
            clientVersion = "0.1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);

        var secondBranchResponse = await client.PostAsJsonAsync("/api/branches/", new
        {
            name = "Secondary",
            code = "B" + Guid.NewGuid().ToString("N")[..9]
        });
        Assert.Equal(HttpStatusCode.Created, secondBranchResponse.StatusCode);
        var secondBranch = await secondBranchResponse.Content.ReadFromJsonAsync<BranchCreated>();
        Assert.NotNull(secondBranch);
        var crossBranchPush = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(),
                    deviceId,
                    branchId = secondBranch!.Id,
                    operationType = "test.cross-branch",
                    schemaVersion = 1,
                    authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = "{\"note\":\"wrong-branch\"}",
                    payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Forbidden, crossBranchPush.StatusCode);

        var firstPush = await client.PostAsJsonAsync("/api/sync/operations", request);
        Assert.Equal(HttpStatusCode.Accepted, firstPush.StatusCode);
        var firstAck = await firstPush.Content.ReadFromJsonAsync<SyncPushResponse>();
        Assert.Single(firstAck!.Accepted);
        Assert.False(firstAck.Accepted[0].Duplicate);
        Assert.True(firstAck.Accepted[0].Sequence > 0);

        var unverifiedEpochPush = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(),
                    deviceId,
                    branchId = tenant.BranchId,
                    operationType = "test.unverified-epoch",
                    schemaVersion = 1,
                    authorityEpoch = 1,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = "{\"note\":\"must-be-rejected\"}",
                    payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Conflict, unverifiedEpochPush.StatusCode);

        var malformedHashPush = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(),
                    deviceId,
                    branchId = tenant.BranchId,
                    operationType = "test.invalid-hash",
                    schemaVersion = 1,
                    authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = "{\"note\":\"invalid-hash\"}",
                    payloadSha256 = new string('A', 4096)
                }
            }
        });
        Assert.Equal(HttpStatusCode.BadRequest, malformedHashPush.StatusCode);

        foreach (var invalidPayload in new[] { "null", "[]", "42", "\"text\"" })
        {
            var invalidPayloadPush = await client.PostAsJsonAsync("/api/sync/operations", new
            {
                operations = new[]
                {
                    new
                    {
                        operationId = Guid.NewGuid(),
                        deviceId,
                        branchId = tenant.BranchId,
                        operationType = "test.invalid-payload",
                        schemaVersion = 1,
                        authorityEpoch = 0,
                        occurredAtUtc = DateTimeOffset.UtcNow,
                        payloadJson = invalidPayload,
                        payloadSha256 = (string?)null
                    }
                }
            });
            Assert.Equal(HttpStatusCode.BadRequest, invalidPayloadPush.StatusCode);
        }

        var nullOperationPush = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new object?[]
            {
                null,
                new
                {
                    operationId = Guid.NewGuid(),
                    deviceId,
                    branchId = tenant.BranchId,
                    operationType = "test.valid-entry",
                    schemaVersion = 1,
                    authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = "{\"note\":\"valid-entry\"}",
                    payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.BadRequest, nullOperationPush.StatusCode);

        var retryPush = await client.PostAsJsonAsync("/api/sync/operations", request);
        Assert.Equal(HttpStatusCode.Accepted, retryPush.StatusCode);
        var retryAck = await retryPush.Content.ReadFromJsonAsync<SyncPushResponse>();
        Assert.Single(retryAck!.Accepted);
        Assert.True(retryAck.Accepted[0].Duplicate);
        Assert.Equal(firstAck.Accepted[0].Sequence, retryAck.Accepted[0].Sequence);

        var pull = await client.GetFromJsonAsync<SyncPullResponse>("/api/sync/operations?afterSequence=0&limit=20");
        Assert.NotNull(pull);
        Assert.Single(pull!.Operations);
        Assert.Equal(operationId, pull.Operations[0].OperationId);
        Assert.Equal(firstAck.Accepted[0].Sequence, pull.NextSequence);

        var acknowledgement = await client.PostAsJsonAsync("/api/sync/acknowledgements", new
        {
            deviceId,
            branchId = tenant.BranchId,
            sequence = firstAck.Accepted[0].Sequence
        });
        Assert.Equal(HttpStatusCode.OK, acknowledgement.StatusCode);
        var acknowledgedCursor = await acknowledgement.Content.ReadFromJsonAsync<SyncCursorResponse>();
        Assert.Equal(firstAck.Accepted[0].Sequence, acknowledgedCursor!.Sequence);
        Assert.False(acknowledgedCursor.StaleAcknowledgement);

        var futureAcknowledgement = await client.PostAsJsonAsync("/api/sync/acknowledgements", new
        {
            deviceId,
            branchId = tenant.BranchId,
            sequence = firstAck.Accepted[0].Sequence + 100000
        });
        Assert.Equal(HttpStatusCode.BadRequest, futureAcknowledgement.StatusCode);

        var staleAcknowledgement = await client.PostAsJsonAsync("/api/sync/acknowledgements", new
        {
            deviceId,
            branchId = tenant.BranchId,
            sequence = 0
        });
        var staleCursor = await staleAcknowledgement.Content.ReadFromJsonAsync<SyncCursorResponse>();
        Assert.True(staleCursor!.StaleAcknowledgement);
        Assert.Equal(firstAck.Accepted[0].Sequence, staleCursor.Sequence);

        var repeatHeartbeat = await client.PostAsJsonAsync("/api/sync/heartbeat", new { deviceId, deviceName = "Sync Test Device", platform = "Test", clientVersion = "0.1.0" });
        Assert.Equal(HttpStatusCode.OK, repeatHeartbeat.StatusCode);
        var secondDeviceId = Guid.NewGuid();
        var secondHeartbeat = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId = secondDeviceId,
            deviceName = "Second Sync Device",
            platform = "Test",
            clientVersion = "0.1.0"
        });
        Assert.Equal(HttpStatusCode.OK, secondHeartbeat.StatusCode);
        var secondDeviceAck = await client.PostAsJsonAsync("/api/sync/acknowledgements", new
        {
            deviceId = secondDeviceId,
            branchId = tenant.BranchId,
            sequence = 0
        });
        Assert.Equal(HttpStatusCode.OK, secondDeviceAck.StatusCode);

        var deviceInventory = await client.GetFromJsonAsync<DevicePresenceResponse[]>("/api/sync/devices");
        Assert.Equal(2, deviceInventory!.Length);
        var firstDeviceStatus = deviceInventory.Single(device => device.DeviceId == deviceId);
        var secondDeviceStatus = deviceInventory.Single(device => device.DeviceId == secondDeviceId);
        Assert.Equal(firstAck.Accepted[0].Sequence, firstDeviceStatus.LastAcknowledgedSequence);
        Assert.Equal(0, firstDeviceStatus.PendingOperationCount);
        Assert.Equal(firstAck.Accepted[0].Sequence, firstDeviceStatus.LatestJournalSequence);
        Assert.Equal(0, secondDeviceStatus.LastAcknowledgedSequence);
        Assert.Equal(firstAck.Accepted[0].Sequence, secondDeviceStatus.PendingOperationCount);

        client.DefaultRequestHeaders.Remove("X-MG-Tenant");
        client.DefaultRequestHeaders.Add("X-MG-Tenant", Guid.NewGuid().ToString());
        var crossTenantPull = await client.GetAsync("/api/sync/operations?afterSequence=0");
        Assert.Equal(HttpStatusCode.Forbidden, crossTenantPull.StatusCode);
        var crossTenantAck = await client.PostAsJsonAsync("/api/sync/acknowledgements", new
        {
            deviceId,
            branchId = tenant.BranchId,
            sequence = firstAck.Accepted[0].Sequence
        });
        Assert.Equal(HttpStatusCode.Forbidden, crossTenantAck.StatusCode);
    }


    [Fact]
    public async Task BranchAcknowledgementsRemainIndependentWhenOneDeviceSwitchesBranches()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"branch-cursor-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Branch Cursor Store",
            slug = $"branch-cursor-{Guid.NewGuid():N}",
            firstBranchName = "Branch A"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var secondBranchResponse = await client.PostAsJsonAsync("/api/branches/", new
        {
            name = "Branch B",
            code = "B" + Guid.NewGuid().ToString("N")[..9]
        });
        Assert.Equal(HttpStatusCode.Created, secondBranchResponse.StatusCode);
        var secondBranch = await secondBranchResponse.Content.ReadFromJsonAsync<BranchCreated>();
        Assert.NotNull(secondBranch);

        var deviceId = Guid.NewGuid();
        var heartbeatA = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId, deviceName = "Switching Device", platform = "Test", clientVersion = "0.1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeatA.StatusCode);

        client.DefaultRequestHeaders.Remove("X-MG-Branch");
        client.DefaultRequestHeaders.Add("X-MG-Branch", secondBranch!.Id.ToString());
        var branchBPush = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(), deviceId, branchId = secondBranch.Id,
                    operationType = "test.branch-b", schemaVersion = 1, authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow, payloadJson = "{\"branch\":\"B\"}",
                    payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Accepted, branchBPush.StatusCode);
        var branchBAck = await branchBPush.Content.ReadFromJsonAsync<SyncPushResponse>();
        var branchBSequence = branchBAck!.Accepted.Single().Sequence;

        client.DefaultRequestHeaders.Remove("X-MG-Branch");
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());
        var branchAPush = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(), deviceId, branchId = tenant.BranchId,
                    operationType = "test.branch-a", schemaVersion = 1, authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow, payloadJson = "{\"branch\":\"A\"}",
                    payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Accepted, branchAPush.StatusCode);
        var branchAAck = await branchAPush.Content.ReadFromJsonAsync<SyncPushResponse>();
        var branchASequence = branchAAck!.Accepted.Single().Sequence;
        Assert.True(branchASequence > branchBSequence);

        var ackA = await client.PostAsJsonAsync("/api/sync/acknowledgements", new
        {
            deviceId, branchId = tenant.BranchId, sequence = branchASequence
        });
        Assert.Equal(HttpStatusCode.OK, ackA.StatusCode);

        client.DefaultRequestHeaders.Remove("X-MG-Branch");
        client.DefaultRequestHeaders.Add("X-MG-Branch", secondBranch.Id.ToString());
        var heartbeatB = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId, deviceName = "Switching Device", platform = "Test", clientVersion = "0.1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeatB.StatusCode);

        var inventory = await client.GetFromJsonAsync<DevicePresenceResponse[]>("/api/sync/devices");
        var device = inventory!.Single(x => x.DeviceId == deviceId);
        Assert.Equal(secondBranch.Id, device.BranchId);
        Assert.Equal(0, device.LastAcknowledgedSequence);
        Assert.Equal(1, device.PendingOperationCount);

        var ackB = await client.PostAsJsonAsync("/api/sync/acknowledgements", new
        {
            deviceId, branchId = secondBranch.Id, sequence = branchBSequence
        });
        Assert.Equal(HttpStatusCode.OK, ackB.StatusCode);
        inventory = await client.GetFromJsonAsync<DevicePresenceResponse[]>("/api/sync/devices");
        device = inventory!.Single(x => x.DeviceId == deviceId);
        Assert.Equal(branchBSequence, device.LastAcknowledgedSequence);
        Assert.Equal(0, device.PendingOperationCount);
    }

    [Fact]
    public async Task HeartbeatRegistersDeviceAndOwnerCanReadConnectivity()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"heartbeat-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Heartbeat Test Store",
            slug = $"heartbeat-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var deviceId = Guid.NewGuid();
        var heartbeat = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId,
            deviceName = "MG Test Browser",
            platform = "Test",
            clientVersion = "0.1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);
        await Task.Delay(20);
        var secondHeartbeat = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId,
            deviceName = "MG Test Browser Updated",
            platform = "Test",
            clientVersion = "0.1.1"
        });
        Assert.Equal(HttpStatusCode.OK, secondHeartbeat.StatusCode);

        var devices = await client.GetFromJsonAsync<DevicePresenceResponse[]>("/api/sync/devices");
        Assert.NotNull(devices);
        var device = Assert.Single(devices!);
        Assert.Equal(deviceId, device.DeviceId);
        Assert.Equal("MG Test Browser Updated", device.DeviceName);
        Assert.Equal("0.1.1", device.ClientVersion);
        Assert.True(device.LastSeenAtUtc >= device.FirstSeenAtUtc);
        Assert.True(device.IsOnline);
        Assert.Null(device.AlertCode);
    }

    [Fact]
    public async Task SyncPushRejectsDeviceIdentityOwnedByAnotherTenantMember()
    {
        using var factory = CreateFactory();
        using var owner = await RegisterAndLoginAsync(factory, $"sync-owner-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await owner.PostAsJsonAsync("/api/tenants", new
        {
            name = "Device Ownership Store",
            slug = $"device-owner-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        owner.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        owner.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var deviceId = Guid.NewGuid();
        var heartbeat = await owner.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId,
            deviceName = "Owner Device",
            platform = "Test",
            clientVersion = "0.1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);

        var staffEmail = $"sync-staff-{Guid.NewGuid():N}@mg.test";
        using var staff = await RegisterAndLoginAsync(factory, staffEmail);
        var membership = await owner.PostAsJsonAsync("/api/memberships", new
        {
            email = staffEmail,
            role = 3,
            branchId = tenant.BranchId
        });
        Assert.Equal(HttpStatusCode.Created, membership.StatusCode);
        staff.DefaultRequestHeaders.Add("X-MG-Tenant", tenant.TenantId.ToString());
        staff.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var spoofedAcknowledgement = await staff.PostAsJsonAsync("/api/sync/acknowledgements", new
        {
            deviceId,
            branchId = tenant.BranchId,
            sequence = 0
        });
        Assert.Equal(HttpStatusCode.Forbidden, spoofedAcknowledgement.StatusCode);

        var push = await staff.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(),
                    deviceId,
                    branchId = tenant.BranchId,
                    operationType = "test.device-spoof",
                    schemaVersion = 1,
                    authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = "{\"note\":\"spoofed-device\"}",
                    payloadSha256 = (string?)null
                }
            }
        });

        Assert.Equal(HttpStatusCode.Forbidden, push.StatusCode);
    }

    [Fact]
    public async Task BranchScopedPullHidesOtherBranchesAndAdvancesAcrossHiddenRows()
    {
        using var factory = CreateFactory();
        using var owner = await RegisterAndLoginAsync(factory, $"branch-sync-owner-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await owner.PostAsJsonAsync("/api/tenants", new
        {
            name = "Branch Sync Store",
            slug = $"branch-sync-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        owner.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        owner.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var secondBranchResponse = await owner.PostAsJsonAsync("/api/branches/", new
        {
            name = "Secondary",
            code = "S" + Guid.NewGuid().ToString("N")[..8]
        });
        Assert.Equal(HttpStatusCode.Created, secondBranchResponse.StatusCode);
        var secondary = await secondBranchResponse.Content.ReadFromJsonAsync<BranchCreated>();
        Assert.NotNull(secondary);

        var ownerDeviceId = Guid.NewGuid();
        var ownerHeartbeat = await owner.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId = ownerDeviceId,
            deviceName = "Owner Sync Device",
            platform = "Test",
            clientVersion = "1.0"
        });
        Assert.Equal(HttpStatusCode.OK, ownerHeartbeat.StatusCode);

        var mainOperationId = Guid.NewGuid();
        var mainPush = await owner.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = mainOperationId,
                    deviceId = ownerDeviceId,
                    branchId = tenant.BranchId,
                    operationType = "test.main-branch",
                    schemaVersion = 1,
                    authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = "{}",
                    payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Accepted, mainPush.StatusCode);

        owner.DefaultRequestHeaders.Remove("X-MG-Branch");
        owner.DefaultRequestHeaders.Add("X-MG-Branch", secondary!.Id.ToString());
        var secondaryOperationId = Guid.NewGuid();
        var secondaryPush = await owner.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = secondaryOperationId,
                    deviceId = ownerDeviceId,
                    branchId = secondary.Id,
                    operationType = "test.secondary-branch",
                    schemaVersion = 1,
                    authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = "{\"scope\":\"secondary\"}",
                    payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Accepted, secondaryPush.StatusCode);

        var globalOperationId = Guid.NewGuid();
        var globalPush = await owner.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = globalOperationId,
                    deviceId = ownerDeviceId,
                    branchId = (Guid?)null,
                    operationType = "test.tenant-wide",
                    schemaVersion = 1,
                    authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = "{\"scope\":\"tenant\"}",
                    payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Accepted, globalPush.StatusCode);

        var staffEmail = $"branch-sync-staff-{Guid.NewGuid():N}@mg.test";
        using var staff = await RegisterAndLoginAsync(factory, staffEmail);
        var membership = await owner.PostAsJsonAsync("/api/memberships", new
        {
            email = staffEmail,
            role = 3,
            branchId = secondary.Id
        });
        Assert.Equal(HttpStatusCode.Created, membership.StatusCode);

        staff.DefaultRequestHeaders.Add("X-MG-Tenant", tenant.TenantId.ToString());
        staff.DefaultRequestHeaders.Add("X-MG-Branch", secondary.Id.ToString());
        var staffDeviceId = Guid.NewGuid();
        var staffHeartbeat = await staff.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId = staffDeviceId,
            deviceName = "Secondary Branch Device",
            platform = "Test",
            clientVersion = "1.0"
        });
        Assert.Equal(HttpStatusCode.OK, staffHeartbeat.StatusCode);

        var forbiddenTenantWidePush = await staff.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(),
                    deviceId = staffDeviceId,
                    branchId = (Guid?)null,
                    operationType = "test.forbidden-tenant-wide",
                    schemaVersion = 1,
                    authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = "{\"scope\":\"tenant\"}",
                    payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Forbidden, forbiddenTenantWidePush.StatusCode);

        var forbiddenOtherBranchPush = await staff.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(),
                    deviceId = staffDeviceId,
                    branchId = tenant.BranchId,
                    operationType = "test.forbidden-other-branch",
                    schemaVersion = 1,
                    authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = System.Text.Json.JsonSerializer.Serialize(new { scope = "main" }),
                    payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Forbidden, forbiddenOtherBranchPush.StatusCode);

        var unassignedStaffEmail = $"branch-sync-unassigned-{Guid.NewGuid():N}@mg.test";
        using var unassignedStaff = await RegisterAndLoginAsync(factory, unassignedStaffEmail);
        var unassignedMembership = await owner.PostAsJsonAsync("/api/memberships", new
        {
            email = unassignedStaffEmail,
            role = 3,
            branchId = (Guid?)null
        });
        Assert.Equal(HttpStatusCode.Created, unassignedMembership.StatusCode);
        unassignedStaff.DefaultRequestHeaders.Add("X-MG-Tenant", tenant.TenantId.ToString());
        var unassignedDeviceId = Guid.NewGuid();
        var unassignedHeartbeat = await unassignedStaff.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId = unassignedDeviceId,
            deviceName = "Unassigned Staff Device",
            platform = "Test",
            clientVersion = "1.0"
        });
        Assert.Equal(HttpStatusCode.OK, unassignedHeartbeat.StatusCode);

        var forbiddenUnselectedBranchPush = await unassignedStaff.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(),
                    deviceId = unassignedDeviceId,
                    branchId = secondary.Id,
                    operationType = "test.forbidden-unselected-branch",
                    schemaVersion = 1,
                    authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = "{\"scope\":\"unselected\"}",
                    payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Forbidden, forbiddenUnselectedBranchPush.StatusCode);

        var pull = await staff.GetFromJsonAsync<SyncPullResponse>("/api/sync/operations?afterSequence=0&limit=20");
        Assert.NotNull(pull);
        Assert.Single(pull!.Operations);
        Assert.Contains(pull.Operations, operation => operation.OperationId == secondaryOperationId);
        Assert.DoesNotContain(pull.Operations, operation => operation.OperationId == globalOperationId);
        Assert.DoesNotContain(pull.Operations, operation => operation.OperationId == mainOperationId);
        Assert.True(pull.NextSequence >= pull.Operations.Max(operation => operation.Sequence));

        var ownerPull = await owner.GetFromJsonAsync<SyncPullResponse>("/api/sync/operations?afterSequence=0&limit=20");
        Assert.NotNull(ownerPull);
        Assert.Equal(2, ownerPull!.Operations.Length);
        Assert.Contains(ownerPull.Operations, operation => operation.OperationId == secondaryOperationId);
        Assert.Contains(ownerPull.Operations, operation => operation.OperationId == globalOperationId);
        Assert.DoesNotContain(ownerPull.Operations, operation => operation.OperationId == mainOperationId);

        var unassignedPull = await unassignedStaff.GetFromJsonAsync<SyncPullResponse>("/api/sync/operations?afterSequence=0&limit=20");
        Assert.NotNull(unassignedPull);
        Assert.Empty(unassignedPull!.Operations);

        var deviceInventory = await owner.GetFromJsonAsync<DevicePresenceResponse[]>("/api/sync/devices");
        Assert.NotNull(deviceInventory);
        Assert.Equal(2, deviceInventory!.Single(device => device.DeviceId == ownerDeviceId).PendingOperationCount);
        Assert.Equal(1, deviceInventory.Single(device => device.DeviceId == staffDeviceId).PendingOperationCount);
        Assert.Equal(0, deviceInventory.Single(device => device.DeviceId == unassignedDeviceId).PendingOperationCount);
    }

    private static async Task<HttpClient> RegisterAndLoginAsync(WebApplicationFactory<Program> factory, string email)
    {
        using (var scope = factory.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<BusinessDbContext>();
            await db.Database.EnsureCreatedAsync();
        }

        var client = factory.CreateClient();
        var register = await client.PostAsJsonAsync("/auth/register", new { email, password = "Strong!Passw0rd" });
        Assert.Equal(HttpStatusCode.OK, register.StatusCode);
        var login = await client.PostAsJsonAsync("/auth/login", new { email, password = "Strong!Passw0rd", useCookies = false });
        Assert.Equal(HttpStatusCode.OK, login.StatusCode);
        var tokens = await login.Content.ReadFromJsonAsync<TokenResponse>();
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", tokens!.AccessToken);
        return client;
    }

    private static WebApplicationFactory<Program> CreateFactory()
    {
        var connection = new Microsoft.Data.Sqlite.SqliteConnection("Data Source=:memory:");
        connection.Open();
        return new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
            builder.ConfigureServices(services =>
            {
                services.RemoveAll<DbContextOptions<BusinessDbContext>>();
                services.RemoveAll<IDbContextOptionsConfiguration<BusinessDbContext>>();
                services.RemoveAll<BusinessDbContext>();
                services.AddSingleton(connection);
                services.AddDbContext<BusinessDbContext>((provider, options) =>
                    options.UseSqlite(provider.GetRequiredService<Microsoft.Data.Sqlite.SqliteConnection>()));
            }));
    }

    private sealed record TokenResponse(string TokenType, string AccessToken, int ExpiresIn, string RefreshToken);
    private sealed record TenantCreated(Guid TenantId, Guid BranchId, string Name, string Slug, string Role);
    private sealed record BranchCreated(Guid Id, string Code, string Name, bool IsActive, DateTimeOffset CreatedAtUtc);
    private sealed record SyncPushResponse(SyncAck[] Accepted);
    private sealed record SyncAck(Guid OperationId, long Sequence, bool Duplicate);
    private sealed record SyncCursorResponse(Guid DeviceId, long Sequence, bool StaleAcknowledgement);
    private sealed record SyncPullResponse(SyncOperationResponse[] Operations, long NextSequence, bool HasMore);
    private sealed record DevicePresenceResponse(Guid DeviceId, Guid ActorId, Guid? BranchId, string DeviceName, string Platform, string ClientVersion, DateTimeOffset FirstSeenAtUtc, DateTimeOffset LastSeenAtUtc, long LastAcknowledgedSequence, long LatestJournalSequence, long PendingOperationCount, bool IsOnline, string? AlertCode);
    private sealed record SyncOperationResponse(long Sequence, Guid OperationId, Guid DeviceId, Guid? BranchId,
        string OperationType, int SchemaVersion, long AuthorityEpoch, DateTimeOffset OccurredAtUtc,
        DateTimeOffset ReceivedAtUtc, string PayloadJson, string PayloadSha256);
}

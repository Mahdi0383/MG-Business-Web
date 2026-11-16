using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using MG.Business.Api.Endpoints;
using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.Configuration;
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
    public async Task SyncPushFailsClosedForUnconfiguredAuthorityEpochAndTamperedPayloadHash()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sync-fencing-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Sync Fencing Store",
            slug = $"sync-fencing-{Guid.NewGuid():N}",
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
            deviceId, deviceName = "Fencing Test Device", platform = "Test", clientVersion = "1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);

        async Task<HttpResponseMessage> Push(long authorityEpoch, string payload, string? hash)
            => await client.PostAsJsonAsync("/api/sync/operations", new
            {
                operations = new[]
                {
                    new
                    {
                        operationId = Guid.NewGuid(), deviceId, branchId = tenant.BranchId,
                        operationType = "test.fencing", schemaVersion = 1, authorityEpoch,
                        occurredAtUtc = DateTimeOffset.UtcNow, payloadJson = payload, payloadSha256 = hash
                    }
                }
            });

        using var nonZeroEpoch = await Push(1, "{\"value\":1}", null);
        Assert.Equal(HttpStatusCode.Conflict, nonZeroEpoch.StatusCode);
        var fencingBody = await nonZeroEpoch.Content.ReadAsStringAsync();
        Assert.Contains("AUTHORITY_LEASE_NOT_CONFIGURED", fencingBody);

        using var tamperedHash = await Push(0, "{\"value\":2}", new string('0', 64));
        Assert.Equal(HttpStatusCode.BadRequest, tamperedHash.StatusCode);

        var journal = await client.GetFromJsonAsync<SyncPullResponse>("/api/sync/operations?afterSequence=0&limit=20");
        Assert.Empty(journal!.Operations);
    }

    [Fact]
    public async Task KitchenStaffCannotReadCustomerSyncJournal()
    {
        using var factory = CreateFactory();
        using var owner = await RegisterAndLoginAsync(factory, $"sync-owner-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await owner.PostAsJsonAsync("/api/tenants", new
        {
            name = "Sync Privacy Store",
            slug = $"sync-privacy-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        owner.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        owner.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var staffEmail = $"kitchen-{Guid.NewGuid():N}@mg.test";
        using var kitchenStaff = await RegisterAndLoginAsync(factory, staffEmail);
        var membership = await owner.PostAsJsonAsync("/api/memberships", new
        {
            email = staffEmail,
            role = TenantRole.KitchenStaff,
            branchId = tenant.BranchId
        });
        Assert.Equal(HttpStatusCode.Created, membership.StatusCode);

        kitchenStaff.DefaultRequestHeaders.Add("X-MG-Tenant", tenant.TenantId.ToString());
        kitchenStaff.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());
        var journal = await kitchenStaff.GetAsync("/api/sync/operations?afterSequence=0&limit=20");

        Assert.Equal(HttpStatusCode.Forbidden, journal.StatusCode);
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

        var reusedOperationIdPayload = System.Text.Json.JsonSerializer.Serialize(new
        {
            customerId = customer.Id, baseVersion = 2, name = "Changed envelope",
            phone = "09123333333", email = "changed@example.com", address = "Different", notes = "Must reject"
        });
        var reusedOperationId = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId, deviceId, branchId = tenant.BranchId,
                    operationType = "customer.profile.updated", schemaVersion = 1,
                    authorityEpoch = 0, occurredAtUtc, payloadJson = reusedOperationIdPayload,
                    payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Conflict, reusedOperationId.StatusCode);

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
    public async Task CustomerUpdateBatchRollsBackEarlierMutationsWhenAnyBaseVersionIsStale()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sync-customer-batch-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Customer Batch Store",
            slug = $"customer-batch-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var firstCreate = await client.PostAsJsonAsync("/api/customers", new
        {
            code = "BATCH-001", name = "First Before", phone = (string?)null,
            email = (string?)null, address = (string?)null, notes = (string?)null
        });
        var secondCreate = await client.PostAsJsonAsync("/api/customers", new
        {
            code = "BATCH-002", name = "Second Before", phone = (string?)null,
            email = (string?)null, address = (string?)null, notes = (string?)null
        });
        var first = await firstCreate.Content.ReadFromJsonAsync<CustomerEndpoints.CustomerResponse>();
        var second = await secondCreate.Content.ReadFromJsonAsync<CustomerEndpoints.CustomerResponse>();
        Assert.NotNull(first);
        Assert.NotNull(second);

        var deviceId = Guid.NewGuid();
        var heartbeat = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId, deviceName = "Batch Device", platform = "Test", clientVersion = "0.1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);

        var firstPayload = System.Text.Json.JsonSerializer.Serialize(new
        {
            customerId = first!.Id, baseVersion = 1, name = "First Must Roll Back",
            phone = (string?)null, email = (string?)null, address = (string?)null, notes = (string?)null
        });
        var stalePayload = System.Text.Json.JsonSerializer.Serialize(new
        {
            customerId = second!.Id, baseVersion = 99, name = "Stale Second",
            phone = (string?)null, email = (string?)null, address = (string?)null, notes = (string?)null
        });
        var push = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(), deviceId, branchId = tenant.BranchId,
                    operationType = "customer.profile.updated", schemaVersion = 1,
                    authorityEpoch = 0, occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = firstPayload, payloadSha256 = (string?)null
                },
                new
                {
                    operationId = Guid.NewGuid(), deviceId, branchId = tenant.BranchId,
                    operationType = "customer.profile.updated", schemaVersion = 1,
                    authorityEpoch = 0, occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = stalePayload, payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.Conflict, push.StatusCode);

        var firstAfter = await client.GetFromJsonAsync<CustomerEndpoints.CustomerResponse>($"/api/customers/{first.Id}");
        var secondAfter = await client.GetFromJsonAsync<CustomerEndpoints.CustomerResponse>($"/api/customers/{second.Id}");
        Assert.Equal(1, firstAfter!.Version);
        Assert.Equal("First Before", firstAfter.Name);
        Assert.Equal(1, secondAfter!.Version);
        Assert.Equal("Second Before", secondAfter.Name);
        var journal = await client.GetFromJsonAsync<SyncPullResponse>("/api/sync/operations?afterSequence=0&limit=20");
        Assert.Empty(journal!.Operations);
    }

    [Fact]
    public async Task CustomerDeactivationCommandIsVersionedAndIdempotent()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sync-customer-deactivate-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Customer Deactivation Store",
            slug = $"customer-deactivate-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var create = await client.PostAsJsonAsync("/api/customers", new
        {
            code = "C-DEL-001", name = "To Deactivate", phone = (string?)null,
            email = (string?)null, address = (string?)null, notes = (string?)null
        });
        Assert.Equal(HttpStatusCode.Created, create.StatusCode);
        var customer = await create.Content.ReadFromJsonAsync<CustomerEndpoints.CustomerResponse>();
        Assert.NotNull(customer);

        var deviceId = Guid.NewGuid();
        var heartbeat = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId, deviceName = "Customer Deactivation Device", platform = "Test", clientVersion = "0.1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);

        var operationId = Guid.NewGuid();
        var occurredAtUtc = DateTimeOffset.UtcNow;
        var payload = System.Text.Json.JsonSerializer.Serialize(new
        {
            customerId = customer!.Id, baseVersion = customer.Version
        });
        var request = new
        {
            operations = new[]
            {
                new
                {
                    operationId, deviceId, branchId = tenant.BranchId,
                    operationType = "customer.profile.deactivated", schemaVersion = 1,
                    authorityEpoch = 0, occurredAtUtc, payloadJson = payload, payloadSha256 = (string?)null
                }
            }
        };

        var push = await client.PostAsJsonAsync("/api/sync/operations", request);
        Assert.Equal(HttpStatusCode.Accepted, push.StatusCode);
        var firstAck = await push.Content.ReadFromJsonAsync<SyncPushResponse>();
        Assert.False(firstAck!.Accepted.Single().Duplicate);

        var deactivated = await client.GetFromJsonAsync<CustomerEndpoints.CustomerResponse>($"/api/customers/{customer.Id}");
        Assert.False(deactivated!.IsActive);
        Assert.Equal(customer.Version + 1, deactivated.Version);
        var activeCustomers = await client.GetFromJsonAsync<CustomerEndpoints.CustomerResponse[]>("/api/customers");
        Assert.Empty(activeCustomers!);

        var retry = await client.PostAsJsonAsync("/api/sync/operations", request);
        Assert.Equal(HttpStatusCode.Accepted, retry.StatusCode);
        var retryAck = await retry.Content.ReadFromJsonAsync<SyncPushResponse>();
        Assert.True(retryAck!.Accepted.Single().Duplicate);
        Assert.Equal(firstAck.Accepted.Single().Sequence, retryAck.Accepted.Single().Sequence);
    }

    [Fact]
    public async Task CustomerDeactivationRejectsStaleBaseVersionWithoutChangingCustomerOrJournal()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sync-customer-deactivate-stale-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Customer Deactivation Conflict Store",
            slug = $"customer-deactivate-conflict-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var create = await client.PostAsJsonAsync("/api/customers", new
        {
            code = "C-DEL-STALE", name = "Keep Active", phone = (string?)null,
            email = (string?)null, address = (string?)null, notes = (string?)null
        });
        Assert.Equal(HttpStatusCode.Created, create.StatusCode);
        var customer = await create.Content.ReadFromJsonAsync<CustomerEndpoints.CustomerResponse>();
        Assert.NotNull(customer);

        var deviceId = Guid.NewGuid();
        var heartbeat = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId, deviceName = "Stale Deactivation Device", platform = "Test", clientVersion = "0.1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);

        var payload = System.Text.Json.JsonSerializer.Serialize(new
        {
            customerId = customer!.Id, baseVersion = customer.Version + 1
        });
        var push = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(), deviceId, branchId = tenant.BranchId,
                    operationType = "customer.profile.deactivated", schemaVersion = 1,
                    authorityEpoch = 0, occurredAtUtc = DateTimeOffset.UtcNow,
                    payloadJson = payload, payloadSha256 = (string?)null
                }
            }
        });

        Assert.Equal(HttpStatusCode.Conflict, push.StatusCode);
        var unchanged = await client.GetFromJsonAsync<CustomerEndpoints.CustomerResponse>($"/api/customers/{customer.Id}");
        Assert.True(unchanged!.IsActive);
        Assert.Equal(customer.Version, unchanged.Version);
        var journal = await client.GetFromJsonAsync<SyncPullResponse>("/api/sync/operations?afterSequence=0&limit=20");
        Assert.Empty(journal!.Operations);
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

        owner.DefaultRequestHeaders.Remove("X-MG-Branch");
        var tenantWideOwnerPull = await owner.GetFromJsonAsync<SyncPullResponse>("/api/sync/operations?afterSequence=0&limit=20");
        Assert.NotNull(tenantWideOwnerPull);
        Assert.Equal(3, tenantWideOwnerPull!.Operations.Length);
        Assert.Contains(tenantWideOwnerPull.Operations, operation => operation.OperationId == mainOperationId);
        Assert.Contains(tenantWideOwnerPull.Operations, operation => operation.OperationId == secondaryOperationId);
        Assert.Contains(tenantWideOwnerPull.Operations, operation => operation.OperationId == globalOperationId);

        var unassignedPull = await unassignedStaff.GetFromJsonAsync<SyncPullResponse>("/api/sync/operations?afterSequence=0&limit=20");
        Assert.NotNull(unassignedPull);
        Assert.Empty(unassignedPull!.Operations);

        var deviceInventory = await owner.GetFromJsonAsync<DevicePresenceResponse[]>("/api/sync/devices");
        Assert.NotNull(deviceInventory);
        Assert.Equal(2, deviceInventory!.Single(device => device.DeviceId == ownerDeviceId).PendingOperationCount);
        Assert.Equal(1, deviceInventory.Single(device => device.DeviceId == staffDeviceId).PendingOperationCount);
        Assert.Equal(0, deviceInventory.Single(device => device.DeviceId == unassignedDeviceId).PendingOperationCount);
    }



    [Fact]
    public async Task SyncBatchRejectsMultipleMutationsOfTheSameCustomer()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sync-customer-batch-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Customer Batch Store",
            slug = $"customer-batch-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var create = await client.PostAsJsonAsync("/api/customers", new
        {
            code = "C-BATCH-001", name = "Before"
        });
        Assert.Equal(HttpStatusCode.Created, create.StatusCode);
        var customer = await create.Content.ReadFromJsonAsync<CustomerEndpoints.CustomerResponse>();
        Assert.NotNull(customer);

        var deviceId = Guid.NewGuid();
        var heartbeat = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId, deviceName = "Batch Device", platform = "Test", clientVersion = "1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);

        var operations = new[]
        {
            new
            {
                operationId = Guid.NewGuid(), deviceId, branchId = tenant.BranchId,
                operationType = "customer.profile.updated", schemaVersion = 1, authorityEpoch = 0,
                occurredAtUtc = DateTimeOffset.UtcNow,
                payloadJson = System.Text.Json.JsonSerializer.Serialize(new
                {
                    customerId = customer!.Id, baseVersion = customer.Version, name = "First"
                }),
                payloadSha256 = (string?)null
            },
            new
            {
                operationId = Guid.NewGuid(), deviceId, branchId = tenant.BranchId,
                operationType = "customer.profile.updated", schemaVersion = 1, authorityEpoch = 0,
                occurredAtUtc = DateTimeOffset.UtcNow,
                payloadJson = System.Text.Json.JsonSerializer.Serialize(new
                {
                    customerId = customer!.Id, baseVersion = customer.Version, name = "Second"
                }),
                payloadSha256 = (string?)null
            }
        };

        var response = await client.PostAsJsonAsync("/api/sync/operations", new { operations });
        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);

        var unchanged = await client.GetFromJsonAsync<CustomerEndpoints.CustomerResponse>(
            $"/api/customers/{customer!.Id}");
        Assert.NotNull(unchanged);
        Assert.Equal("Before", unchanged!.Name);
        Assert.Equal(1, unchanged.Version);
        var journal = await client.GetFromJsonAsync<SyncPullResponse>(
            "/api/sync/operations?afterSequence=0&limit=20");
        Assert.Empty(journal!.Operations);
    }

    [Fact]
    public async Task DeviceHeartbeatRejectsOversizedOrControlCharacterMetadata()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sync-heartbeat-validation-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Heartbeat Validation Store",
            slug = $"heartbeat-validation-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var oversized = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId = Guid.NewGuid(), deviceName = new string('D', 101), platform = "Web", clientVersion = "1.0"
        });
        Assert.Equal(HttpStatusCode.BadRequest, oversized.StatusCode);

        var controlCharacter = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId = Guid.NewGuid(), deviceName = "MG\nBrowser", platform = "Web", clientVersion = "1.0"
        });
        Assert.Equal(HttpStatusCode.BadRequest, controlCharacter.StatusCode);

        var valid = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId = Guid.NewGuid(), deviceName = "MG Browser", platform = "Web", clientVersion = "1.0"
        });
        Assert.Equal(HttpStatusCode.OK, valid.StatusCode);
    }

    [Fact]
    public async Task SyncPushRejectsOversizedCombinedPayloadsAndFutureOperationTimestamps()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sync-batch-limits-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Sync Batch Limits Store",
            slug = $"sync-batch-limits-{Guid.NewGuid():N}",
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
            deviceId, deviceName = "Batch Limits Device", platform = "Test", clientVersion = "1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);

        var largePayload = "{\"data\":\"" + new string('x', 65_500) + "\"}";
        var oversizedBatch = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = Enumerable.Range(0, 17).Select(_ => new
            {
                operationId = Guid.NewGuid(), deviceId, branchId = tenant.BranchId,
                operationType = "test.payload", schemaVersion = 1, authorityEpoch = 0,
                occurredAtUtc = DateTimeOffset.UtcNow,
                payloadJson = largePayload, payloadSha256 = (string?)null
            }).ToArray()
        });
        Assert.Equal(HttpStatusCode.BadRequest, oversizedBatch.StatusCode);
        Assert.Contains("1 MiB", await oversizedBatch.Content.ReadAsStringAsync());

        var futureOperation = await client.PostAsJsonAsync("/api/sync/operations", new
        {
            operations = new[]
            {
                new
                {
                    operationId = Guid.NewGuid(), deviceId, branchId = tenant.BranchId,
                    operationType = "test.future", schemaVersion = 1, authorityEpoch = 0,
                    occurredAtUtc = DateTimeOffset.UtcNow.AddMinutes(6),
                    payloadJson = "{\"value\":1}", payloadSha256 = (string?)null
                }
            }
        });
        Assert.Equal(HttpStatusCode.BadRequest, futureOperation.StatusCode);
        Assert.Contains("five minutes", await futureOperation.Content.ReadAsStringAsync());

        var journal = await client.GetFromJsonAsync<SyncPullResponse>("/api/sync/operations?afterSequence=0&limit=20");
        Assert.Empty(journal!.Operations);
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


    [Fact]
    public async Task SignedAuthorityLeaseIsVerifiableAndOnlyOneDeviceCanHoldBranchAuthority()
    {
        using var serverSigningKey = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        var serverPrivateKey = Convert.ToBase64String(serverSigningKey.ExportPkcs8PrivateKey());
        using var factory = CreateFactory(
            requireDeviceSignatures: true,
            requireAuthorityLeases: true,
            authoritySigningPrivateKeyBase64: serverPrivateKey);
        using var client = await RegisterAndLoginAsync(factory, $"authority-lease-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Authority Lease Store",
            slug = $"authority-lease-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var actorId = Guid.Empty;
        async Task<(Guid DeviceId, ECDsa Key, string Credential)> EnrollDevice()
        {
            var deviceId = Guid.NewGuid();
            var key = ECDsa.Create(ECCurve.NamedCurves.nistP256);
            var challengeResponse = await client.PostAsJsonAsync("/api/sync/enrollment/challenges", new { deviceId });
            Assert.Equal(HttpStatusCode.OK, challengeResponse.StatusCode);
            var challenge = await challengeResponse.Content.ReadFromJsonAsync<DeviceEnrollmentChallengeResponse>();
            Assert.NotNull(challenge);
            actorId = challenge!.ActorId;
            var publicKey = Convert.ToBase64String(key.ExportSubjectPublicKeyInfo());
            var message = string.Join("\\n",
                "MG-DEVICE-ENROLLMENT-V1",
                tenant.TenantId.ToString("D"),
                tenant.BranchId.ToString("D"),
                actorId.ToString("D"),
                deviceId.ToString("D"),
                challenge.ChallengeId.ToString("D"),
                challenge.Challenge);
            var proof = Convert.ToBase64String(key.SignData(
                Encoding.UTF8.GetBytes(message),
                HashAlgorithmName.SHA256,
                DSASignatureFormat.IeeeP1363FixedFieldConcatenation));
            var completed = await client.PostAsJsonAsync("/api/sync/enrollment/complete", new
            {
                challengeId = challenge.ChallengeId,
                deviceId,
                challenge = challenge.Challenge,
                publicKeySpkiBase64 = publicKey,
                signatureBase64 = proof
            });
            Assert.Equal(HttpStatusCode.Created, completed.StatusCode);
            var credential = await completed.Content.ReadFromJsonAsync<DeviceEnrollmentResult>();
            Assert.NotNull(credential);
            return (deviceId, key, credential!.Credential);
        }

        var first = await EnrollDevice();
        var second = await EnrollDevice();

        async Task<HttpResponseMessage> SendSigned(string method, string path,
            (Guid DeviceId, ECDsa Key, string Credential) device, string body = "")
        {
            var timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            var nonce = Guid.NewGuid();
            var bodyHash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(body))).ToLowerInvariant();
            var canonical = string.Join("\\n",
                "MG-DEVICE-REQUEST-V1",
                method.ToUpperInvariant(),
                path,
                tenant.TenantId.ToString("D"),
                tenant.BranchId.ToString("D"),
                actorId.ToString("D"),
                device.DeviceId.ToString("D"),
                "1",
                timestamp.ToString(System.Globalization.CultureInfo.InvariantCulture),
                nonce.ToString("D"),
                bodyHash);
            var signature = Convert.ToBase64String(device.Key.SignData(
                Encoding.UTF8.GetBytes(canonical),
                HashAlgorithmName.SHA256,
                DSASignatureFormat.IeeeP1363FixedFieldConcatenation));
            using var request = new HttpRequestMessage(new HttpMethod(method), path);
            if (method.Equals("POST", StringComparison.Ordinal))
                request.Content = new StringContent(body, Encoding.UTF8, "application/json");
            request.Headers.TryAddWithoutValidation("X-MG-Device-Id", device.DeviceId.ToString("D"));
            request.Headers.TryAddWithoutValidation("X-MG-Device-Key-Version", "1");
            request.Headers.TryAddWithoutValidation("X-MG-Device-Credential", device.Credential);
            request.Headers.TryAddWithoutValidation("X-MG-Device-Timestamp", timestamp.ToString(System.Globalization.CultureInfo.InvariantCulture));
            request.Headers.TryAddWithoutValidation("X-MG-Device-Nonce", nonce.ToString("D"));
            request.Headers.TryAddWithoutValidation("X-MG-Device-Signature", signature);
            return await client.SendAsync(request);
        }

        using var acquired = await SendSigned("POST", "/api/sync/authority-lease", first, "{}");
        Assert.Equal(HttpStatusCode.OK, acquired.StatusCode);
        var lease = await acquired.Content.ReadFromJsonAsync<AuthorityLeaseResponse>();
        Assert.NotNull(lease);
        Assert.Equal(1, lease!.Epoch);
        Assert.Equal(first.DeviceId, lease.DeviceId);
        Assert.Equal(tenant.BranchId, lease.BranchId);

        using var publicKey = ECDsa.Create();
        publicKey.ImportSubjectPublicKeyInfo(Convert.FromBase64String(lease.PublicKeySpkiBase64), out _);
        var leaseMessage = string.Join("\\n",
            "MG-AUTHORITY-LEASE-V1",
            lease.TenantId.ToString("D"),
            lease.BranchId?.ToString("D") ?? string.Empty,
            lease.ScopeKey,
            lease.DeviceId.ToString("D"),
            lease.LeaseId.ToString("D"),
            lease.Epoch.ToString(System.Globalization.CultureInfo.InvariantCulture),
            lease.IssuedAtUtc.ToUnixTimeMilliseconds().ToString(System.Globalization.CultureInfo.InvariantCulture),
            lease.ExpiresAtUtc.ToUnixTimeMilliseconds().ToString(System.Globalization.CultureInfo.InvariantCulture));
        Assert.True(publicKey.VerifyData(
            Encoding.UTF8.GetBytes(leaseMessage),
            Convert.FromBase64String(lease.SignatureBase64),
            HashAlgorithmName.SHA256,
            DSASignatureFormat.IeeeP1363FixedFieldConcatenation));

        using var secondRead = await SendSigned("GET", "/api/sync/authority-lease", second);
        Assert.Equal(HttpStatusCode.OK, secondRead.StatusCode);
        var observed = await secondRead.Content.ReadFromJsonAsync<AuthorityLeaseResponse>();
        Assert.NotNull(observed);
        Assert.Equal(lease.LeaseId, observed!.LeaseId);
        Assert.Equal(lease.Epoch, observed.Epoch);

        using var competingAcquire = await SendSigned("POST", "/api/sync/authority-lease", second, "{}");
        Assert.Equal(HttpStatusCode.Conflict, competingAcquire.StatusCode);

        first.Key.Dispose();
        second.Key.Dispose();
    }

    private static WebApplicationFactory<Program> CreateFactory(bool requireDeviceSignatures = false, bool requireAuthorityLeases = false, string? authoritySigningPrivateKeyBase64 = null)
    {
        var connection = new Microsoft.Data.Sqlite.SqliteConnection("Data Source=:memory:");
        connection.Open();
        return new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
        {
            builder.ConfigureAppConfiguration((_, configuration) => configuration.AddInMemoryCollection(
                new Dictionary<string, string?> { ["SyncSecurity:RequireDeviceSignatures"] = requireDeviceSignatures.ToString(), ["SyncSecurity:RequireAuthorityLeases"] = requireAuthorityLeases.ToString(), ["SyncSecurity:AuthoritySigningPrivateKeyPkcs8Base64"] = authoritySigningPrivateKeyBase64 }));
            builder.ConfigureServices(services =>
            {
                services.RemoveAll<DbContextOptions<BusinessDbContext>>();
                services.RemoveAll<IDbContextOptionsConfiguration<BusinessDbContext>>();
                services.RemoveAll<BusinessDbContext>();
                services.AddSingleton(connection);
                services.AddDbContext<BusinessDbContext>((provider, options) =>
                    options.UseSqlite(provider.GetRequiredService<Microsoft.Data.Sqlite.SqliteConnection>()));
            });
        });
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

    [Fact]
    public void DeviceEnrollmentChallengeIsSingleUseAndBoundToItsSecurityContext()
    {
        var tenantId = Guid.NewGuid();
        var actorId = Guid.NewGuid();
        var branchId = Guid.NewGuid();
        var deviceId = Guid.NewGuid();
        var now = DateTimeOffset.UtcNow;
        var digest = new string('a', 64);
        var challenge = new DeviceEnrollmentChallenge(Guid.NewGuid(), tenantId, actorId,
            branchId, deviceId, digest, now, now.AddMinutes(5));

        Assert.False(challenge.TryConsume(Guid.NewGuid(), actorId, branchId, deviceId, digest, now.AddSeconds(1)));
        Assert.False(challenge.TryConsume(tenantId, actorId, null, deviceId, digest, now.AddSeconds(1)));
        Assert.False(challenge.TryConsume(tenantId, actorId, branchId, Guid.NewGuid(), digest, now.AddSeconds(1)));
        Assert.False(challenge.TryConsume(tenantId, actorId, branchId, deviceId, new string('b', 64), now.AddSeconds(1)));
        Assert.True(challenge.TryConsume(tenantId, actorId, branchId, deviceId, digest, now.AddSeconds(1)));
        Assert.False(challenge.TryConsume(tenantId, actorId, branchId, deviceId, digest, now.AddSeconds(2)));
    }

    [Fact]
    public void DeviceEnrollmentChallengeExpiresClosed()
    {
        var now = DateTimeOffset.UtcNow;
        var challenge = new DeviceEnrollmentChallenge(Guid.NewGuid(), Guid.NewGuid(), Guid.NewGuid(),
            null, Guid.NewGuid(), new string('c', 64), now, now.AddMinutes(1));

        Assert.False(challenge.TryConsume(challenge.TenantId, challenge.ActorId, null,
            challenge.DeviceId, new string('c', 64), now.AddMinutes(1)));
    }

    [Fact]
    public void DeviceCredentialRevocationIsIdempotent()
    {
        var now = DateTimeOffset.UtcNow;
        var credential = new DeviceCredential(Guid.NewGuid(), Guid.NewGuid(), null, Guid.NewGuid(),
            Convert.ToBase64String(new byte[] { 1, 2, 3 }), new string('d', 64), now);

        Assert.True(credential.IsActive);
        credential.RotateCredential(new string('e', 64), now.AddSeconds(1));
        Assert.Equal(2, credential.KeyVersion);
        Assert.Equal(new string('e', 64), credential.CredentialSha256);
        credential.Revoke(now.AddSeconds(2));
        var revokedAt = credential.RevokedAtUtc;
        credential.Revoke(now.AddSeconds(3));

        Assert.False(credential.IsActive);
        Assert.Equal(revokedAt, credential.RevokedAtUtc);
        Assert.Equal(2, credential.KeyVersion);
    }

    [Fact]
    public async Task DeviceEnrollmentRequiresProofOfPossessionAndConsumesChallengeOnce()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"device-enroll-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Device Enrollment Store",
            slug = $"device-enroll-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var deviceId = Guid.NewGuid();
        var challengeResponse = await client.PostAsJsonAsync("/api/sync/enrollment/challenges", new { deviceId });
        Assert.Equal(HttpStatusCode.OK, challengeResponse.StatusCode);
        var challenge = await challengeResponse.Content.ReadFromJsonAsync<DeviceEnrollmentChallengeResponse>();
        Assert.NotNull(challenge);

        using var key = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        var publicKey = Convert.ToBase64String(key.ExportSubjectPublicKeyInfo());
        var signedMessage = string.Join("\n",
            "MG-DEVICE-ENROLLMENT-V1",
            tenant.TenantId.ToString("D"),
            tenant.BranchId.ToString("D"),
            challenge!.ActorId.ToString("D"),
            deviceId.ToString("D"),
            challenge.ChallengeId.ToString("D"),
            challenge.Challenge);
        var signature = Convert.ToBase64String(key.SignData(
            Encoding.UTF8.GetBytes(signedMessage),
            HashAlgorithmName.SHA256,
            DSASignatureFormat.IeeeP1363FixedFieldConcatenation));
        var completionRequest = new
        {
            challengeId = challenge.ChallengeId,
            deviceId,
            challenge = challenge.Challenge,
            publicKeySpkiBase64 = publicKey,
            signatureBase64 = signature
        };

        var completed = await client.PostAsJsonAsync("/api/sync/enrollment/complete", completionRequest);
        Assert.Equal(HttpStatusCode.Created, completed.StatusCode);
        var result = await completed.Content.ReadFromJsonAsync<DeviceEnrollmentResult>();
        Assert.NotNull(result);
        Assert.Equal(deviceId, result!.DeviceId);
        Assert.Equal(1, result.KeyVersion);
        Assert.False(string.IsNullOrWhiteSpace(result.Credential));

        using var scope = factory.Services.CreateScope();
        var db = scope.ServiceProvider.GetRequiredService<BusinessDbContext>();
        var persisted = await db.DeviceCredentials.SingleAsync(
            x => x.TenantId == tenant.TenantId && x.DeviceId == deviceId);
        Assert.Equal(challenge.ActorId, persisted.ActorId);
        Assert.NotEqual(result.Credential, persisted.CredentialSha256);
        Assert.Equal(Convert.ToHexString(SHA256.HashData(Convert.FromBase64String(result.Credential))).ToLowerInvariant(),
            persisted.CredentialSha256);

        var replay = await client.PostAsJsonAsync("/api/sync/enrollment/complete", completionRequest);
        Assert.Equal(HttpStatusCode.Conflict, replay.StatusCode);

        // A new single-use challenge plus the same persisted key recovers a
        // lost one-time credential response by rotating the server-side digest.
        var recoveryChallengeResponse = await client.PostAsJsonAsync(
            "/api/sync/enrollment/challenges", new { deviceId });
        Assert.Equal(HttpStatusCode.OK, recoveryChallengeResponse.StatusCode);
        var recoveryChallenge = await recoveryChallengeResponse.Content.ReadFromJsonAsync<DeviceEnrollmentChallengeResponse>();
        Assert.NotNull(recoveryChallenge);
        var recoveryMessage = string.Join("\n",
            "MG-DEVICE-ENROLLMENT-V1",
            tenant.TenantId.ToString("D"),
            tenant.BranchId.ToString("D"),
            recoveryChallenge!.ActorId.ToString("D"),
            deviceId.ToString("D"),
            recoveryChallenge.ChallengeId.ToString("D"),
            recoveryChallenge.Challenge);
        var recoverySignature = Convert.ToBase64String(key.SignData(
            Encoding.UTF8.GetBytes(recoveryMessage),
            HashAlgorithmName.SHA256,
            DSASignatureFormat.IeeeP1363FixedFieldConcatenation));
        var recoveredResponse = await client.PostAsJsonAsync("/api/sync/enrollment/complete", new
        {
            challengeId = recoveryChallenge.ChallengeId,
            deviceId,
            challenge = recoveryChallenge.Challenge,
            publicKeySpkiBase64 = publicKey,
            signatureBase64 = recoverySignature
        });
        Assert.Equal(HttpStatusCode.OK, recoveredResponse.StatusCode);
        var recovered = await recoveredResponse.Content.ReadFromJsonAsync<DeviceEnrollmentResult>();
        Assert.NotNull(recovered);
        Assert.Equal(2, recovered!.KeyVersion);
        Assert.NotEqual(result.Credential, recovered.Credential);
        await db.Entry(persisted).ReloadAsync();
        Assert.Equal(2, persisted.KeyVersion);
        Assert.Equal(Convert.ToHexString(SHA256.HashData(Convert.FromBase64String(recovered.Credential))).ToLowerInvariant(),
            persisted.CredentialSha256);
    }

    [Fact]
    public async Task DeviceEnrollmentRejectsSignatureForAlteredChallengeContext()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"device-enroll-tamper-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Device Enrollment Tamper Store",
            slug = $"device-enroll-tamper-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var deviceId = Guid.NewGuid();
        var challengeResponse = await client.PostAsJsonAsync("/api/sync/enrollment/challenges", new { deviceId });
        var challenge = await challengeResponse.Content.ReadFromJsonAsync<DeviceEnrollmentChallengeResponse>();
        Assert.NotNull(challenge);

        using var key = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        var signedMessage = string.Join("\n",
            "MG-DEVICE-ENROLLMENT-V1",
            tenant.TenantId.ToString("D"),
            tenant.BranchId.ToString("D"),
            challenge!.ActorId.ToString("D"),
            deviceId.ToString("D"),
            challenge.ChallengeId.ToString("D"),
            challenge.Challenge);
        var signature = Convert.ToBase64String(key.SignData(
            Encoding.UTF8.GetBytes(signedMessage),
            HashAlgorithmName.SHA256,
            DSASignatureFormat.IeeeP1363FixedFieldConcatenation));

        var response = await client.PostAsJsonAsync("/api/sync/enrollment/complete", new
        {
            challengeId = challenge.ChallengeId,
            deviceId,
            challenge = Convert.ToBase64String(RandomNumberGenerator.GetBytes(32)),
            publicKeySpkiBase64 = Convert.ToBase64String(key.ExportSubjectPublicKeyInfo()),
            signatureBase64 = signature
        });
        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    private sealed record DeviceEnrollmentChallengeResponse(
        Guid ChallengeId, string Challenge, Guid TenantId, Guid? BranchId, Guid ActorId,
        Guid DeviceId, DateTimeOffset ExpiresAtUtc, string Algorithm);

    private sealed record DeviceEnrollmentResult(Guid DeviceId, int KeyVersion, string Credential);
    private sealed record AuthorityLeaseResponse(Guid LeaseId, Guid TenantId, Guid? BranchId, string ScopeKey, Guid DeviceId, long Epoch, DateTimeOffset IssuedAtUtc, DateTimeOffset ExpiresAtUtc, string SignatureBase64, string PublicKeySpkiBase64);

    [Fact]
    public async Task SignedSyncRequestsRejectReplayAndBodyTampering()
    {
        using var factory = CreateFactory(requireDeviceSignatures: true);
        using var client = await RegisterAndLoginAsync(factory, $"sync-signed-{Guid.NewGuid():N}@mg.test");
        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Signed Sync Store",
            slug = $"sync-signed-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.NotNull(tenant);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var deviceId = Guid.NewGuid();
        var challengeResponse = await client.PostAsJsonAsync("/api/sync/enrollment/challenges", new { deviceId });
        Assert.Equal(HttpStatusCode.OK, challengeResponse.StatusCode);
        var challenge = await challengeResponse.Content.ReadFromJsonAsync<DeviceEnrollmentChallengeResponse>();
        Assert.NotNull(challenge);

        using var key = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        var publicKey = Convert.ToBase64String(key.ExportSubjectPublicKeyInfo());
        var enrollmentMessage = string.Join("\n",
            "MG-DEVICE-ENROLLMENT-V1",
            tenant.TenantId.ToString("D"),
            tenant.BranchId.ToString("D"),
            challenge!.ActorId.ToString("D"),
            deviceId.ToString("D"),
            challenge.ChallengeId.ToString("D"),
            challenge.Challenge);
        var enrollmentSignature = Convert.ToBase64String(key.SignData(
            Encoding.UTF8.GetBytes(enrollmentMessage),
            HashAlgorithmName.SHA256,
            DSASignatureFormat.IeeeP1363FixedFieldConcatenation));
        var completed = await client.PostAsJsonAsync("/api/sync/enrollment/complete", new
        {
            challengeId = challenge.ChallengeId,
            deviceId,
            challenge = challenge.Challenge,
            publicKeySpkiBase64 = publicKey,
            signatureBase64 = enrollmentSignature
        });
        Assert.Equal(HttpStatusCode.Created, completed.StatusCode);
        var credential = await completed.Content.ReadFromJsonAsync<DeviceEnrollmentResult>();
        Assert.NotNull(credential);

        async Task<HttpResponseMessage> Send(string signedBody, string actualBody, Guid nonce)
        {
            var timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            var bodyHash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(signedBody))).ToLowerInvariant();
            var canonical = string.Join("\n",
                "MG-DEVICE-REQUEST-V1",
                "POST",
                "/api/sync/heartbeat",
                tenant.TenantId.ToString("D"),
                tenant.BranchId.ToString("D"),
                challenge.ActorId.ToString("D"),
                deviceId.ToString("D"),
                "1",
                timestamp.ToString(System.Globalization.CultureInfo.InvariantCulture),
                nonce.ToString("D"),
                bodyHash);
            var signature = Convert.ToBase64String(key.SignData(
                Encoding.UTF8.GetBytes(canonical),
                HashAlgorithmName.SHA256,
                DSASignatureFormat.IeeeP1363FixedFieldConcatenation));
            using var request = new HttpRequestMessage(HttpMethod.Post, "/api/sync/heartbeat")
            {
                Content = new StringContent(actualBody, Encoding.UTF8, "application/json")
            };
            request.Headers.TryAddWithoutValidation("X-MG-Device-Id", deviceId.ToString("D"));
            request.Headers.TryAddWithoutValidation("X-MG-Device-Key-Version", "1");
            request.Headers.TryAddWithoutValidation("X-MG-Device-Credential", credential!.Credential);
            request.Headers.TryAddWithoutValidation("X-MG-Device-Timestamp", timestamp.ToString(System.Globalization.CultureInfo.InvariantCulture));
            request.Headers.TryAddWithoutValidation("X-MG-Device-Nonce", nonce.ToString("D"));
            request.Headers.TryAddWithoutValidation("X-MG-Device-Signature", signature);
            return await client.SendAsync(request);
        }

        var body = System.Text.Json.JsonSerializer.Serialize(new
        {
            deviceId,
            deviceName = "Signed Browser",
            platform = "Test",
            clientVersion = "1.0"
        });
        var nonce = Guid.NewGuid();
        using var accepted = await Send(body, body, nonce);
        Assert.Equal(HttpStatusCode.OK, accepted.StatusCode);

        using var replay = await Send(body, body, nonce);
        Assert.Equal(HttpStatusCode.Unauthorized, replay.StatusCode);

        var tamperedBody = body.Replace("Signed Browser", "Tampered Browser", StringComparison.Ordinal);
        using var tampered = await Send(body, tamperedBody, Guid.NewGuid());
        Assert.Equal(HttpStatusCode.Unauthorized, tampered.StatusCode);
    }


    [Fact]
    public void AuthorityLeaseFencesCompetingDeviceAndIncrementsEpochAfterExpiry()
    {
        var now = DateTimeOffset.UtcNow;
        var tenantId = Guid.NewGuid();
        var branchId = Guid.NewGuid();
        var firstDevice = Guid.NewGuid();
        var secondDevice = Guid.NewGuid();
        var lease = new DeviceAuthorityLease(
            Guid.NewGuid(), tenantId, branchId.ToString("N"), branchId,
            firstDevice, now, now.AddMinutes(2));
        lease.SetSignature(Convert.ToBase64String(new byte[64]));

        Assert.Equal(1, lease.Epoch);
        Assert.False(lease.TryAcquire(secondDevice, now.AddSeconds(30), now.AddMinutes(2)));
        Assert.Equal(firstDevice, lease.DeviceId);
        Assert.Equal(1, lease.Epoch);

        Assert.True(lease.TryAcquire(firstDevice, now.AddSeconds(30), now.AddMinutes(2)));
        Assert.Equal(1, lease.Epoch);

        var epochStartedAt = lease.IssuedAtUtc;
        Assert.True(lease.TryAcquire(firstDevice, now.AddMinutes(3), now.AddMinutes(5)));
        Assert.Equal(firstDevice, lease.DeviceId);
        Assert.Equal(2, lease.Epoch);
        Assert.Equal(now.AddMinutes(3), lease.IssuedAtUtc);
        Assert.NotEqual(epochStartedAt, lease.IssuedAtUtc);

        Assert.True(lease.TryAcquire(secondDevice, now.AddMinutes(6), now.AddMinutes(8)));
        Assert.Equal(secondDevice, lease.DeviceId);
        Assert.Equal(3, lease.Epoch);
        Assert.NotEqual(Guid.Empty, lease.LeaseId);
        Assert.True(lease.IsActiveAt(now.AddMinutes(6)));
    }
}

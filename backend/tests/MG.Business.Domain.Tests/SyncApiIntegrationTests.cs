using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using MG.Business.Domain.Entities;
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
        var unregisteredAck = await client.PostAsJsonAsync("/api/sync/acknowledgements", new { deviceId, sequence = 0 });
        Assert.Equal(HttpStatusCode.Forbidden, unregisteredAck.StatusCode);

        var heartbeat = await client.PostAsJsonAsync("/api/sync/heartbeat", new
        {
            deviceId,
            deviceName = "Registered Sync Test Device",
            platform = "Test",
            clientVersion = "0.1.0"
        });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);

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
            sequence = firstAck.Accepted[0].Sequence
        });
        Assert.Equal(HttpStatusCode.OK, acknowledgement.StatusCode);
        var acknowledgedCursor = await acknowledgement.Content.ReadFromJsonAsync<SyncCursorResponse>();
        Assert.Equal(firstAck.Accepted[0].Sequence, acknowledgedCursor!.Sequence);
        Assert.False(acknowledgedCursor.StaleAcknowledgement);

        var futureAcknowledgement = await client.PostAsJsonAsync("/api/sync/acknowledgements", new
        {
            deviceId,
            sequence = firstAck.Accepted[0].Sequence + 100000
        });
        Assert.Equal(HttpStatusCode.BadRequest, futureAcknowledgement.StatusCode);

        var staleAcknowledgement = await client.PostAsJsonAsync("/api/sync/acknowledgements", new
        {
            deviceId,
            sequence = 0
        });
        var staleCursor = await staleAcknowledgement.Content.ReadFromJsonAsync<SyncCursorResponse>();
        Assert.True(staleCursor!.StaleAcknowledgement);
        Assert.Equal(firstAck.Accepted[0].Sequence, staleCursor.Sequence);

        var heartbeat = await client.PostAsJsonAsync("/api/sync/heartbeat", new { deviceId, deviceName = "Sync Test Device", platform = "Test", clientVersion = "0.1.0" });
        Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);
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
            sequence = firstAck.Accepted[0].Sequence
        });
        Assert.Equal(HttpStatusCode.Forbidden, crossTenantAck.StatusCode);
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
    private sealed record SyncPushResponse(SyncAck[] Accepted);
    private sealed record SyncAck(Guid OperationId, long Sequence, bool Duplicate);
    private sealed record SyncCursorResponse(Guid DeviceId, long Sequence, bool StaleAcknowledgement);
    private sealed record SyncPullResponse(SyncOperationResponse[] Operations, long NextSequence, bool HasMore);
    private sealed record DevicePresenceResponse(Guid DeviceId, Guid ActorId, Guid? BranchId, string DeviceName, string Platform, string ClientVersion, DateTimeOffset FirstSeenAtUtc, DateTimeOffset LastSeenAtUtc, long LastAcknowledgedSequence, long LatestJournalSequence, long PendingOperationCount, bool IsOnline, string? AlertCode);
    private sealed record SyncOperationResponse(long Sequence, Guid OperationId, Guid DeviceId, Guid? BranchId,
        string OperationType, int SchemaVersion, long AuthorityEpoch, DateTimeOffset OccurredAtUtc,
        DateTimeOffset ReceivedAtUtc, string PayloadJson, string PayloadSha256);
}

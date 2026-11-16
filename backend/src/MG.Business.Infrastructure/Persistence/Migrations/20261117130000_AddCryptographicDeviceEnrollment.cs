using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

namespace MG.Business.Infrastructure.Persistence.Migrations;

[DbContext(typeof(BusinessDbContext))]
[Migration("20261117130000_AddCryptographicDeviceEnrollment")]
public sealed class AddCryptographicDeviceEnrollment : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.CreateTable(
            name: "device_credentials",
            columns: table => new
            {
                TenantId = table.Column<Guid>(type: "uuid", nullable: false),
                DeviceId = table.Column<Guid>(type: "uuid", nullable: false),
                ActorId = table.Column<Guid>(type: "uuid", nullable: false),
                PublicKeySpkiBase64 = table.Column<string>(type: "character varying(2048)", maxLength: 2048, nullable: false),
                CredentialSha256 = table.Column<string>(type: "character varying(64)", maxLength: 64, nullable: false),
                KeyVersion = table.Column<int>(type: "integer", nullable: false),
                CreatedAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false),
                LastRotatedAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false),
                RevokedAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: true),
                LastUsedAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: true)
            },
            constraints: table =>
            {
                table.PrimaryKey("PK_device_credentials", x => new { x.TenantId, x.DeviceId });
                table.ForeignKey("FK_device_credentials_tenants_TenantId", x => x.TenantId, "tenants", "Id", onDelete: ReferentialAction.Cascade);
                table.ForeignKey("FK_device_credentials_AspNetUsers_ActorId", x => x.ActorId, "AspNetUsers", "Id", onDelete: ReferentialAction.Cascade);
            });

        migrationBuilder.CreateTable(
            name: "device_enrollment_challenges",
            columns: table => new
            {
                Id = table.Column<Guid>(type: "uuid", nullable: false),
                TenantId = table.Column<Guid>(type: "uuid", nullable: false),
                ActorId = table.Column<Guid>(type: "uuid", nullable: false),
                BranchId = table.Column<Guid>(type: "uuid", nullable: true),
                DeviceId = table.Column<Guid>(type: "uuid", nullable: false),
                ChallengeSha256 = table.Column<string>(type: "character varying(64)", maxLength: 64, nullable: false),
                CreatedAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false),
                ExpiresAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false),
                ConsumedAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: true)
            },
            constraints: table =>
            {
                table.PrimaryKey("PK_device_enrollment_challenges", x => x.Id);
                table.ForeignKey("FK_device_enrollment_challenges_tenants_TenantId", x => x.TenantId, "tenants", "Id", onDelete: ReferentialAction.Cascade);
                table.ForeignKey("FK_device_enrollment_challenges_AspNetUsers_ActorId", x => x.ActorId, "AspNetUsers", "Id", onDelete: ReferentialAction.Cascade);
                table.ForeignKey("FK_device_enrollment_challenges_branches_TenantId_BranchId", x => new { x.TenantId, x.BranchId }, "branches", new[] { "TenantId", "Id" }, onDelete: ReferentialAction.Cascade);
            });

        migrationBuilder.CreateIndex(name: "IX_device_credentials_TenantId_ActorId", table: "device_credentials", columns: new[] { "TenantId", "ActorId" });
        migrationBuilder.CreateIndex(name: "IX_device_enrollment_challenges_ActorId", table: "device_enrollment_challenges", column: "ActorId");
        migrationBuilder.CreateIndex(name: "IX_device_enrollment_challenges_TenantId_ActorId_DeviceId_ExpiresAtUtc", table: "device_enrollment_challenges", columns: new[] { "TenantId", "ActorId", "DeviceId", "ExpiresAtUtc" });
        migrationBuilder.CreateIndex(name: "IX_device_enrollment_challenges_TenantId_BranchId", table: "device_enrollment_challenges", columns: new[] { "TenantId", "BranchId" });
        migrationBuilder.CreateIndex(name: "IX_device_enrollment_challenges_ExpiresAtUtc", table: "device_enrollment_challenges", column: "ExpiresAtUtc");
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropTable(name: "device_enrollment_challenges");
        migrationBuilder.DropTable(name: "device_credentials");
    }
}

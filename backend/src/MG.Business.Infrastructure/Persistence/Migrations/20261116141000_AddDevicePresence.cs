using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace MG.Business.Infrastructure.Persistence.Migrations;

[DbContext(typeof(BusinessDbContext))]
[Migration("20261116141000_AddDevicePresence")]
public partial class AddDevicePresence : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.CreateTable(
            name: "device_presences",
            columns: table => new
            {
                TenantId = table.Column<Guid>(type: "uuid", nullable: false),
                DeviceId = table.Column<Guid>(type: "uuid", nullable: false),
                ActorId = table.Column<Guid>(type: "uuid", nullable: false),
                BranchId = table.Column<Guid>(type: "uuid", nullable: true),
                DeviceName = table.Column<string>(type: "character varying(100)", maxLength: 100, nullable: false),
                Platform = table.Column<string>(type: "character varying(40)", maxLength: 40, nullable: false),
                ClientVersion = table.Column<string>(type: "character varying(40)", maxLength: 40, nullable: false),
                FirstSeenAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false),
                LastSeenAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false)
            },
            constraints: table =>
            {
                table.PrimaryKey("PK_device_presences", x => new { x.TenantId, x.DeviceId });
                table.ForeignKey("FK_device_presences_AspNetUsers_ActorId", x => x.ActorId,
                    principalTable: "AspNetUsers", principalColumn: "Id", onDelete: ReferentialAction.Restrict);
                table.ForeignKey("FK_device_presences_branches_TenantId_BranchId", x => new { x.TenantId, x.BranchId },
                    principalTable: "branches", principalColumns: new[] { "TenantId", "Id" }, onDelete: ReferentialAction.Restrict);
                table.ForeignKey("FK_device_presences_tenants_TenantId", x => x.TenantId,
                    principalTable: "tenants", principalColumn: "Id", onDelete: ReferentialAction.Restrict);
            });

        migrationBuilder.CreateIndex(name: "IX_device_presences_ActorId", table: "device_presences", column: "ActorId");
        migrationBuilder.CreateIndex(name: "IX_device_presences_TenantId_BranchId", table: "device_presences", columns: new[] { "TenantId", "BranchId" });
        migrationBuilder.CreateIndex(name: "IX_device_presences_TenantId_LastSeenAtUtc", table: "device_presences", columns: new[] { "TenantId", "LastSeenAtUtc" });
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropTable(name: "device_presences");
    }
}

using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace MG.Business.Infrastructure.Persistence.Migrations;

[DbContext(typeof(BusinessDbContext))]
[Migration("20261116170000_AddBranchScopedSyncCursors")]
public partial class AddBranchScopedSyncCursors : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.CreateTable(
            name: "sync_device_branch_cursors",
            columns: table => new
            {
                TenantId = table.Column<Guid>(type: "uuid", nullable: false),
                DeviceId = table.Column<Guid>(type: "uuid", nullable: false),
                BranchId = table.Column<Guid>(type: "uuid", nullable: false),
                ActorId = table.Column<Guid>(type: "uuid", nullable: false),
                LastAcknowledgedSequence = table.Column<long>(type: "bigint", nullable: false),
                UpdatedAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false)
            },
            constraints: table =>
            {
                table.PrimaryKey("PK_sync_device_branch_cursors", x => new { x.TenantId, x.DeviceId, x.BranchId });
                table.ForeignKey("FK_sync_device_branch_cursors_AspNetUsers_ActorId", x => x.ActorId,
                    principalTable: "AspNetUsers", principalColumn: "Id", onDelete: ReferentialAction.Restrict);
                table.ForeignKey("FK_sync_device_branch_cursors_tenants_TenantId", x => x.TenantId,
                    principalTable: "tenants", principalColumn: "Id", onDelete: ReferentialAction.Restrict);
                table.ForeignKey("FK_sync_device_branch_cursors_branches_TenantId_BranchId", x => new { x.TenantId, x.BranchId },
                    principalTable: "branches", principalColumns: new[] { "TenantId", "Id" }, onDelete: ReferentialAction.Restrict);
            });

        migrationBuilder.CreateIndex(name: "IX_sync_device_branch_cursors_ActorId", table: "sync_device_branch_cursors", column: "ActorId");
        migrationBuilder.CreateIndex(name: "IX_sync_device_branch_cursor_scope_seq",
            table: "sync_device_branch_cursors", columns: new[] { "TenantId", "BranchId", "LastAcknowledgedSequence" });
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropTable(name: "sync_device_branch_cursors");
    }
}

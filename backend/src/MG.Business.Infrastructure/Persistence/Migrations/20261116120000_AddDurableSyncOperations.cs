using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;
using Npgsql.EntityFrameworkCore.PostgreSQL.Metadata;

#nullable disable

namespace MG.Business.Infrastructure.Persistence.Migrations;

[DbContext(typeof(BusinessDbContext))]
[Migration("20261116120000_AddDurableSyncOperations")]
public partial class AddDurableSyncOperations : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.CreateTable(
            name: "sync_operations",
            columns: table => new
            {
                Sequence = table.Column<long>(type: "bigint", nullable: false)
                    .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                Id = table.Column<Guid>(type: "uuid", nullable: false),
                TenantId = table.Column<Guid>(type: "uuid", nullable: false),
                ActorId = table.Column<Guid>(type: "uuid", nullable: false),
                DeviceId = table.Column<Guid>(type: "uuid", nullable: false),
                BranchId = table.Column<Guid>(type: "uuid", nullable: true),
                OperationId = table.Column<Guid>(type: "uuid", nullable: false),
                OperationType = table.Column<string>(type: "character varying(100)", maxLength: 100, nullable: false),
                SchemaVersion = table.Column<int>(type: "integer", nullable: false),
                AuthorityEpoch = table.Column<long>(type: "bigint", nullable: false),
                OccurredAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false),
                ReceivedAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false),
                PayloadJson = table.Column<string>(type: "text", nullable: false),
                PayloadSha256 = table.Column<string>(type: "character varying(64)", maxLength: 64, nullable: false)
            },
            constraints: table =>
            {
                table.PrimaryKey("PK_sync_operations", x => x.Sequence);
                table.ForeignKey("FK_sync_operations_AspNetUsers_ActorId", x => x.ActorId,
                    principalTable: "AspNetUsers", principalColumn: "Id", onDelete: ReferentialAction.Restrict);
                table.ForeignKey("FK_sync_operations_branches_TenantId_BranchId", x => new { x.TenantId, x.BranchId },
                    principalTable: "branches", principalColumns: new[] { "TenantId", "Id" }, onDelete: ReferentialAction.Restrict);
                table.ForeignKey("FK_sync_operations_tenants_TenantId", x => x.TenantId,
                    principalTable: "tenants", principalColumn: "Id", onDelete: ReferentialAction.Restrict);
            });

        migrationBuilder.CreateIndex(name: "IX_sync_operations_TenantId_BranchId", table: "sync_operations", columns: new[] { "TenantId", "BranchId" });
        migrationBuilder.CreateIndex(name: "IX_sync_operations_ActorId", table: "sync_operations", column: "ActorId");
        migrationBuilder.CreateIndex(name: "IX_sync_operations_TenantId_OperationId",
            table: "sync_operations", columns: new[] { "TenantId", "OperationId" }, unique: true);
        migrationBuilder.CreateIndex(name: "IX_sync_operations_TenantId_Sequence",
            table: "sync_operations", columns: new[] { "TenantId", "Sequence" });
        migrationBuilder.CreateIndex(name: "IX_sync_operations_TenantId_DeviceId_Sequence",
            table: "sync_operations", columns: new[] { "TenantId", "DeviceId", "Sequence" });
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropTable(name: "sync_operations");
    }
}

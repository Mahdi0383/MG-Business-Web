using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

namespace MG.Business.Infrastructure.Persistence.Migrations;

[DbContext(typeof(BusinessDbContext))]
[Migration("20261117110000_AddStockMovementIdempotency")]
public sealed class AddStockMovementIdempotency : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.AddColumn<string>(name: "IdempotencyKey", table: "stock_movements", type: "character varying(128)", maxLength: 128, nullable: true);
        migrationBuilder.AddColumn<string>(name: "RequestFingerprint", table: "stock_movements", type: "character varying(64)", maxLength: 64, nullable: true);
        migrationBuilder.CreateIndex(name: "IX_stock_movements_TenantId_BranchId_IdempotencyKey", table: "stock_movements",
            columns: new[] { "TenantId", "BranchId", "IdempotencyKey" }, unique: true);
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropIndex(name: "IX_stock_movements_TenantId_BranchId_IdempotencyKey", table: "stock_movements");
        migrationBuilder.DropColumn(name: "IdempotencyKey", table: "stock_movements");
        migrationBuilder.DropColumn(name: "RequestFingerprint", table: "stock_movements");
    }
}

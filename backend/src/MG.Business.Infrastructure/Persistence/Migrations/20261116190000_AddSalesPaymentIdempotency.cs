using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

namespace MG.Business.Infrastructure.Persistence.Migrations;

[DbContext(typeof(BusinessDbContext))]
[Migration("20261116190000_AddSalesPaymentIdempotency")]
public sealed class AddSalesPaymentIdempotency : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.AddColumn<string>(name: "IdempotencyKey", table: "sales_payments", type: "character varying(128)", maxLength: 128, nullable: true);
        migrationBuilder.AddColumn<string>(name: "RequestFingerprint", table: "sales_payments", type: "character varying(64)", maxLength: 64, nullable: true);
        migrationBuilder.CreateIndex(name: "IX_sales_payments_TenantId_BranchId_IdempotencyKey", table: "sales_payments",
            columns: new[] { "TenantId", "BranchId", "IdempotencyKey" }, unique: true);
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropIndex(name: "IX_sales_payments_TenantId_BranchId_IdempotencyKey", table: "sales_payments");
        migrationBuilder.DropColumn(name: "IdempotencyKey", table: "sales_payments");
        migrationBuilder.DropColumn(name: "RequestFingerprint", table: "sales_payments");
    }
}

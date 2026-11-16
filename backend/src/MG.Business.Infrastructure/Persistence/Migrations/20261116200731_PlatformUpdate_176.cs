using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace MG.Business.Infrastructure.Persistence.Migrations
{
    /// <inheritdoc />
    public partial class PlatformUpdate_176 : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropIndex(
                name: "IX_sales_payments_TenantId_BranchId",
                table: "sales_payments");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.CreateIndex(
                name: "IX_sales_payments_TenantId_BranchId",
                table: "sales_payments",
                columns: new[] { "TenantId", "BranchId" });
        }
    }
}

using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace MG.Business.Infrastructure.Persistence.Migrations
{
    /// <inheritdoc />
    public partial class PlatformUpdate_95 : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.AddColumn<decimal>(
                name: "DefaultSalePrice",
                table: "products",
                type: "numeric(18,2)",
                precision: 18,
                scale: 2,
                nullable: false,
                defaultValue: 0m);
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropColumn(
                name: "DefaultSalePrice",
                table: "products");
        }
    }
}

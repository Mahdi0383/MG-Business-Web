using System;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace MG.Business.Infrastructure.Persistence.Migrations
{
    /// <inheritdoc />
    public partial class AddDeviceAuthorityLeases : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.CreateTable(
                name: "device_authority_leases",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    LeaseId = table.Column<Guid>(type: "uuid", nullable: false),
                    TenantId = table.Column<Guid>(type: "uuid", nullable: false),
                    ScopeKey = table.Column<string>(type: "character varying(36)", maxLength: 36, nullable: false),
                    BranchId = table.Column<Guid>(type: "uuid", nullable: true),
                    DeviceId = table.Column<Guid>(type: "uuid", nullable: false),
                    Epoch = table.Column<long>(type: "bigint", nullable: false),
                    IssuedAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false),
                    ExpiresAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false),
                    RevokedAtUtc = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: true),
                    SignatureBase64 = table.Column<string>(type: "character varying(256)", maxLength: 256, nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_device_authority_leases", x => x.Id);
                    table.ForeignKey(
                        name: "FK_device_authority_leases_branches_TenantId_BranchId",
                        columns: x => new { x.TenantId, x.BranchId },
                        principalTable: "branches",
                        principalColumns: new[] { "TenantId", "Id" },
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_device_authority_leases_device_credentials_TenantId_DeviceId",
                        columns: x => new { x.TenantId, x.DeviceId },
                        principalTable: "device_credentials",
                        principalColumns: new[] { "TenantId", "DeviceId" },
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_device_authority_leases_tenants_TenantId",
                        column: x => x.TenantId,
                        principalTable: "tenants",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateIndex(
                name: "IX_device_authority_leases_TenantId_BranchId",
                table: "device_authority_leases",
                columns: new[] { "TenantId", "BranchId" });

            migrationBuilder.CreateIndex(
                name: "IX_device_authority_leases_TenantId_DeviceId_ExpiresAtUtc",
                table: "device_authority_leases",
                columns: new[] { "TenantId", "DeviceId", "ExpiresAtUtc" });

            migrationBuilder.CreateIndex(
                name: "IX_device_authority_leases_TenantId_ScopeKey",
                table: "device_authority_leases",
                columns: new[] { "TenantId", "ScopeKey" },
                unique: true);
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropTable(
                name: "device_authority_leases");
        }
    }
}

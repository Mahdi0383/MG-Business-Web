using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Endpoints;

public static class InventoryEndpoints
{
    private static readonly string[] ReadRoles = ["Owner", "Manager", "Accountant", "Cashier", "Waiter", "GameOperator", "InventoryManager", "Viewer"];
    private static readonly string[] WriteRoles = ["Owner", "Manager", "InventoryManager"];

    public static IEndpointRouteBuilder MapInventoryEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/inventory").RequireAuthorization("TenantMember");

        group.MapGet("/products", async (ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var products = await db.Products.AsNoTracking()
                .Where(x => x.TenantId == tenant.TenantId)
                .OrderBy(x => x.Name)
                .Select(x => new ProductResponse(x.Id, x.Sku, x.Name, x.BaseUnit, x.TracksInventory, x.IsActive))
                .ToListAsync(cancellationToken);
            return Results.Ok(products);
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ReadRoles)));

        group.MapPost("/products", async (CreateProductRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId)
                return Results.Forbid();
            var product = new Product(tenantId, request.Sku, request.Name, request.BaseUnit, request.TracksInventory);
            db.Products.Add(product);
            try
            {
                await db.SaveChangesAsync(cancellationToken);
            }
            catch (DbUpdateException)
            {
                return Results.Conflict(new { message = "کد کالا تکراری است یا اطلاعات با محدودیت پایگاه داده سازگار نیست." });
            }

            return Results.Created($"/api/inventory/products/{product.Id}",
                new ProductResponse(product.Id, product.Sku, product.Name, product.BaseUnit, product.TracksInventory, product.IsActive));
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        group.MapGet("/stock", async (ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var query = db.StockMovements.AsNoTracking().Where(x => x.TenantId == tenant.TenantId);
            if (tenant.BranchId is Guid branchId)
                query = query.Where(x => x.BranchId == branchId);

            var movements = await query.ToListAsync(cancellationToken);
            var result = movements.GroupBy(x => x.ProductId).Select(grouping => new
            {
                productId = grouping.Key,
                quantityOnHand = grouping.Sum(x => x.SignedQuantity),
                movementCount = grouping.Count(),
                inventoryValueAtMovementCost = grouping.Sum(x => x.SignedQuantity * x.UnitCost)
            }).ToList();

            return Results.Ok(result);
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ReadRoles)));

        group.MapPost("/movements", async (CreateStockMovementRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.BranchId is not Guid branchId)
                return Results.BadRequest(new { message = "ابتدا شعبه فعال را انتخاب کنید." });

            var productExists = await db.Products.AnyAsync(x =>
                x.TenantId == tenantId && x.Id == request.ProductId && x.IsActive && x.TracksInventory,
                cancellationToken);
            if (!productExists)
                return Results.NotFound(new { message = "کالای فعالِ انبارگردانی‌شونده در این کسب‌وکار پیدا نشد." });

            var movement = new StockMovement(tenantId, branchId, request.ProductId, request.Type,
                request.Quantity, request.UnitCost, request.Reference);
            db.StockMovements.Add(movement);
            await db.SaveChangesAsync(cancellationToken);

            return Results.Created($"/api/inventory/movements/{movement.Id}", new
            {
                movement.Id,
                movement.ProductId,
                movement.BranchId,
                type = movement.Type.ToString(),
                movement.Quantity,
                movement.UnitCost,
                movement.SignedQuantity,
                movement.Reference,
                movement.CreatedAtUtc
            });
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        return endpoints;
    }

    public sealed record CreateProductRequest(string Sku, string Name, string BaseUnit, bool TracksInventory = true);
    public sealed record ProductResponse(Guid Id, string Sku, string Name, string BaseUnit, bool TracksInventory, bool IsActive);
    public sealed record CreateStockMovementRequest(Guid ProductId, StockMovementType Type, decimal Quantity, decimal UnitCost, string? Reference);
}

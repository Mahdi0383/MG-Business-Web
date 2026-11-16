using System.Data;
using System.Data.Common;
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
                .Select(x => new ProductResponse(x.Id, x.Sku, x.Name, x.BaseUnit, x.TracksInventory, x.DefaultSalePrice, x.IsActive))
                .ToListAsync(cancellationToken);
            return Results.Ok(products);
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ReadRoles)));

        group.MapPost("/products", async (CreateProductRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId)
                return Results.Forbid();

            Product product;
            try
            {
                product = new Product(tenantId, request.Sku, request.Name, request.BaseUnit, request.TracksInventory, request.DefaultSalePrice);
            }
            catch (ArgumentException exception)
            {
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["product"] = [exception.Message] });
            }

            db.Products.Add(product);
            try
            {
                await db.SaveChangesAsync(cancellationToken);
            }
            catch (DbUpdateException)
            {
                return Results.Conflict(new { message = "کد کالا تکراری است یا اطلاعات با محدودیت پایگاه داده سازگار نیست." });
            }
            catch (DbException)
            {
                return Results.Conflict(new { message = "پایگاه داده نتوانست کالا را با اطمینان ثبت کند؛ اطلاعات را بررسی و دوباره تلاش کنید." });
            }

            return Results.Created($"/api/inventory/products/{product.Id}",
                new ProductResponse(product.Id, product.Sku, product.Name, product.BaseUnit, product.TracksInventory, product.DefaultSalePrice, product.IsActive));
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        group.MapGet("/stock", async (ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var query = db.StockMovements.AsNoTracking().Where(x => x.TenantId == tenant.TenantId);
            if (tenant.BranchId is Guid branchId)
                query = query.Where(x => x.BranchId == branchId);

            var totals = new Dictionary<(Guid ProductId, Guid BranchId), (InventoryValuationAccumulator Valuation, int Count)>();
            await foreach (var movement in query
                .OrderBy(x => x.ProductId).ThenBy(x => x.BranchId)
                .ThenBy(x => x.CreatedAtUtc).ThenBy(x => x.Id)
                .AsAsyncEnumerable().WithCancellation(cancellationToken))
            {
                var key = (movement.ProductId, movement.BranchId);
                if (!totals.TryGetValue(key, out var state))
                    state = (new InventoryValuationAccumulator(), 0);
                state.Valuation.Apply(movement);
                totals[key] = (state.Valuation, state.Count + 1);
            }

            var result = totals.Select(pair =>
            {
                var valuation = pair.Value.Valuation.ToValuation();
                return new
                {
                    ProductId = pair.Key.ProductId,
                    BranchId = pair.Key.BranchId,
                    valuation.QuantityOnHand,
                    valuation.AverageUnitCost,
                    valuation.InventoryValue,
                    MovementCount = pair.Value.Count
                };
            }).OrderBy(x => x.ProductId).ThenBy(x => x.BranchId).ToList();

            return Results.Ok(result);
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ReadRoles)));

        group.MapPost("/movements", async (CreateStockMovementRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.BranchId is not Guid branchId)
                return Results.BadRequest(new { message = "ابتدا شعبه فعال را انتخاب کنید." });
            if (!Enum.IsDefined(request.Type))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["type"] = ["نوع گردش انبار معتبر نیست."] });
            if (request.Quantity <= 0m || decimal.Round(request.Quantity, 3) != request.Quantity)
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["quantity"] = ["مقدار باید مثبت و حداکثر تا سه رقم اعشار باشد."] });

            var isInboundMovement = request.Type is StockMovementType.Receipt
                or StockMovementType.SalesReturn or StockMovementType.AdjustmentIncrease;
            if (isInboundMovement &&
                (request.UnitCost is null || request.UnitCost < 0m || decimal.Round(request.UnitCost.Value, 2) != request.UnitCost.Value))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["unitCost"] = ["بهای واحد برای ورود موجودی الزامی، غیرمنفی و حداکثر تا دو رقم اعشار است."] });
            if (!isInboundMovement && request.UnitCost is not null)
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["unitCost"] = ["برای خروج موجودی، بهای واحد به‌صورت خودکار از میانگین موجودی محاسبه می‌شود."] });

            var productExists = await db.Products.AnyAsync(x =>
                x.TenantId == tenantId && x.Id == request.ProductId && x.IsActive && x.TracksInventory,
                cancellationToken);
            if (!productExists)
                return Results.NotFound(new { message = "کالای فعالِ انبارگردانی‌شونده در این کسب‌وکار پیدا نشد." });

            await using var transaction = await db.Database.BeginTransactionAsync(IsolationLevel.Serializable, cancellationToken);
            try
            {
                var history = await db.StockMovements.AsNoTracking()
                    .Where(x => x.TenantId == tenantId && x.BranchId == branchId && x.ProductId == request.ProductId)
                    .ToListAsync(cancellationToken);
                var current = InventoryValuation.Calculate(history);
                decimal unitCost;
                if (isInboundMovement)
                {
                    unitCost = request.UnitCost!.Value;
                }
                else
                {
                    if (request.Quantity > current.QuantityOnHand)
                        return Results.Conflict(new { message = "موجودی کافی نیست؛ خروج موجودی منفی مجاز نیست.", current.QuantityOnHand });
                    unitCost = current.AverageUnitCost;
                }

                var movement = new StockMovement(tenantId, branchId, request.ProductId, request.Type,
                    request.Quantity, unitCost, request.Reference);
                db.StockMovements.Add(movement);
                await db.SaveChangesAsync(cancellationToken);
                await transaction.CommitAsync(cancellationToken);

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
            }
            catch (ArgumentException exception)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["movement"] = [exception.Message] });
            }
            catch (DbUpdateException)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = "تغییر هم‌زمان موجودی رخ داد؛ عملیات را دوباره بررسی کنید." });
            }
            catch (DbException)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = "تغییر هم‌زمان موجودی رخ داد؛ عملیات را دوباره بررسی کنید." });
            }
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        return endpoints;
    }

    public sealed record CreateProductRequest(string Sku, string Name, string BaseUnit, bool TracksInventory = true, decimal DefaultSalePrice = 0m);
    public sealed record ProductResponse(Guid Id, string Sku, string Name, string BaseUnit, bool TracksInventory, decimal DefaultSalePrice, bool IsActive);
    public sealed record CreateStockMovementRequest(Guid ProductId, StockMovementType Type, decimal Quantity, decimal? UnitCost, string? Reference);
}

using System.Data;
using System.Data.Common;
using System.Runtime.CompilerServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
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

        group.MapGet("/products/catalog", async (int? page, int? pageSize, string? search, bool? activeOnly, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var pageNumber = page ?? 1;
            var take = pageSize ?? 50;
            if (pageNumber is < 1 or > 1_000_000 || take is < 1 or > 200)
                return Results.ValidationProblem(new Dictionary<string, string[]>
                {
                    ["pagination"] = ["شماره صفحه باید مثبت و اندازه صفحه بین ۱ تا ۲۰۰ باشد."]
                });

            var normalizedSearch = string.IsNullOrWhiteSpace(search) ? null : search.Trim();
            if (normalizedSearch?.Length > 100)
                return Results.ValidationProblem(new Dictionary<string, string[]>
                {
                    ["search"] = ["عبارت جست‌وجو نمی‌تواند بیش از ۱۰۰ نویسه باشد."]
                });

            var query = db.Products.AsNoTracking().Where(x => x.TenantId == tenant.TenantId);
            if (activeOnly is true)
                query = query.Where(x => x.IsActive);
            if (normalizedSearch is not null)
                query = query.Where(x => x.Name.Contains(normalizedSearch) || x.Sku.Contains(normalizedSearch));

            var totalCount = await query.CountAsync(cancellationToken);
            var items = await query.OrderBy(x => x.Name).ThenBy(x => x.Id)
                .Skip((pageNumber - 1) * take).Take(take)
                .Select(x => new ProductResponse(x.Id, x.Sku, x.Name, x.BaseUnit, x.TracksInventory, x.DefaultSalePrice, x.IsActive))
                .ToListAsync(cancellationToken);

            return Results.Ok(new
            {
                page = pageNumber,
                pageSize = take,
                totalCount,
                totalPages = totalCount == 0 ? 0 : (int)Math.Ceiling(totalCount / (double)take),
                items
            });
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
            await foreach (var movement in ReadOrderedMovements(query, db.Database.IsSqlite(), cancellationToken))
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

        group.MapGet("/movements", async (int? page, int? pageSize, Guid? productId, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var pageNumber = page ?? 1;
            var take = pageSize ?? 50;
            if (pageNumber is < 1 or > 1_000_000 || take is < 1 or > 200)
                return Results.ValidationProblem(new Dictionary<string, string[]>
                {
                    ["pagination"] = ["شماره صفحه باید مثبت و اندازه صفحه بین ۱ تا ۲۰۰ باشد."]
                });

            var query = db.StockMovements.AsNoTracking()
                .Where(x => x.TenantId == tenant.TenantId);
            if (tenant.BranchId is Guid branchId)
                query = query.Where(x => x.BranchId == branchId);
            if (productId is Guid selectedProductId)
                query = query.Where(x => x.ProductId == selectedProductId);

            var totalCount = await query.CountAsync(cancellationToken);
            var totalPages = totalCount == 0 ? 0 : (int)Math.Ceiling(totalCount / (double)take);

            List<StockMovement> movements;
            if (db.Database.IsSqlite())
            {
                // SQLite cannot translate DateTimeOffset ordering; the tenant/branch/product
                // filters are still applied by the database before deterministic paging.
                var filtered = await query.ToListAsync(cancellationToken);
                movements = filtered.OrderByDescending(x => x.CreatedAtUtc)
                    .ThenByDescending(x => x.Id)
                    .Skip((pageNumber - 1) * take).Take(take).ToList();
            }
            else
            {
                movements = await query.OrderByDescending(x => x.CreatedAtUtc)
                    .ThenByDescending(x => x.Id)
                    .Skip((pageNumber - 1) * take).Take(take)
                    .ToListAsync(cancellationToken);
            }

            return Results.Ok(new
            {
                page = pageNumber,
                pageSize = take,
                totalCount,
                totalPages,
                items = movements.Select(x => new
                {
                    x.Id, x.ProductId, x.BranchId, type = x.Type.ToString(),
                    x.Quantity, x.UnitCost, x.SignedQuantity, x.Reference, x.CreatedAtUtc
                })
            });
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ReadRoles)));

        group.MapPost("/movements", async (CreateStockMovementRequest request, HttpRequest httpRequest, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
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

            string? idempotencyKey = null;
            if (httpRequest.Headers.TryGetValue("Idempotency-Key", out var idempotencyHeaders))
            {
                if (idempotencyHeaders.Count != 1)
                    return Results.ValidationProblem(new Dictionary<string, string[]> { ["Idempotency-Key"] = ["فقط یک کلید یکتای درخواست مجاز است."] });
                idempotencyKey = idempotencyHeaders[0];
                if (string.IsNullOrWhiteSpace(idempotencyKey) || idempotencyKey.Length > 128
                    || idempotencyKey.Any(ch => ch < 0x21 || ch > 0x7e))
                    return Results.ValidationProblem(new Dictionary<string, string[]> { ["Idempotency-Key"] = ["کلید باید بین ۱ تا ۱۲۸ نویسه قابل‌نمایش ASCII باشد."] });
            }
            if (idempotencyKey is null)
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["Idempotency-Key"] = ["برای ثبت گردش انبار، کلید یکتای درخواست الزامی است."] });

            var normalizedReference = string.IsNullOrWhiteSpace(request.Reference) ? null : request.Reference.Trim();
            var fingerprintInput = JsonSerializer.Serialize(new
            {
                request.ProductId,
                Type = (int)request.Type,
                request.Quantity,
                request.UnitCost,
                Reference = normalizedReference
            });
            var requestFingerprint = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(fingerprintInput))).ToLowerInvariant();

            await using var transaction = await db.Database.BeginTransactionAsync(IsolationLevel.Serializable, cancellationToken);
            try
            {
                if (idempotencyKey is not null)
                {
                    var prior = await db.StockMovements.AsNoTracking().SingleOrDefaultAsync(x =>
                        x.TenantId == tenantId && x.BranchId == branchId && x.IdempotencyKey == idempotencyKey,
                        cancellationToken);
                    if (prior is not null)
                    {
                        await transaction.RollbackAsync(cancellationToken);
                        if (!string.Equals(prior.RequestFingerprint, requestFingerprint, StringComparison.Ordinal))
                            return Results.Conflict(new { message = "این کلید یکتا قبلاً با محتوای دیگری استفاده شده است." });
                        return Results.Ok(new
                        {
                            prior.Id, prior.ProductId, prior.BranchId, type = prior.Type.ToString(),
                            prior.Quantity, prior.UnitCost, prior.SignedQuantity, prior.Reference,
                            prior.CreatedAtUtc, replayed = true
                        });
                    }
                }

                // Resolve the product and read its stock ledger inside the same serializable
                // transaction as the movement write, preventing stale pre-transaction checks.
                var productExists = await db.Products.AnyAsync(x =>
                    x.TenantId == tenantId && x.Id == request.ProductId && x.IsActive && x.TracksInventory,
                    cancellationToken);
                if (!productExists)
                {
                    await transaction.RollbackAsync(cancellationToken);
                    return Results.NotFound(new { message = "کالای فعالِ انبارگردانی‌شونده در این کسب‌وکار پیدا نشد." });
                }

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
                    request.Quantity, unitCost, normalizedReference, idempotencyKey, requestFingerprint);
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
                if (idempotencyKey is not null)
                {
                    db.ChangeTracker.Clear();
                    var prior = await db.StockMovements.AsNoTracking().SingleOrDefaultAsync(x =>
                        x.TenantId == tenantId && x.BranchId == branchId && x.IdempotencyKey == idempotencyKey,
                        cancellationToken);
                    if (prior is not null)
                    {
                        if (!string.Equals(prior.RequestFingerprint, requestFingerprint, StringComparison.Ordinal))
                            return Results.Conflict(new { message = "این کلید یکتا هم‌زمان با محتوای دیگری ثبت شده است." });
                        return Results.Ok(new
                        {
                            prior.Id, prior.ProductId, prior.BranchId, type = prior.Type.ToString(),
                            prior.Quantity, prior.UnitCost, prior.SignedQuantity, prior.Reference,
                            prior.CreatedAtUtc, replayed = true
                        });
                    }
                }
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

    private static async IAsyncEnumerable<StockMovement> ReadOrderedMovements(
        IQueryable<StockMovement> query,
        bool isSqlite,
        [EnumeratorCancellation] CancellationToken cancellationToken)
    {
        // EF Core SQLite cannot translate DateTimeOffset ordering. Keep the production
        // PostgreSQL path fully streamed; use deterministic client ordering for SQLite.
        if (isSqlite)
        {
            var movements = await query.ToListAsync(cancellationToken);
            foreach (var movement in movements
                .OrderBy(x => x.ProductId).ThenBy(x => x.BranchId)
                .ThenBy(x => x.CreatedAtUtc).ThenBy(x => x.Id))
            {
                cancellationToken.ThrowIfCancellationRequested();
                yield return movement;
            }
            yield break;
        }

        await foreach (var movement in query
            .OrderBy(x => x.ProductId).ThenBy(x => x.BranchId)
            .ThenBy(x => x.CreatedAtUtc).ThenBy(x => x.Id)
            .AsAsyncEnumerable().WithCancellation(cancellationToken))
        {
            yield return movement;
        }
    }

    public sealed record CreateProductRequest(string Sku, string Name, string BaseUnit, bool TracksInventory = true, decimal DefaultSalePrice = 0m);
    public sealed record ProductResponse(Guid Id, string Sku, string Name, string BaseUnit, bool TracksInventory, decimal DefaultSalePrice, bool IsActive);
    public sealed record CreateStockMovementRequest(Guid ProductId, StockMovementType Type, decimal Quantity, decimal? UnitCost, string? Reference);
}

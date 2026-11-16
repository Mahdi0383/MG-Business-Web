using System.Data;
using System.Data.Common;
using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Endpoints;

public static class SalesEndpoints
{
    private static readonly string[] ReadRoles = ["Owner", "Manager", "Accountant", "Cashier", "Viewer"];
    private static readonly string[] WriteRoles = ["Owner", "Manager", "Cashier"];

    public static IEndpointRouteBuilder MapSalesEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/sales").RequireAuthorization("TenantMember");

        group.MapGet("/orders", async (ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var orders = await db.SalesOrders.AsNoTracking()
                .Where(x => x.TenantId == tenant.TenantId)
                .OrderByDescending(x => x.CreatedAtUtc)
                .Take(100)
                .Select(x => new SalesOrderResponse(x.Id, x.Number, x.CustomerId, x.Subtotal,
                    x.LineDiscountTotal, x.Discount, x.Total, x.PaidAmount, x.BalanceDue, x.Status.ToString(), x.CreatedAtUtc))
                .ToListAsync(cancellationToken);
            return Results.Ok(orders);
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ReadRoles)));

        group.MapPost("/orders", async (CreateSaleRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.BranchId is not Guid branchId)
                return Results.BadRequest(new { message = "ابتدا کسب‌وکار و شعبه فعال را انتخاب کنید." });
            if (request.Lines is null || request.Lines.Count == 0)
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["lines"] = ["فاکتور باید حداقل یک قلم داشته باشد."] });
            if (!Enum.IsDefined(request.PaymentMethod))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["paymentMethod"] = ["روش پرداخت معتبر نیست."] });
            if ((request.AmountPaid > 0m && request.PaymentMethod == PaymentMethod.OnAccount)
                || (request.AmountPaid == 0m && request.PaymentMethod != PaymentMethod.OnAccount))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["paymentMethod"] = ["روش پرداخت با مبلغ پرداختی سازگار نیست."] });
            if (request.AmountPaid < 0m || decimal.Round(request.AmountPaid, 2) != request.AmountPaid)
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["amountPaid"] = ["مبلغ پرداخت معتبر نیست."] });

            if (request.CustomerId is Guid customerId &&
                !await db.Customers.AnyAsync(x => x.TenantId == tenantId && x.Id == customerId && x.IsActive, cancellationToken))
                return Results.BadRequest(new { message = "مشتری معتبر نیست یا به این کسب‌وکار تعلق ندارد." });

            var productIds = request.Lines.Select(x => x.ProductId).Distinct().ToArray();
            var products = await db.Products.AsNoTracking()
                .Where(x => x.TenantId == tenantId && x.IsActive && productIds.Contains(x.Id))
                .ToDictionaryAsync(x => x.Id, cancellationToken);
            if (products.Count != productIds.Length)
                return Results.BadRequest(new { message = "یک یا چند کالا نامعتبر یا متعلق به کسب‌وکار دیگری است." });

            var revenueAccount = await FindAccount(db, tenantId, request.RevenueAccountId, AccountType.Revenue, cancellationToken);
            if (revenueAccount is null)
                return Results.BadRequest(new { message = "حساب درآمد معتبر نیست." });

            await using var transaction = await db.Database.BeginTransactionAsync(IsolationLevel.Serializable, cancellationToken);
            try
            {
                var valuations = new Dictionary<Guid, InventoryValuation>();
                foreach (var lineGroup in request.Lines.Where(x => products[x.ProductId].TracksInventory).GroupBy(x => x.ProductId))
                {
                    var history = await db.StockMovements.AsNoTracking()
                        .Where(x => x.TenantId == tenantId && x.BranchId == branchId && x.ProductId == lineGroup.Key)
                        .ToListAsync(cancellationToken);
                    var valuation = InventoryValuation.Calculate(history);
                    var requestedQuantity = lineGroup.Sum(x => x.Quantity);
                    if (requestedQuantity <= 0m || requestedQuantity > valuation.QuantityOnHand)
                    {
                        await transaction.RollbackAsync(cancellationToken);
                        return Results.Conflict(new { message = $"موجودی کالای {products[lineGroup.Key].Name} کافی نیست.", available = valuation.QuantityOnHand });
                    }
                    valuations[lineGroup.Key] = valuation;
                }

                var orderLines = request.Lines.Select(line =>
                {
                    var product = products[line.ProductId];
                    var unitCost = product.TracksInventory ? valuations[line.ProductId].AverageUnitCost : 0m;
                    return new SalesOrderLine(tenantId, product.Id, product.Name, product.Sku,
                        line.Quantity, line.UnitPrice, line.Discount, unitCost);
                }).ToList();

                var orderNumber = $"S-{DateTime.UtcNow:yyyyMMdd}-{Guid.NewGuid():N}"[..19].ToUpperInvariant();
                var order = new SalesOrder(tenantId, branchId, orderNumber, request.CustomerId, orderLines, request.Discount);
                order.SetPaidAmount(request.AmountPaid);
                if (order.BalanceDue > 0m && request.CustomerId is null)
                    return Results.ValidationProblem(new Dictionary<string, string[]> { ["customerId"] = ["برای فروش نسیه یا تسویه ناقص، انتخاب مشتری الزامی است."] });

                var dueAccountId = request.ReceivableAccountId;
                if (order.BalanceDue > 0m &&
                    await FindAccount(db, tenantId, dueAccountId, AccountType.Asset, cancellationToken) is null)
                    return Results.BadRequest(new { message = "حساب دریافتنی معتبر نیست." });

                if (order.PaidAmount > 0m &&
                    await FindAccount(db, tenantId, request.PaymentAccountId, AccountType.Asset, cancellationToken) is null)
                    return Results.BadRequest(new { message = "حساب دریافت وجه معتبر نیست." });

                var totalCost = order.Lines.Sum(x => x.CostTotal);
                if (totalCost > 0m &&
                    (await FindAccount(db, tenantId, request.InventoryAccountId, AccountType.Asset, cancellationToken) is null
                    || await FindAccount(db, tenantId, request.CostOfGoodsSoldAccountId, AccountType.Expense, cancellationToken) is null))
                    return Results.BadRequest(new { message = "حساب موجودی کالا یا بهای تمام‌شده معتبر نیست." });

                db.SalesOrders.Add(order);

                foreach (var lineGroup in request.Lines.Where(x => products[x.ProductId].TracksInventory).GroupBy(x => x.ProductId))
                {
                    var valuation = valuations[lineGroup.Key];
                    var quantity = lineGroup.Sum(x => x.Quantity);
                    db.StockMovements.Add(new StockMovement(tenantId, branchId, lineGroup.Key,
                        StockMovementType.Sale, quantity, valuation.AverageUnitCost, order.Number));
                }

                var revenueLines = new List<JournalEntryLine>();
                if (order.PaidAmount > 0m)
                    revenueLines.Add(new JournalEntryLine(tenantId, request.PaymentAccountId!.Value, order.PaidAmount, 0m, "Sales receipt"));
                if (order.BalanceDue > 0m)
                    revenueLines.Add(new JournalEntryLine(tenantId, dueAccountId!.Value, order.BalanceDue, 0m, "Customer receivable"));
                revenueLines.Add(new JournalEntryLine(tenantId, revenueAccount.Id, 0m, order.Total, "Sales revenue"));

                var revenueEntry = new JournalEntry(tenantId, branchId, DateOnly.FromDateTime(DateTime.UtcNow),
                    $"Sales {order.Number}", revenueLines, order.Number);
                db.JournalEntries.Add(revenueEntry);

                if (totalCost > 0m)
                {
                    var costEntry = new JournalEntry(tenantId, branchId, DateOnly.FromDateTime(DateTime.UtcNow),
                        $"Cost of goods sold {order.Number}",
                        [
                            new JournalEntryLine(tenantId, request.CostOfGoodsSoldAccountId!.Value, totalCost, 0m, "Cost of goods sold"),
                            new JournalEntryLine(tenantId, request.InventoryAccountId!.Value, 0m, totalCost, "Inventory reduction")
                        ], order.Number);
                    db.JournalEntries.Add(costEntry);
                }

                if (order.PaidAmount > 0m)
                    db.SalesPayments.Add(new SalesPayment(tenantId, branchId, order.Id,
                        order.PaidAmount, request.PaymentMethod, request.PaymentReference));

                await db.SaveChangesAsync(cancellationToken);
                await transaction.CommitAsync(cancellationToken);

                return Results.Created($"/api/sales/orders/{order.Id}", new
                {
                    order.Id,
                    order.Number,
                    order.Subtotal,
                    order.LineDiscountTotal,
                    order.Discount,
                    order.Total,
                    order.PaidAmount,
                    order.BalanceDue,
                    CostOfGoodsSold = totalCost,
                    GrossProfit = order.Total - totalCost,
                    status = order.Status.ToString(),
                    order.CreatedAtUtc
                });
            }
            catch (ArgumentException exception)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["sale"] = [exception.Message] });
            }
            catch (DbUpdateException)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = "ثبت فروش به دلیل تعارض داده‌ها انجام نشد؛ موجودی و اطلاعات حساب‌ها را بررسی کنید." });
            }
            catch (DbException)
            {
                await transaction.RollbackAsync(cancellationToken);
                return Results.Conflict(new { message = "تغییر هم‌زمان اطلاعات رخ داد؛ فروش را دوباره بررسی کنید." });
            }
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        return endpoints;
    }

    private static Task<Account?> FindAccount(BusinessDbContext db, Guid tenantId, Guid? accountId,
        AccountType type, CancellationToken cancellationToken)
    {
        if (accountId is not Guid id)
            return Task.FromResult<Account?>(null);

        return db.Accounts.SingleOrDefaultAsync(x =>
            x.TenantId == tenantId && x.Id == id && x.Type == type && x.IsActive, cancellationToken);
    }

    public sealed record CreateSaleRequest(Guid? CustomerId, decimal Discount, decimal AmountPaid,
        PaymentMethod PaymentMethod, Guid RevenueAccountId, Guid? PaymentAccountId, Guid? ReceivableAccountId,
        Guid? InventoryAccountId, Guid? CostOfGoodsSoldAccountId, string? PaymentReference, List<SaleLineRequest> Lines);
    public sealed record SaleLineRequest(Guid ProductId, decimal Quantity, decimal UnitPrice, decimal Discount);
    public sealed record SalesOrderResponse(Guid Id, string Number, Guid? CustomerId, decimal Subtotal,
        decimal LineDiscountTotal, decimal Discount, decimal Total, decimal PaidAmount, decimal BalanceDue,
        string Status, DateTimeOffset CreatedAtUtc);
}

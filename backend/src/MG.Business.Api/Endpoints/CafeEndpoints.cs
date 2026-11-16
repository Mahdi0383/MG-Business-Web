using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Endpoints;

public static class CafeEndpoints
{
    private static readonly string[] ReadRoles = ["Owner", "Manager", "Accountant", "Cashier", "Waiter", "KitchenStaff", "Viewer"];
    private static readonly string[] CreateRoles = ["Owner", "Manager", "Cashier", "Waiter"];
    private static readonly string[] KitchenRoles = ["Owner", "Manager", "Waiter", "KitchenStaff"];

    public static IEndpointRouteBuilder MapCafeEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/cafe/kitchen").RequireAuthorization("TenantMember");

        group.MapGet("/tickets", async (KitchenTicketStatus? status, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var query = db.CafeKitchenTickets.AsNoTracking()
                .Where(x => x.TenantId == tenant.TenantId);
            if (tenant.BranchId is Guid branchId)
                query = query.Where(x => x.BranchId == branchId);
            if (status.HasValue)
                query = query.Where(x => x.Status == status.Value);

            var tickets = await query.Take(100)
                .Select(x => new TicketResponse(x.Id, x.SalesOrderId, x.ServiceType.ToString(),
                    x.TableLabel, x.Notes, x.Status.ToString(), x.CreatedAtUtc, x.PreparingAtUtc, x.ReadyAtUtc))
                .ToListAsync(cancellationToken);
            return Results.Ok(tickets.OrderBy(x => x.CreatedAtUtc));
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ReadRoles)));

        group.MapPost("/tickets", async (CreateTicketRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId || tenant.BranchId is not Guid branchId)
                return Results.BadRequest(new { message = "ابتدا شعبه فعال را انتخاب کنید." });
            if (!Enum.IsDefined(request.ServiceType))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["serviceType"] = ["نوع سرویس معتبر نیست."] });

            var orderExists = await db.SalesOrders.AnyAsync(x =>
                x.TenantId == tenantId && x.Id == request.SalesOrderId && x.BranchId == branchId, cancellationToken);
            if (!orderExists)
                return Results.NotFound(new { message = "فاکتور فروش در این شعبه پیدا نشد." });

            if (await db.CafeKitchenTickets.AnyAsync(x => x.TenantId == tenantId && x.SalesOrderId == request.SalesOrderId, cancellationToken))
                return Results.Conflict(new { message = "برای این فاکتور قبلاً تیکت آشپزخانه ثبت شده است." });

            CafeKitchenTicket ticket;
            try
            {
                ticket = new CafeKitchenTicket(tenantId, branchId, request.SalesOrderId,
                    request.ServiceType, request.TableLabel, request.Notes);
            }
            catch (ArgumentException exception)
            {
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["ticket"] = [exception.Message] });
            }

            db.CafeKitchenTickets.Add(ticket);
            try
            {
                await db.SaveChangesAsync(cancellationToken);
            }
            catch (DbUpdateException)
            {
                return Results.Conflict(new { message = "برای این فاکتور تیکت آشپزخانه ثبت شده یا وضعیت فاکتور تغییر کرده است." });
            }

            return Results.Created($"/api/cafe/kitchen/tickets/{ticket.Id}", ToResponse(ticket));
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(CreateRoles)));

        group.MapPut("/tickets/{ticketId:guid}/status", async (Guid ticketId, UpdateTicketStatusRequest request,
            ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var ticket = await db.CafeKitchenTickets.SingleOrDefaultAsync(x =>
                x.TenantId == tenant.TenantId && x.Id == ticketId
                && (tenant.BranchId == null || x.BranchId == tenant.BranchId), cancellationToken);
            if (ticket is null) return Results.NotFound();

            try
            {
                ticket.TransitionTo(request.Status);
                await db.SaveChangesAsync(cancellationToken);
            }
            catch (InvalidOperationException exception)
            {
                return Results.Conflict(new { message = exception.Message });
            }
            catch (DbUpdateConcurrencyException)
            {
                return Results.Conflict(new { message = "وضعیت تیکت هم‌زمان توسط کاربر دیگری تغییر کرده است؛ تیکت را دوباره بارگذاری کنید." });
            }
            catch (DbUpdateException)
            {
                return Results.Conflict(new { message = "ذخیره وضعیت تیکت با تعارض داده‌ها مواجه شد؛ تیکت را دوباره بررسی کنید." });
            }

            return Results.Ok(ToResponse(ticket));
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(KitchenRoles)));

        return endpoints;
    }

    private static TicketResponse ToResponse(CafeKitchenTicket ticket) =>
        new(ticket.Id, ticket.SalesOrderId, ticket.ServiceType.ToString(), ticket.TableLabel,
            ticket.Notes, ticket.Status.ToString(), ticket.CreatedAtUtc, ticket.PreparingAtUtc, ticket.ReadyAtUtc);

    public sealed record CreateTicketRequest(Guid SalesOrderId, CafeServiceType ServiceType, string? TableLabel, string? Notes);
    public sealed record UpdateTicketStatusRequest(KitchenTicketStatus Status);
    public sealed record TicketResponse(Guid Id, Guid SalesOrderId, string ServiceType, string? TableLabel,
        string? Notes, string Status, DateTimeOffset CreatedAtUtc, DateTimeOffset? PreparingAtUtc, DateTimeOffset? ReadyAtUtc);
}

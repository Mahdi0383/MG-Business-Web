using System.Data.Common;
using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Endpoints;

public static class BranchEndpoints
{
    private static readonly string[] ManageRoles = ["Owner", "Manager"];

    public static IEndpointRouteBuilder MapBranchEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/branches").RequireAuthorization("TenantMember");

        group.MapGet("/", async (ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var branches = await db.Branches.AsNoTracking()
                .Where(x => x.TenantId == tenant.TenantId && x.IsActive
                    && (tenant.BranchId == null || x.Id == tenant.BranchId))
                .OrderBy(x => x.Code)
                .Select(x => new BranchResponse(x.Id, x.Code, x.Name, x.IsActive, x.CreatedAtUtc))
                .ToListAsync(cancellationToken);
            return Results.Ok(branches);
        });

        group.MapPost("/", async (CreateBranchRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId)
                return Results.Forbid();

            Branch branch;
            try { branch = new Branch(tenantId, request.Name, request.Code); }
            catch (ArgumentException exception)
            {
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["branch"] = [exception.Message] });
            }

            db.Branches.Add(branch);
            try { await db.SaveChangesAsync(cancellationToken); }
            catch (DbUpdateException) { return Results.Conflict(new { message = "کد شعبه در این کسب‌وکار تکراری است." }); }
            catch (DbException) { return Results.Conflict(new { message = "پایگاه داده نتوانست شعبه را با اطمینان ثبت کند؛ وضعیت را بررسی و دوباره تلاش کنید." }); }

            return Results.Created($"/api/branches/{branch.Id}", ToResponse(branch));
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ManageRoles)));

        group.MapPut("/{branchId:guid}", async (Guid branchId, UpdateBranchRequest request,
            ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var branch = await db.Branches.SingleOrDefaultAsync(x =>
                x.TenantId == tenant.TenantId && x.Id == branchId, cancellationToken);
            if (branch is null) return Results.NotFound();

            try
            {
                branch.UpdateDetails(request.Name, request.Code);
                await db.SaveChangesAsync(cancellationToken);
            }
            catch (ArgumentException exception)
            {
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["branch"] = [exception.Message] });
            }
            catch (DbUpdateException)
            {
                return Results.Conflict(new { message = "کد شعبه تکراری است." });
            }
            catch (DbException)
            {
                return Results.Conflict(new { message = "پایگاه داده نتوانست تغییرات شعبه را با اطمینان ثبت کند؛ اطلاعات را تازه‌سازی کنید." });
            }

            return Results.Ok(ToResponse(branch));
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ManageRoles)));

        group.MapPatch("/{branchId:guid}/active", async (Guid branchId, SetBranchActiveRequest request,
            ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var branch = await db.Branches.SingleOrDefaultAsync(x =>
                x.TenantId == tenant.TenantId && x.Id == branchId, cancellationToken);
            if (branch is null) return Results.NotFound();

            if (!request.IsActive)
            {
                var activeBranchCount = await db.Branches.CountAsync(x =>
                    x.TenantId == tenant.TenantId && x.IsActive, cancellationToken);
                if (activeBranchCount <= 1)
                    return Results.Conflict(new { message = "هر کسب‌وکار باید حداقل یک شعبه فعال داشته باشد." });

                var hasActiveGameSessions = await db.GameSessions.AnyAsync(x =>
                    x.TenantId == tenant.TenantId && x.BranchId == branchId && x.Status == GameSessionStatus.Active,
                    cancellationToken);
                var hasOpenKitchenTickets = await db.CafeKitchenTickets.AnyAsync(x =>
                    x.TenantId == tenant.TenantId && x.BranchId == branchId
                    && x.Status != KitchenTicketStatus.Completed && x.Status != KitchenTicketStatus.Cancelled,
                    cancellationToken);

                if (hasActiveGameSessions || hasOpenKitchenTickets)
                    return Results.Conflict(new { message = "تا زمان بستن سانس‌های فعال و تکمیل یا لغو سفارش‌های آشپزخانه، غیرفعال‌سازی شعبه مجاز نیست." });
            }

            branch.SetActive(request.IsActive);
            try
            {
                await db.SaveChangesAsync(cancellationToken);
            }
            catch (DbUpdateException)
            {
                return Results.Conflict(new { message = "وضعیت شعبه به دلیل تعارض داده‌ها تغییر نکرد؛ اطلاعات را تازه‌سازی کنید." });
            }
            catch (DbException)
            {
                return Results.Conflict(new { message = "پایگاه داده نتوانست وضعیت شعبه را با اطمینان تغییر دهد؛ وضعیت را دوباره بررسی کنید." });
            }
            return Results.Ok(ToResponse(branch));
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ManageRoles)));

        return endpoints;
    }

    private static BranchResponse ToResponse(Branch branch) =>
        new(branch.Id, branch.Code, branch.Name, branch.IsActive, branch.CreatedAtUtc);

    public sealed record CreateBranchRequest(string Name, string Code);
    public sealed record UpdateBranchRequest(string Name, string Code);
    public sealed record SetBranchActiveRequest(bool IsActive);
    public sealed record BranchResponse(Guid Id, string Code, string Name, bool IsActive, DateTimeOffset CreatedAtUtc);
}

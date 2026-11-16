using System.Security.Claims;
using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Endpoints;

public static class TenantEndpoints
{
    public static IEndpointRouteBuilder MapTenantEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/tenants").RequireAuthorization();

        group.MapPost("/", async (CreateTenantRequest request, ITenantContext context, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (context.UserId is not Guid userId)
                return Results.Unauthorized();
            if (string.IsNullOrWhiteSpace(request.Name) || string.IsNullOrWhiteSpace(request.Slug))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["name"] = ["نام و شناسه کسب‌وکار الزامی است."] });

            var slug = request.Slug.Trim().ToLowerInvariant();
            if (await db.Tenants.AnyAsync(x => x.Slug == slug, cancellationToken))
                return Results.Conflict(new { message = "این شناسه کسب‌وکار قبلاً ثبت شده است." });

            var tenant = new Tenant(request.Name, slug);
            var branch = new Branch(tenant.Id, string.IsNullOrWhiteSpace(request.FirstBranchName) ? "شعبه اصلی" : request.FirstBranchName, "MAIN");
            var membership = new TenantMembership(tenant.Id, userId, TenantRole.Owner);
            db.AddRange(tenant, branch, membership);
            await db.SaveChangesAsync(cancellationToken);

            return Results.Created($"/api/tenants/{tenant.Id}", new
            {
                tenantId = tenant.Id,
                tenant.Name,
                tenant.Slug,
                branchId = branch.Id,
                role = membership.Role.ToString()
            });
        });

        group.MapGet("/mine", async (ITenantContext context, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (context.UserId is not Guid userId)
                return Results.Unauthorized();

            var memberships = await db.TenantMemberships.AsNoTracking()
                .Where(x => x.UserId == userId && x.IsActive)
                .Join(db.Tenants.AsNoTracking(), membership => membership.TenantId, tenant => tenant.Id,
                    (membership, tenant) => new
                    {
                        tenant.Id,
                        tenant.Name,
                        tenant.Slug,
                        Role = membership.Role.ToString(),
                        membership.BranchId
                    })
                .ToListAsync(cancellationToken);

            return Results.Ok(memberships);
        });

        return endpoints;
    }

    public sealed record CreateTenantRequest(string Name, string Slug, string? FirstBranchName);
}

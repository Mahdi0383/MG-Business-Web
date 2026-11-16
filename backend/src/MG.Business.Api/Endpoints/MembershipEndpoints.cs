using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Endpoints;

public static class MembershipEndpoints
{
    private static readonly string[] OwnerOnly = ["Owner"];

    public static IEndpointRouteBuilder MapMembershipEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/memberships").RequireAuthorization("TenantMember");

        group.MapGet("/", async (ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var memberships = await db.TenantMemberships.AsNoTracking()
                .Where(x => x.TenantId == tenant.TenantId && x.IsActive)
                .Join(db.Users.AsNoTracking(), membership => membership.UserId, user => user.Id,
                    (membership, user) => new
                    {
                        membership.Id,
                        membership.UserId,
                        user.Email,
                        user.DisplayName,
                        Role = membership.Role.ToString(),
                        membership.BranchId
                    })
                .OrderBy(x => x.DisplayName)
                .ToListAsync(cancellationToken);
            return Results.Ok(memberships);
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(OwnerOnly)));

        group.MapPost("/", async (CreateMembershipRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId)
                return Results.Forbid();
            if (!Enum.IsDefined(request.Role))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["role"] = ["نقش کاربری معتبر نیست."] });

            var email = request.Email.Trim().ToUpperInvariant();
            var user = await db.Users.SingleOrDefaultAsync(x => x.NormalizedEmail == email && x.IsActive, cancellationToken);
            if (user is null)
                return Results.NotFound(new { message = "کاربر فعالی با این ایمیل پیدا نشد؛ ابتدا باید حساب کاربری بسازد." });

            if (request.BranchId is Guid branchId &&
                !await db.Branches.AnyAsync(x => x.TenantId == tenantId && x.Id == branchId && x.IsActive, cancellationToken))
                return Results.BadRequest(new { message = "شعبه انتخاب‌شده متعلق به این کسب‌وکار نیست." });

            var membership = new TenantMembership(tenantId, user.Id, request.Role, request.BranchId);
            db.TenantMemberships.Add(membership);
            try { await db.SaveChangesAsync(cancellationToken); }
            catch (DbUpdateException) { return Results.Conflict(new { message = "این کاربر قبلاً عضو این کسب‌وکار شده است." }); }

            return Results.Created($"/api/memberships/{membership.Id}", new
            {
                membership.Id,
                membership.UserId,
                user.Email,
                user.DisplayName,
                Role = membership.Role.ToString(),
                membership.BranchId
            });
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(OwnerOnly)));

        group.MapPut("/{membershipId:guid}", async (Guid membershipId, UpdateMembershipRequest request,
            ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (!Enum.IsDefined(request.Role))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["role"] = ["نقش کاربری معتبر نیست."] });
            if (request.BranchId is Guid branchId &&
                !await db.Branches.AnyAsync(x => x.TenantId == tenant.TenantId && x.Id == branchId && x.IsActive, cancellationToken))
                return Results.BadRequest(new { message = "شعبه انتخاب‌شده متعلق به این کسب‌وکار نیست." });

            var membership = await db.TenantMemberships.SingleOrDefaultAsync(x =>
                x.TenantId == tenant.TenantId && x.Id == membershipId && x.IsActive, cancellationToken);
            if (membership is null) return Results.NotFound();

            if (membership.Role == TenantRole.Owner && request.Role != TenantRole.Owner
                && !await HasAnotherActiveOwnerAsync(db, tenant.TenantId!.Value, membership.Id, cancellationToken))
                return Results.Conflict(new { message = "هر کسب‌وکار باید حداقل یک مالک فعال داشته باشد." });

            membership.ChangeRole(request.Role);
            membership.AssignBranch(request.BranchId);
            await db.SaveChangesAsync(cancellationToken);
            return Results.Ok(new { membership.Id, Role = membership.Role.ToString(), membership.BranchId });
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(OwnerOnly)));

        group.MapDelete("/{membershipId:guid}", async (Guid membershipId, ITenantContext tenant,
            BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var membership = await db.TenantMemberships.SingleOrDefaultAsync(x =>
                x.TenantId == tenant.TenantId && x.Id == membershipId && x.IsActive, cancellationToken);
            if (membership is null) return Results.NotFound();
            if (membership.UserId == tenant.UserId)
                return Results.BadRequest(new { message = "مالک نمی‌تواند عضویت خودش را از این مسیر غیرفعال کند." });
            if (membership.Role == TenantRole.Owner
                && !await HasAnotherActiveOwnerAsync(db, tenant.TenantId!.Value, membership.Id, cancellationToken))
                return Results.Conflict(new { message = "هر کسب‌وکار باید حداقل یک مالک فعال داشته باشد." });

            membership.Deactivate();
            await db.SaveChangesAsync(cancellationToken);
            return Results.NoContent();
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(OwnerOnly)));

        return endpoints;
    }

    private static Task<bool> HasAnotherActiveOwnerAsync(BusinessDbContext db, Guid tenantId,
        Guid excludedMembershipId, CancellationToken cancellationToken) =>
        db.TenantMemberships.AnyAsync(x => x.TenantId == tenantId && x.Id != excludedMembershipId
            && x.IsActive && x.Role == TenantRole.Owner, cancellationToken);

    public sealed record CreateMembershipRequest(string Email, TenantRole Role, Guid? BranchId);
    public sealed record UpdateMembershipRequest(TenantRole Role, Guid? BranchId);
}

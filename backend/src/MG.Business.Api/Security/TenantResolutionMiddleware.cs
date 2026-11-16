using System.Security.Claims;
using MG.Business.Infrastructure.Identity;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Security;

public sealed class TenantResolutionMiddleware(RequestDelegate next)
{
    public async Task InvokeAsync(HttpContext httpContext, BusinessDbContext db, TenantContext tenantContext)
    {
        if (httpContext.User.Identity?.IsAuthenticated == true)
        {
            var userIdValue = httpContext.User.FindFirstValue(ClaimTypes.NameIdentifier)
                ?? httpContext.User.FindFirstValue("sub");

            if (!Guid.TryParse(userIdValue, out var userId))
            {
                httpContext.Response.StatusCode = StatusCodes.Status401Unauthorized;
                return;
            }

            var userIsActive = await db.Users.AsNoTracking()
                .AnyAsync(user => user.Id == userId && user.IsActive, httpContext.RequestAborted);

            if (!userIsActive)
            {
                httpContext.Response.StatusCode = StatusCodes.Status401Unauthorized;
                return;
            }

            tenantContext.UserId = userId;
            var tenantHeader = httpContext.Request.Headers["X-MG-Tenant"].FirstOrDefault();
            var branchHeader = httpContext.Request.Headers["X-MG-Branch"].FirstOrDefault();

            if (!string.IsNullOrWhiteSpace(tenantHeader))
            {
                if (!Guid.TryParse(tenantHeader, out var tenantId))
                {
                    httpContext.Response.StatusCode = StatusCodes.Status400BadRequest;
                    return;
                }

                var tenantIsActive = await db.Tenants.AsNoTracking()
                    .AnyAsync(item => item.Id == tenantId && item.IsActive, httpContext.RequestAborted);
                if (!tenantIsActive)
                {
                    httpContext.Response.StatusCode = StatusCodes.Status403Forbidden;
                    return;
                }

                var membership = await db.TenantMemberships.AsNoTracking()
                    .SingleOrDefaultAsync(item => item.TenantId == tenantId
                        && item.UserId == userId && item.IsActive, httpContext.RequestAborted);

                if (membership is null)
                {
                    httpContext.Response.StatusCode = StatusCodes.Status403Forbidden;
                    return;
                }

                Guid? selectedBranchId = membership.BranchId;
                if (!string.IsNullOrWhiteSpace(branchHeader))
                {
                    if (!Guid.TryParse(branchHeader, out var requestedBranch)
                        || (selectedBranchId.HasValue && requestedBranch != selectedBranchId.Value))
                    {
                        httpContext.Response.StatusCode = StatusCodes.Status403Forbidden;
                        return;
                    }

                    selectedBranchId = requestedBranch;
                }

                if (selectedBranchId is Guid branchId)
                {
                    var branchIsActive = await db.Branches.AsNoTracking().AnyAsync(branch =>
                        branch.TenantId == tenantId && branch.Id == branchId && branch.IsActive,
                        httpContext.RequestAborted);
                    if (!branchIsActive)
                    {
                        httpContext.Response.StatusCode = StatusCodes.Status403Forbidden;
                        return;
                    }
                }

                tenantContext.TenantId = tenantId;
                tenantContext.BranchId = selectedBranchId;
                tenantContext.Role = membership.Role.ToString();
            }
            else if (!string.IsNullOrWhiteSpace(branchHeader))
            {
                httpContext.Response.StatusCode = StatusCodes.Status400BadRequest;
                return;
            }
        }

        await next(httpContext);
    }
}

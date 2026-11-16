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

                var membership = await db.TenantMemberships.AsNoTracking()
                    .SingleOrDefaultAsync(item => item.TenantId == tenantId
                        && item.UserId == userId && item.IsActive, httpContext.RequestAborted);

                if (membership is null)
                {
                    httpContext.Response.StatusCode = StatusCodes.Status403Forbidden;
                    return;
                }

                tenantContext.TenantId = tenantId;
                tenantContext.Role = membership.Role.ToString();

                if (membership.BranchId.HasValue)
                {
                    if (!string.IsNullOrWhiteSpace(branchHeader)
                        && (!Guid.TryParse(branchHeader, out var requestedBranch)
                            || requestedBranch != membership.BranchId.Value))
                    {
                        httpContext.Response.StatusCode = StatusCodes.Status403Forbidden;
                        return;
                    }

                    tenantContext.BranchId = membership.BranchId;
                }
                else if (!string.IsNullOrWhiteSpace(branchHeader))
                {
                    if (!Guid.TryParse(branchHeader, out var requestedBranch)
                        || !await db.Branches.AsNoTracking().AnyAsync(branch =>
                            branch.TenantId == tenantId && branch.Id == requestedBranch,
                            httpContext.RequestAborted))
                    {
                        httpContext.Response.StatusCode = StatusCodes.Status403Forbidden;
                        return;
                    }

                    tenantContext.BranchId = requestedBranch;
                }
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

using Microsoft.AspNetCore.Authorization;

namespace MG.Business.Api.Security;

public sealed class TenantResolvedRequirement : IAuthorizationRequirement;

public sealed class TenantResolvedHandler(ITenantContext tenantContext) : AuthorizationHandler<TenantResolvedRequirement>
{
    protected override Task HandleRequirementAsync(
        AuthorizationHandlerContext context,
        TenantResolvedRequirement requirement)
    {
        if (tenantContext.IsResolved)
            context.Succeed(requirement);

        return Task.CompletedTask;
    }
}

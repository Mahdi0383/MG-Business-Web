using Microsoft.AspNetCore.Authorization;

namespace MG.Business.Api.Security;

public sealed class TenantRoleRequirement(params string[] allowedRoles) : IAuthorizationRequirement
{
    public IReadOnlySet<string> AllowedRoles { get; } = new HashSet<string>(allowedRoles, StringComparer.OrdinalIgnoreCase);
}

public sealed class TenantRoleHandler(ITenantContext tenantContext) : AuthorizationHandler<TenantRoleRequirement>
{
    protected override Task HandleRequirementAsync(AuthorizationHandlerContext context, TenantRoleRequirement requirement)
    {
        if (tenantContext.Role is not null && requirement.AllowedRoles.Contains(tenantContext.Role))
            context.Succeed(requirement);

        return Task.CompletedTask;
    }
}

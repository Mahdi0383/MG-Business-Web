using MG.Business.Api.Security;

namespace MG.Business.Api.Endpoints;

public static class TenantContextEndpoints
{
    public static IEndpointRouteBuilder MapTenantContextEndpoints(this IEndpointRouteBuilder endpoints)
    {
        endpoints.MapGet("/api/context", (ITenantContext tenant) => Results.Ok(new
        {
            tenantId = tenant.TenantId,
            branchId = tenant.BranchId,
            userId = tenant.UserId,
            role = tenant.Role
        }))
        .RequireAuthorization("TenantMember");

        return endpoints;
    }
}

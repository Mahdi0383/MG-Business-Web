using MG.Business.Api.Security;
using MG.Business.Domain.Entities;
using MG.Business.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Api.Endpoints;

public static class CustomerEndpoints
{
    private static readonly string[] ReadRoles = ["Owner", "Manager", "Accountant", "Cashier", "Waiter", "GameOperator", "Viewer"];
    private static readonly string[] WriteRoles = ["Owner", "Manager", "Cashier"];

    public static IEndpointRouteBuilder MapCustomerEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/customers").RequireAuthorization("TenantMember");

        group.MapGet("/", async (string? search, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var query = db.Customers.AsNoTracking().Where(x => x.TenantId == tenant.TenantId && x.IsActive);
            if (!string.IsNullOrWhiteSpace(search))
            {
                var term = search.Trim();
                query = query.Where(x => x.Name.Contains(term) || x.Code.Contains(term)
                    || (x.Phone != null && x.Phone.Contains(term)) || (x.Email != null && x.Email.Contains(term)));
            }

            var customers = await query.OrderBy(x => x.Name).Take(100)
                .Select(x => new CustomerResponse(x.Id, x.Code, x.Name, x.Phone, x.Email, x.Address, x.Notes, x.IsActive))
                .ToListAsync(cancellationToken);
            return Results.Ok(customers);
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ReadRoles)));

        group.MapPost("/", async (CreateCustomerRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            if (tenant.TenantId is not Guid tenantId)
                return Results.Forbid();

            Customer customer;
            try
            {
                customer = new Customer(tenantId, request.Code, request.Name, request.Phone, request.Email, request.Address, request.Notes);
            }
            catch (ArgumentException exception)
            {
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["customer"] = [exception.Message] });
            }

            db.Customers.Add(customer);
            try
            {
                await db.SaveChangesAsync(cancellationToken);
            }
            catch (DbUpdateException)
            {
                return Results.Conflict(new { message = "کد مشتری در این کسب‌وکار قبلاً استفاده شده است." });
            }

            return Results.Created($"/api/customers/{customer.Id}", ToResponse(customer));
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        group.MapGet("/{customerId:guid}", async (Guid customerId, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var customer = await db.Customers.AsNoTracking().SingleOrDefaultAsync(x =>
                x.TenantId == tenant.TenantId && x.Id == customerId, cancellationToken);
            return customer is null ? Results.NotFound() : Results.Ok(ToResponse(customer));
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(ReadRoles)));

        group.MapPut("/{customerId:guid}", async (Guid customerId, UpdateCustomerRequest request, ITenantContext tenant, BusinessDbContext db, CancellationToken cancellationToken) =>
        {
            var customer = await db.Customers.SingleOrDefaultAsync(x =>
                x.TenantId == tenant.TenantId && x.Id == customerId && x.IsActive, cancellationToken);
            if (customer is null)
                return Results.NotFound();

            try
            {
                customer.UpdateContact(request.Name, request.Phone, request.Email, request.Address, request.Notes);
                await db.SaveChangesAsync(cancellationToken);
            }
            catch (ArgumentException exception)
            {
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["customer"] = [exception.Message] });
            }

            return Results.Ok(ToResponse(customer));
        }).RequireAuthorization(policy => policy.AddRequirements(new TenantRoleRequirement(WriteRoles)));

        return endpoints;
    }

    private static CustomerResponse ToResponse(Customer customer) =>
        new(customer.Id, customer.Code, customer.Name, customer.Phone, customer.Email, customer.Address, customer.Notes, customer.IsActive);

    public sealed record CreateCustomerRequest(string Code, string Name, string? Phone, string? Email, string? Address, string? Notes);
    public sealed record UpdateCustomerRequest(string Name, string? Phone, string? Email, string? Address, string? Notes);
    public sealed record CustomerResponse(Guid Id, string Code, string Name, string? Phone, string? Email, string? Address, string? Notes, bool IsActive);
}

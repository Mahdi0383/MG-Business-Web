using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Design;

namespace MG.Business.Infrastructure.Persistence;

public sealed class BusinessDbContextFactory : IDesignTimeDbContextFactory<BusinessDbContext>
{
    public BusinessDbContext CreateDbContext(string[] args)
    {
        var connectionString = Environment.GetEnvironmentVariable("MG_DESIGN_CONNECTION")
            ?? "Host=localhost;Database=mg_business_design;Username=mg_design;Password=design-only";

        var options = new DbContextOptionsBuilder<BusinessDbContext>()
            .UseNpgsql(connectionString)
            .Options;

        return new BusinessDbContext(options);
    }
}

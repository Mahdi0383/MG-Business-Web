using MG.Business.Domain.Entities;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Infrastructure.Persistence;

public sealed class BusinessDbContext(DbContextOptions<BusinessDbContext> options) : DbContext(options)
{
    public DbSet<Tenant> Tenants => Set<Tenant>();
    public DbSet<Branch> Branches => Set<Branch>();
    public DbSet<Account> Accounts => Set<Account>();
    public DbSet<JournalEntry> JournalEntries => Set<JournalEntry>();
    public DbSet<JournalEntryLine> JournalEntryLines => Set<JournalEntryLine>();
    public DbSet<Product> Products => Set<Product>();
    public DbSet<StockMovement> StockMovements => Set<StockMovement>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        base.OnModelCreating(modelBuilder);

        modelBuilder.Entity<Tenant>(entity =>
        {
            entity.ToTable("tenants");
            entity.HasKey(x => x.Id);
            entity.Property(x => x.Name).HasMaxLength(160).IsRequired();
            entity.Property(x => x.Slug).HasMaxLength(80).IsRequired();
            entity.HasIndex(x => x.Slug).IsUnique();
        });

        modelBuilder.Entity<Branch>(entity =>
        {
            entity.ToTable("branches");
            entity.HasKey(x => x.Id);
            entity.Property(x => x.Name).HasMaxLength(160).IsRequired();
            entity.Property(x => x.Code).HasMaxLength(32).IsRequired();
            entity.HasIndex(x => new { x.TenantId, x.Code }).IsUnique();
            entity.HasAlternateKey(x => new { x.TenantId, x.Id });
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
        });

        modelBuilder.Entity<Account>(entity =>
        {
            entity.ToTable("accounts");
            entity.HasKey(x => x.Id);
            entity.Property(x => x.Code).HasMaxLength(40).IsRequired();
            entity.Property(x => x.Name).HasMaxLength(160).IsRequired();
            entity.Property(x => x.Type).HasConversion<string>().HasMaxLength(20).IsRequired();
            entity.HasIndex(x => new { x.TenantId, x.Code }).IsUnique();
            entity.HasAlternateKey(x => new { x.TenantId, x.Id });
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
        });

        modelBuilder.Entity<JournalEntry>(entity =>
        {
            entity.ToTable("journal_entries");
            entity.HasKey(x => x.Id);
            entity.Property(x => x.Description).HasMaxLength(500).IsRequired();
            entity.Property(x => x.Reference).HasMaxLength(100);
            entity.Property(x => x.EntryDate).HasConversion(
                value => value.ToDateTime(TimeOnly.MinValue),
                value => DateOnly.FromDateTime(value));
            entity.HasAlternateKey(x => new { x.TenantId, x.Id });
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Branch>().WithMany().HasForeignKey(x => new { x.TenantId, x.BranchId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
            entity.HasMany(x => x.Lines).WithOne().HasForeignKey(x => new { x.TenantId, x.JournalEntryId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Cascade);
            entity.Navigation(x => x.Lines).UsePropertyAccessMode(PropertyAccessMode.Field);
        });

        modelBuilder.Entity<Product>(entity =>
        {
            entity.ToTable("products");
            entity.HasKey(x => x.Id);
            entity.Property(x => x.Sku).HasMaxLength(64).IsRequired();
            entity.Property(x => x.Name).HasMaxLength(200).IsRequired();
            entity.Property(x => x.BaseUnit).HasMaxLength(24).IsRequired();
            entity.HasIndex(x => new { x.TenantId, x.Sku }).IsUnique();
            entity.HasAlternateKey(x => new { x.TenantId, x.Id });
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
        });

        modelBuilder.Entity<StockMovement>(entity =>
        {
            entity.ToTable("stock_movements", table =>
            {
                table.HasCheckConstraint("ck_stock_movement_quantity_positive", "CAST(quantity AS NUMERIC) > 0");
                table.HasCheckConstraint("ck_stock_movement_unit_cost_non_negative", "CAST(unit_cost AS NUMERIC) >= 0");
            });
            entity.HasKey(x => x.Id);
            entity.Property(x => x.Type).HasConversion<string>().HasMaxLength(32).IsRequired();
            entity.Property(x => x.Quantity).HasColumnName("quantity").HasPrecision(18, 3);
            entity.Property(x => x.UnitCost).HasColumnName("unit_cost").HasPrecision(18, 2);
            entity.Property(x => x.Reference).HasMaxLength(100);
            entity.HasIndex(x => new { x.TenantId, x.BranchId, x.CreatedAtUtc });
            entity.HasIndex(x => new { x.TenantId, x.ProductId, x.CreatedAtUtc });
            entity.HasOne<Product>().WithMany().HasForeignKey(x => new { x.TenantId, x.ProductId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Branch>().WithMany().HasForeignKey(x => new { x.TenantId, x.BranchId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
        });

        modelBuilder.Entity<JournalEntryLine>(entity =>
        {
            entity.ToTable("journal_entry_lines", table =>
            {
                table.HasCheckConstraint("ck_journal_line_one_side", "(CAST(debit AS NUMERIC) > 0 AND CAST(credit AS NUMERIC) = 0) OR (CAST(credit AS NUMERIC) > 0 AND CAST(debit AS NUMERIC) = 0)");
                table.HasCheckConstraint("ck_journal_line_non_negative", "CAST(debit AS NUMERIC) >= 0 AND CAST(credit AS NUMERIC) >= 0");
            });
            entity.HasKey(x => x.Id);
            entity.Property(x => x.Debit).HasColumnName("debit").HasPrecision(18, 2);
            entity.Property(x => x.Credit).HasColumnName("credit").HasPrecision(18, 2);
            entity.Property(x => x.Description).HasMaxLength(300);
            entity.HasOne<Account>().WithMany().HasForeignKey(x => new { x.TenantId, x.AccountId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
        });
    }
}

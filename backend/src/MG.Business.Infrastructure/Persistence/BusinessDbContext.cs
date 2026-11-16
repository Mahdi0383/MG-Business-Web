using MG.Business.Domain.Entities;
using MG.Business.Infrastructure.Identity;
using Microsoft.AspNetCore.Identity;
using Microsoft.AspNetCore.Identity.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore;

namespace MG.Business.Infrastructure.Persistence;

public sealed class BusinessDbContext(DbContextOptions<BusinessDbContext> options) : IdentityDbContext<ApplicationUser, IdentityRole<Guid>, Guid>(options)
{
    public DbSet<Tenant> Tenants => Set<Tenant>();
    public DbSet<TenantMembership> TenantMemberships => Set<TenantMembership>();
    public DbSet<Branch> Branches => Set<Branch>();
    public DbSet<Customer> Customers => Set<Customer>();
    public DbSet<SalesOrder> SalesOrders => Set<SalesOrder>();
    public DbSet<SalesOrderLine> SalesOrderLines => Set<SalesOrderLine>();
    public DbSet<SalesPayment> SalesPayments => Set<SalesPayment>();
    public DbSet<CafeKitchenTicket> CafeKitchenTickets => Set<CafeKitchenTicket>();
    public DbSet<GameDevice> GameDevices => Set<GameDevice>();
    public DbSet<GameSession> GameSessions => Set<GameSession>();
    public DbSet<Account> Accounts => Set<Account>();
    public DbSet<JournalEntry> JournalEntries => Set<JournalEntry>();
    public DbSet<JournalEntryLine> JournalEntryLines => Set<JournalEntryLine>();
    public DbSet<Product> Products => Set<Product>();
    public DbSet<StockMovement> StockMovements => Set<StockMovement>();
    public DbSet<SyncOperation> SyncOperations => Set<SyncOperation>();
    public DbSet<DevicePresence> DevicePresences => Set<DevicePresence>();
    public DbSet<SyncDeviceCursor> SyncDeviceCursors => Set<SyncDeviceCursor>();
    public DbSet<SyncDeviceBranchCursor> SyncDeviceBranchCursors => Set<SyncDeviceBranchCursor>();

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

        modelBuilder.Entity<ApplicationUser>(entity =>
        {
            entity.Property(x => x.DisplayName).HasMaxLength(160);
            entity.HasIndex(x => x.NormalizedEmail).IsUnique();
        });

        modelBuilder.Entity<TenantMembership>(entity =>
        {
            entity.ToTable("tenant_memberships");
            entity.HasKey(x => x.Id);
            entity.Property(x => x.Role).HasConversion<string>().HasMaxLength(32).IsRequired();
            entity.HasIndex(x => new { x.TenantId, x.UserId }).IsUnique();
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<ApplicationUser>().WithMany().HasForeignKey(x => x.UserId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Branch>().WithMany().HasForeignKey(x => new { x.TenantId, x.BranchId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict).IsRequired(false);
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

        modelBuilder.Entity<Customer>(entity =>
        {
            entity.ToTable("customers");
            entity.HasKey(x => x.Id);
            entity.Property(x => x.Code).HasMaxLength(40).IsRequired();
            entity.Property(x => x.Name).HasMaxLength(160).IsRequired();
            entity.Property(x => x.Phone).HasMaxLength(40);
            entity.Property(x => x.Email).HasMaxLength(254);
            entity.Property(x => x.Address).HasMaxLength(500);
            entity.Property(x => x.Notes).HasMaxLength(1000);
            entity.HasIndex(x => new { x.TenantId, x.Code }).IsUnique();
            entity.HasAlternateKey(x => new { x.TenantId, x.Id });
            entity.HasIndex(x => new { x.TenantId, x.Phone });
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
            entity.Property(x => x.DefaultSalePrice).HasPrecision(18, 2);
            entity.HasIndex(x => new { x.TenantId, x.Sku }).IsUnique();
            entity.HasAlternateKey(x => new { x.TenantId, x.Id });
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
        });

        modelBuilder.Entity<GameDevice>(entity =>
        {
            entity.ToTable("game_devices");
            entity.HasKey(x => x.Id);
            entity.Property(x => x.Code).HasMaxLength(40).IsRequired();
            entity.Property(x => x.Name).HasMaxLength(160).IsRequired();
            entity.Property(x => x.HourlyRate).HasPrecision(18, 2);
            entity.HasIndex(x => new { x.TenantId, x.BranchId, x.Code }).IsUnique();
            entity.HasAlternateKey(x => new { x.TenantId, x.Id });
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Branch>().WithMany().HasForeignKey(x => new { x.TenantId, x.BranchId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Product>().WithMany().HasForeignKey(x => new { x.TenantId, x.ServiceProductId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
        });

        modelBuilder.Entity<GameSession>(entity =>
        {
            entity.ToTable("game_sessions");
            entity.HasKey(x => x.Id);
            entity.Property(x => x.Status).HasConversion<string>().HasMaxLength(24).IsRequired();
            entity.Property(x => x.HourlyRate).HasPrecision(18, 2);
            entity.Property(x => x.TotalAmount).HasPrecision(18, 2);
            entity.HasIndex(x => new { x.TenantId, x.DeviceId }).IsUnique().HasFilter("\"Status\" = 'Active'");
            entity.HasIndex(x => new { x.TenantId, x.BranchId, x.StartedAtUtc });
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Branch>().WithMany().HasForeignKey(x => new { x.TenantId, x.BranchId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<GameDevice>().WithMany().HasForeignKey(x => new { x.TenantId, x.DeviceId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Customer>().WithMany().HasForeignKey(x => new { x.TenantId, x.CustomerId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict).IsRequired(false);
            entity.HasOne<SalesOrder>().WithMany().HasForeignKey(x => new { x.TenantId, x.SalesOrderId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict).IsRequired(false);
        });

        modelBuilder.Entity<CafeKitchenTicket>(entity =>
        {
            entity.ToTable("cafe_kitchen_tickets");
            entity.HasKey(x => x.Id);
            entity.Property(x => x.ServiceType).HasConversion<string>().HasMaxLength(24).IsRequired();
            entity.Property(x => x.Status).HasConversion<string>().HasMaxLength(24).IsRequired();
            entity.Property(x => x.TableLabel).HasMaxLength(40);
            entity.Property(x => x.Notes).HasMaxLength(500);
            entity.HasIndex(x => new { x.TenantId, x.SalesOrderId }).IsUnique();
            entity.HasIndex(x => new { x.TenantId, x.BranchId, x.Status, x.CreatedAtUtc });
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Branch>().WithMany().HasForeignKey(x => new { x.TenantId, x.BranchId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<SalesOrder>().WithMany().HasForeignKey(x => new { x.TenantId, x.SalesOrderId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
        });

        modelBuilder.Entity<SalesOrder>(entity =>
        {
            entity.ToTable("sales_orders");
            entity.HasKey(x => x.Id);
            entity.Property(x => x.Number).HasMaxLength(40).IsRequired();
            entity.Property(x => x.Status).HasConversion<string>().HasMaxLength(24).IsRequired();
            entity.Property(x => x.Subtotal).HasPrecision(18, 2);
            entity.Property(x => x.LineDiscountTotal).HasPrecision(18, 2);
            entity.Property(x => x.Discount).HasPrecision(18, 2);
            entity.Property(x => x.Total).HasPrecision(18, 2);
            entity.Property(x => x.PaidAmount).HasPrecision(18, 2);
            entity.Ignore(x => x.BalanceDue);
            entity.HasIndex(x => new { x.TenantId, x.Number }).IsUnique();
            entity.HasIndex(x => new { x.TenantId, x.CreatedAtUtc });
            entity.HasAlternateKey(x => new { x.TenantId, x.Id });
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Branch>().WithMany().HasForeignKey(x => new { x.TenantId, x.BranchId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Customer>().WithMany().HasForeignKey(x => new { x.TenantId, x.CustomerId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict).IsRequired(false);
            entity.HasMany(x => x.Lines).WithOne().HasForeignKey(x => new { x.TenantId, x.SalesOrderId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Cascade);
            entity.Navigation(x => x.Lines).UsePropertyAccessMode(PropertyAccessMode.Field);
        });

        modelBuilder.Entity<SalesOrderLine>(entity =>
        {
            entity.ToTable("sales_order_lines", table =>
            {
                table.HasCheckConstraint("ck_sales_line_quantity_positive", @"CAST(""Quantity"" AS NUMERIC) > 0");
                table.HasCheckConstraint("ck_sales_line_price_non_negative", @"CAST(""UnitPrice"" AS NUMERIC) >= 0");
                table.HasCheckConstraint("ck_sales_line_discount_non_negative", @"CAST(""Discount"" AS NUMERIC) >= 0");
            });
            entity.HasKey(x => x.Id);
            entity.Property(x => x.ProductName).HasMaxLength(200).IsRequired();
            entity.Property(x => x.Sku).HasMaxLength(64).IsRequired();
            entity.Property(x => x.Quantity).HasPrecision(18, 3);
            entity.Property(x => x.UnitPrice).HasPrecision(18, 2);
            entity.Property(x => x.Discount).HasPrecision(18, 2);
            entity.Property(x => x.UnitCost).HasPrecision(18, 2);
            entity.Ignore(x => x.LineTotal);
            entity.Ignore(x => x.CostTotal);
            entity.HasOne<Product>().WithMany().HasForeignKey(x => new { x.TenantId, x.ProductId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
        });

        modelBuilder.Entity<SalesPayment>(entity =>
        {
            entity.ToTable("sales_payments");
            entity.HasKey(x => x.Id);
            entity.Property(x => x.Amount).HasPrecision(18, 2);
            entity.Property(x => x.Method).HasConversion<string>().HasMaxLength(24).IsRequired();
            entity.Property(x => x.Reference).HasMaxLength(100);
            entity.HasIndex(x => new { x.TenantId, x.SalesOrderId });
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Branch>().WithMany().HasForeignKey(x => new { x.TenantId, x.BranchId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<SalesOrder>().WithMany().HasForeignKey(x => new { x.TenantId, x.SalesOrderId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
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
            entity.Ignore(x => x.SignedQuantity);
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

        modelBuilder.Entity<SyncOperation>(entity =>
        {
            entity.ToTable("sync_operations");
            entity.HasKey(x => x.Sequence);
            entity.Property(x => x.Sequence).ValueGeneratedOnAdd();
            entity.Property(x => x.OperationType).HasMaxLength(100).IsRequired();
            entity.Property(x => x.PayloadJson).IsRequired();
            entity.Property(x => x.PayloadSha256).HasMaxLength(64).IsRequired();
            entity.HasIndex(x => new { x.TenantId, x.OperationId }).IsUnique();
            entity.HasIndex(x => new { x.TenantId, x.Sequence });
            entity.HasIndex(x => new { x.TenantId, x.DeviceId, x.Sequence });
            entity.HasOne<ApplicationUser>().WithMany().HasForeignKey(x => x.ActorId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Branch>().WithMany().HasForeignKey(x => new { x.TenantId, x.BranchId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict).IsRequired(false);
        });

        modelBuilder.Entity<SyncDeviceBranchCursor>(entity =>
        {
            entity.ToTable("sync_device_branch_cursors");
            entity.HasKey(x => new { x.TenantId, x.DeviceId, x.BranchId });
            entity.Property(x => x.LastAcknowledgedSequence).IsConcurrencyToken();
            entity.HasIndex(x => new { x.TenantId, x.BranchId, x.LastAcknowledgedSequence });
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<ApplicationUser>().WithMany().HasForeignKey(x => x.ActorId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Branch>().WithMany().HasForeignKey(x => new { x.TenantId, x.BranchId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict);
        });

        modelBuilder.Entity<SyncDeviceCursor>(entity =>
        {
            entity.ToTable("sync_device_cursors");
            entity.HasKey(x => new { x.TenantId, x.DeviceId });
            entity.Property(x => x.LastAcknowledgedSequence).IsConcurrencyToken();
            entity.HasIndex(x => new { x.TenantId, x.LastAcknowledgedSequence });
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<ApplicationUser>().WithMany().HasForeignKey(x => x.ActorId).OnDelete(DeleteBehavior.Restrict);
        });

        modelBuilder.Entity<DevicePresence>(entity =>
        {
            entity.ToTable("device_presences");
            entity.HasKey(x => new { x.TenantId, x.DeviceId });
            entity.Property(x => x.DeviceName).HasMaxLength(100).IsRequired();
            entity.Property(x => x.Platform).HasMaxLength(40).IsRequired();
            entity.Property(x => x.ClientVersion).HasMaxLength(40).IsRequired();
            entity.HasIndex(x => new { x.TenantId, x.LastSeenAtUtc });
            entity.HasOne<Tenant>().WithMany().HasForeignKey(x => x.TenantId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<ApplicationUser>().WithMany().HasForeignKey(x => x.ActorId).OnDelete(DeleteBehavior.Restrict);
            entity.HasOne<Branch>().WithMany().HasForeignKey(x => new { x.TenantId, x.BranchId })
                .HasPrincipalKey(x => new { x.TenantId, x.Id }).OnDelete(DeleteBehavior.Restrict).IsRequired(false);
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

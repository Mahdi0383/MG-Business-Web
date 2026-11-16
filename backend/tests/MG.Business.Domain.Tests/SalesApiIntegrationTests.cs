using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using MG.Business.Domain.Enums;
using MG.Business.Infrastructure.Persistence;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;

namespace MG.Business.Domain.Tests;

public sealed class SalesApiIntegrationTests
{
    [Fact]
    public async Task SaleAtomicallyCreatesOrderPaymentInventoryAndBalancedAccountingEntries()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sales-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Sales Store",
            slug = $"sales-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var chartResponse = await client.PostAsync("/api/accounting/accounts/initialize-default-chart", null);
        Assert.Equal(HttpStatusCode.OK, chartResponse.StatusCode);
        var chart = await chartResponse.Content.ReadFromJsonAsync<AccountResponse[]>();
        var cash = chart!.Single(x => x.Code == "1000");
        var receivable = chart.Single(x => x.Code == "1100");
        var inventory = chart.Single(x => x.Code == "1200");
        var revenue = chart.Single(x => x.Code == "4000");
        var cogs = chart.Single(x => x.Code == "5000");

        var customerResponse = await client.PostAsJsonAsync("/api/customers", new
        {
            code = "C-100",
            name = "Test Customer",
            phone = "09120000000",
            email = (string?)null,
            address = (string?)null,
            notes = (string?)null
        });
        var customer = await customerResponse.Content.ReadFromJsonAsync<CustomerResponse>();
        Assert.Equal(HttpStatusCode.Created, customerResponse.StatusCode);

        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "COFFEE-01",
            name = "Coffee Beans",
            baseUnit = "kg",
            tracksInventory = true,
            defaultSalePrice = 20m
        });
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();
        Assert.Equal(HttpStatusCode.Created, productResponse.StatusCode);

        var receipt = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = product!.Id,
            type = StockMovementType.Receipt,
            quantity = 10m,
            unitCost = 5m,
            reference = "PUR-1"
        });
        Assert.Equal(HttpStatusCode.Created, receipt.StatusCode);

        var saleResponse = await client.PostAsJsonAsync("/api/sales/orders", new
        {
            customerId = customer!.Id,
            discount = 0m,
            amountPaid = 20m,
            paymentMethod = PaymentMethod.Cash,
            revenueAccountId = revenue.Id,
            paymentAccountId = cash.Id,
            receivableAccountId = receivable.Id,
            inventoryAccountId = inventory.Id,
            costOfGoodsSoldAccountId = cogs.Id,
            paymentReference = "CASH-001",
            lines = new[]
            {
                new { productId = product.Id, quantity = 2m, discount = 0m }
            }
        });

        Assert.Equal(HttpStatusCode.Created, saleResponse.StatusCode);
        var sale = await saleResponse.Content.ReadFromJsonAsync<SaleResponse>();
        Assert.NotNull(sale);
        Assert.Equal(40m, sale!.Total);
        Assert.Equal(20m, sale.PaidAmount);
        Assert.Equal(20m, sale.BalanceDue);
        Assert.Equal(10m, sale.CostOfGoodsSold);
        Assert.Equal(30m, sale.GrossProfit);

        var stock = await client.GetFromJsonAsync<StockResponse[]>("/api/inventory/stock");
        Assert.Single(stock!);
        Assert.Equal(8m, stock[0].QuantityOnHand);
        Assert.Equal(40m, stock[0].InventoryValue);

        var trial = await client.GetFromJsonAsync<TrialBalanceResponse>("/api/accounting/trial-balance");
        Assert.NotNull(trial);
        Assert.Equal(50m, trial!.TotalDebit);
        Assert.Equal(50m, trial.TotalCredit);
        Assert.True(trial.IsBalanced);

        var orders = await client.GetFromJsonAsync<SalesOrderResponse[]>("/api/sales/orders");
        Assert.Single(orders!);
        Assert.Equal(sale.Id, orders[0].Id);
    }

    [Fact]
    public async Task SaleRejectsInsufficientStockWithoutCreatingFinancialEntries()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sales-stock-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "No Stock Store",
            slug = $"no-stock-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var revenue = await CreateAccountAsync(client, "4000", "Sales Revenue", AccountType.Revenue);
        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "EMPTY-01",
            name = "Out of Stock",
            baseUnit = "unit",
            tracksInventory = true
        });
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();

        var response = await client.PostAsJsonAsync("/api/sales/orders", new
        {
            customerId = (Guid?)null,
            discount = 0m,
            amountPaid = 10m,
            paymentMethod = PaymentMethod.Cash,
            revenueAccountId = revenue.Id,
            paymentAccountId = Guid.NewGuid(),
            receivableAccountId = (Guid?)null,
            inventoryAccountId = (Guid?)null,
            costOfGoodsSoldAccountId = (Guid?)null,
            paymentReference = (string?)null,
            lines = new[] { new { productId = product!.Id, quantity = 1m, unitPrice = 10m, discount = 0m } }
        });

        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode);
        var trial = await client.GetFromJsonAsync<TrialBalanceResponse>("/api/accounting/trial-balance");
        Assert.Equal(0m, trial!.TotalDebit);
        Assert.Equal(0m, trial.TotalCredit);
    }


    [Fact]
    public async Task SaleRejectsOverpaymentAndUnassignedPartialPaymentAsValidationErrors()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sales-settlement-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Settlement Validation Store",
            slug = $"settlement-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var revenue = await CreateAccountAsync(client, "4000", "Sales Revenue", AccountType.Revenue);
        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "SERVICE-SETTLEMENT",
            name = "Service",
            baseUnit = "service",
            tracksInventory = false,
            defaultSalePrice = 10m
        });
        Assert.Equal(HttpStatusCode.Created, productResponse.StatusCode);
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();

        async Task<HttpResponseMessage> CreateInvalidSale(decimal amountPaid, string reference)
        {
            return await client.PostAsJsonAsync("/api/sales/orders", new
            {
                customerId = (Guid?)null,
                discount = 0m,
                amountPaid,
                paymentMethod = PaymentMethod.Cash,
                revenueAccountId = revenue.Id,
                paymentAccountId = Guid.NewGuid(),
                receivableAccountId = (Guid?)null,
                inventoryAccountId = (Guid?)null,
                costOfGoodsSoldAccountId = (Guid?)null,
                paymentReference = reference,
                lines = new[] { new { productId = product!.Id, quantity = 1m, unitPrice = 10m, discount = 0m } }
            });
        }

        client.DefaultRequestHeaders.Remove("Idempotency-Key");
        client.DefaultRequestHeaders.Add("Idempotency-Key", "settlement-overpayment-61");
        client.DefaultRequestHeaders.Remove("Idempotency-Key");
        client.DefaultRequestHeaders.Add("Idempotency-Key", "settlement-overpayment-61");
        using var overpayment = await CreateInvalidSale(11m, "OVERPAY");
        using var unassignedPartial = await CreateInvalidSale(5m, "PARTIAL");
        Assert.Equal(HttpStatusCode.BadRequest, overpayment.StatusCode);
        Assert.Equal(HttpStatusCode.BadRequest, unassignedPartial.StatusCode);

        var orders = await client.GetFromJsonAsync<SalesOrderResponse[]>("/api/sales/orders");
        Assert.Empty(orders!);
        var trial = await client.GetFromJsonAsync<TrialBalanceResponse>("/api/accounting/trial-balance");
        Assert.Equal(0m, trial!.TotalDebit);
        Assert.Equal(0m, trial.TotalCredit);
    }

    [Fact]
    public async Task SaleRejectsInvalidLineAndDiscountPrecisionBeforePersistence()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sales-precision-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Sales Precision Store",
            slug = $"sales-precision-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var revenue = await CreateAccountAsync(client, "4000", "Sales Revenue", AccountType.Revenue);
        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "PRECISION-SERVICE",
            name = "Precision Service",
            baseUnit = "service",
            tracksInventory = false,
            defaultSalePrice = 10m
        });
        Assert.Equal(HttpStatusCode.Created, productResponse.StatusCode);
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();

        async Task<HttpResponseMessage> CreateSale(decimal quantity, decimal? unitPrice, decimal lineDiscount, decimal orderDiscount)
        {
            return await client.PostAsJsonAsync("/api/sales/orders", new
            {
                customerId = (Guid?)null,
                discount = orderDiscount,
                amountPaid = 10m,
                paymentMethod = PaymentMethod.Cash,
                revenueAccountId = revenue.Id,
                paymentAccountId = Guid.NewGuid(),
                receivableAccountId = (Guid?)null,
                inventoryAccountId = (Guid?)null,
                costOfGoodsSoldAccountId = (Guid?)null,
                paymentReference = (string?)null,
                lines = new[] { new { productId = product!.Id, quantity, unitPrice, discount = lineDiscount } }
            });
        }

        using var invalidQuantity = await CreateSale(1.0001m, 10m, 0m, 0m);
        using var invalidPrice = await CreateSale(1m, 10.001m, 0m, 0m);
        using var invalidLineDiscount = await CreateSale(1m, 10m, 0.001m, 0m);
        using var invalidOrderDiscount = await CreateSale(1m, 10m, 0m, 0.001m);

        Assert.Equal(HttpStatusCode.BadRequest, invalidQuantity.StatusCode);
        Assert.Equal(HttpStatusCode.BadRequest, invalidPrice.StatusCode);
        Assert.Equal(HttpStatusCode.BadRequest, invalidLineDiscount.StatusCode);
        Assert.Equal(HttpStatusCode.BadRequest, invalidOrderDiscount.StatusCode);

        var orders = await client.GetFromJsonAsync<SalesOrderResponse[]>("/api/sales/orders");
        Assert.Empty(orders!);
        var trial = await client.GetFromJsonAsync<TrialBalanceResponse>("/api/accounting/trial-balance");
        Assert.Equal(0m, trial!.TotalDebit);
        Assert.Equal(0m, trial.TotalCredit);
    }

    [Fact]
    public async Task BranchScopedCashierListsOnlySalesFromAssignedBranch()
    {
        using var factory = CreateFactory();
        using var owner = await RegisterAndLoginAsync(factory, $"sales-owner-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await owner.PostAsJsonAsync("/api/tenants", new
        {
            name = "Branch Sales",
            slug = $"branch-sales-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        owner.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        owner.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var branchResponse = await owner.PostAsJsonAsync("/api/branches", new { name = "Second", code = "B02" });
        Assert.Equal(HttpStatusCode.Created, branchResponse.StatusCode);
        var branch = await branchResponse.Content.ReadFromJsonAsync<BranchCreated>();

        var cash = await CreateAccountAsync(owner, "1000", "Cash", AccountType.Asset);
        var revenue = await CreateAccountAsync(owner, "4000", "Sales Revenue", AccountType.Revenue);
        var productResponse = await owner.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "SERVICE-01",
            name = "Service Item",
            baseUnit = "service",
            tracksInventory = false,
            defaultSalePrice = 25m
        });
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();

        async Task<SaleResponse> CreateSaleAsync(string reference)
        {
            var response = await owner.PostAsJsonAsync("/api/sales/orders", new
            {
                customerId = (Guid?)null,
                discount = 0m,
                amountPaid = 25m,
                paymentMethod = PaymentMethod.Cash,
                revenueAccountId = revenue.Id,
                paymentAccountId = cash.Id,
                receivableAccountId = (Guid?)null,
                inventoryAccountId = (Guid?)null,
                costOfGoodsSoldAccountId = (Guid?)null,
                paymentReference = reference,
                lines = new[] { new { productId = product!.Id, quantity = 1m, unitPrice = 25m, discount = 0m } }
            });
            Assert.Equal(HttpStatusCode.Created, response.StatusCode);
            return (await response.Content.ReadFromJsonAsync<SaleResponse>())!;
        }

        var mainSale = await CreateSaleAsync("MAIN-SALE");
        owner.DefaultRequestHeaders.Remove("X-MG-Branch");
        owner.DefaultRequestHeaders.Add("X-MG-Branch", branch!.Id.ToString());
        var secondSale = await CreateSaleAsync("SECOND-SALE");

        var staffEmail = $"cashier-{Guid.NewGuid():N}@mg.test";
        using var cashier = await RegisterAndLoginAsync(factory, staffEmail);
        var membership = await owner.PostAsJsonAsync("/api/memberships", new
        {
            email = staffEmail,
            role = TenantRole.Cashier,
            branchId = branch.Id
        });
        Assert.Equal(HttpStatusCode.Created, membership.StatusCode);

        cashier.DefaultRequestHeaders.Add("X-MG-Tenant", tenant.TenantId.ToString());
        cashier.DefaultRequestHeaders.Add("X-MG-Branch", branch.Id.ToString());
        var orders = await cashier.GetFromJsonAsync<SalesOrderResponse[]>("/api/sales/orders");

        Assert.Single(orders!);
        Assert.Equal(secondSale.Id, orders[0].Id);
        Assert.NotEqual(mainSale.Id, orders[0].Id);
    }

    [Fact]
    public async Task InventoryRejectsInvalidMovementPrecisionAndOutboundCostBeforePersistence()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"inventory-validation-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Inventory Validation Store",
            slug = $"inventory-validation-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "INV-VALIDATION-01",
            name = "Validation Item",
            baseUnit = "unit",
            tracksInventory = true,
            defaultSalePrice = 5m
        });
        Assert.Equal(HttpStatusCode.Created, productResponse.StatusCode);
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();

        using var zeroQuantity = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = product!.Id,
            type = StockMovementType.Receipt,
            quantity = 0m,
            unitCost = 2m,
            reference = "INVALID-ZERO"
        });
        client.DefaultRequestHeaders.Remove("Idempotency-Key");
        client.DefaultRequestHeaders.Add("Idempotency-Key", "invalid-precision");
        using var excessivePrecision = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = product.Id,
            type = StockMovementType.Receipt,
            quantity = 1.0001m,
            unitCost = 2m,
            reference = "INVALID-PRECISION"
        });
        using var outboundWithCost = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = product.Id,
            type = StockMovementType.Issue,
            quantity = 1m,
            unitCost = 2m,
            reference = "INVALID-OUTBOUND-COST"
        });
        var serviceResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "INV-SERVICE-01",
            name = "Non-stock service",
            baseUnit = "service",
            tracksInventory = false,
            defaultSalePrice = 10m
        });
        Assert.Equal(HttpStatusCode.Created, serviceResponse.StatusCode);
        var service = await serviceResponse.Content.ReadFromJsonAsync<ProductResponse>();
        using var serviceMovement = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = service!.Id,
            type = StockMovementType.Receipt,
            quantity = 1m,
            unitCost = 2m,
            reference = "INVALID-SERVICE-STOCK"
        });

        Assert.Equal(HttpStatusCode.BadRequest, zeroQuantity.StatusCode);
        Assert.Equal(HttpStatusCode.BadRequest, excessivePrecision.StatusCode);
        Assert.Equal(HttpStatusCode.BadRequest, outboundWithCost.StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, serviceMovement.StatusCode);
        var stock = await client.GetFromJsonAsync<StockResponse[]>("/api/inventory/stock");
        Assert.Empty(stock!);
    }

    [Fact]
    public async Task InventoryRejectsOutboundMovementThatWouldMakeStockNegative()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"inventory-overdraw-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Inventory Overdraw Store",
            slug = $"inventory-overdraw-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "INV-OVERDRAW-01",
            name = "Stocked Item",
            baseUnit = "unit",
            tracksInventory = true,
            defaultSalePrice = 8m
        });
        Assert.Equal(HttpStatusCode.Created, productResponse.StatusCode);
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();

        using var receipt = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = product!.Id,
            type = StockMovementType.Receipt,
            quantity = 3m,
            unitCost = 2m,
            reference = "RECEIPT-3"
        });
        Assert.Equal(HttpStatusCode.Created, receipt.StatusCode);

        using var overdraw = await client.PostAsJsonAsync("/api/inventory/movements", new
        {
            productId = product.Id,
            type = StockMovementType.Issue,
            quantity = 4m,
            unitCost = (decimal?)null,
            reference = "ISSUE-4"
        });
        Assert.Equal(HttpStatusCode.Conflict, overdraw.StatusCode);

        var stock = await client.GetFromJsonAsync<StockResponse[]>("/api/inventory/stock");
        Assert.Single(stock!);
        Assert.Equal(3m, stock[0].QuantityOnHand);
        Assert.Equal(6m, stock[0].InventoryValue);
        Assert.Equal(1, stock[0].MovementCount);
    }

    [Fact]
    public async Task CustomerBalanceCanBeSettledInMultipleAtomicPayments()
    {
        using var factory = CreateFactory();
        using var client = await RegisterAndLoginAsync(factory, $"sales-payment-{Guid.NewGuid():N}@mg.test");

        var tenantResponse = await client.PostAsJsonAsync("/api/tenants", new
        {
            name = "Payment Settlement Store",
            slug = $"payment-settlement-{Guid.NewGuid():N}",
            firstBranchName = "Main"
        });
        var tenant = await tenantResponse.Content.ReadFromJsonAsync<TenantCreated>();
        Assert.Equal(HttpStatusCode.Created, tenantResponse.StatusCode);
        client.DefaultRequestHeaders.Add("X-MG-Tenant", tenant!.TenantId.ToString());
        client.DefaultRequestHeaders.Add("X-MG-Branch", tenant.BranchId.ToString());

        var cash = await CreateAccountAsync(client, "1000", "Cash", AccountType.Asset);
        var receivable = await CreateAccountAsync(client, "1100", "Receivable", AccountType.Asset);
        var revenue = await CreateAccountAsync(client, "4000", "Revenue", AccountType.Revenue);
        var customerResponse = await client.PostAsJsonAsync("/api/customers", new
        {
            code = "PAY-C-1", name = "Payment Customer", phone = (string?)null,
            email = (string?)null, address = (string?)null, notes = (string?)null
        });
        var customer = await customerResponse.Content.ReadFromJsonAsync<CustomerResponse>();
        Assert.Equal(HttpStatusCode.Created, customerResponse.StatusCode);

        var productResponse = await client.PostAsJsonAsync("/api/inventory/products", new
        {
            sku = "PAY-SERVICE", name = "Service", baseUnit = "service",
            tracksInventory = false, defaultSalePrice = 100m
        });
        var product = await productResponse.Content.ReadFromJsonAsync<ProductResponse>();
        Assert.Equal(HttpStatusCode.Created, productResponse.StatusCode);

        var saleResponse = await client.PostAsJsonAsync("/api/sales/orders", new
        {
            customerId = customer!.Id, discount = 0m, amountPaid = 0m,
            paymentMethod = PaymentMethod.OnAccount, revenueAccountId = revenue.Id,
            paymentAccountId = (Guid?)null, receivableAccountId = receivable.Id,
            inventoryAccountId = (Guid?)null, costOfGoodsSoldAccountId = (Guid?)null,
            paymentReference = (string?)null,
            lines = new[] { new { productId = product!.Id, quantity = 1m, unitPrice = 100m, discount = 0m } }
        });
        Assert.Equal(HttpStatusCode.Created, saleResponse.StatusCode);
        var sale = await saleResponse.Content.ReadFromJsonAsync<SaleResponse>();
        Assert.Equal(100m, sale!.BalanceDue);

        client.DefaultRequestHeaders.Add("Idempotency-Key", "invalid-zero");
        using var zeroPayment = await client.PostAsJsonAsync($"/api/sales/orders/{sale.Id}/payments", new
        {
            amount = 0m, paymentMethod = PaymentMethod.Cash,
            paymentAccountId = cash.Id, receivableAccountId = receivable.Id,
            paymentReference = "ZERO"
        });
        using var excessivePrecision = await client.PostAsJsonAsync($"/api/sales/orders/{sale.Id}/payments", new
        {
            amount = 1.001m, paymentMethod = PaymentMethod.Cash,
            paymentAccountId = cash.Id, receivableAccountId = receivable.Id,
            paymentReference = "PRECISION"
        });
        client.DefaultRequestHeaders.Remove("Idempotency-Key");
        client.DefaultRequestHeaders.Add("Idempotency-Key", "invalid-on-account");
        using var onAccountReceipt = await client.PostAsJsonAsync($"/api/sales/orders/{sale.Id}/payments", new
        {
            amount = 10m, paymentMethod = PaymentMethod.OnAccount,
            paymentAccountId = cash.Id, receivableAccountId = receivable.Id,
            paymentReference = "ON-ACCOUNT"
        });
        client.DefaultRequestHeaders.Remove("Idempotency-Key");
        client.DefaultRequestHeaders.Add("Idempotency-Key", "invalid-foreign-account");
        using var foreignCashAccount = await client.PostAsJsonAsync($"/api/sales/orders/{sale.Id}/payments", new
        {
            amount = 10m, paymentMethod = PaymentMethod.Cash,
            paymentAccountId = Guid.NewGuid(), receivableAccountId = receivable.Id,
            paymentReference = "FOREIGN-ACCOUNT"
        });
        Assert.Equal(HttpStatusCode.BadRequest, zeroPayment.StatusCode);
        Assert.Equal(HttpStatusCode.BadRequest, excessivePrecision.StatusCode);
        Assert.Equal(HttpStatusCode.BadRequest, onAccountReceipt.StatusCode);
        Assert.Equal(HttpStatusCode.BadRequest, foreignCashAccount.StatusCode);
        client.DefaultRequestHeaders.Remove("Idempotency-Key");
        client.DefaultRequestHeaders.Add("Idempotency-Key", "invalid-same-account");
        using var sameSettlementAccount = await client.PostAsJsonAsync($"/api/sales/orders/{sale.Id}/payments", new
        {
            amount = 10m, paymentMethod = PaymentMethod.Cash,
            paymentAccountId = cash.Id, receivableAccountId = cash.Id,
            paymentReference = "SAME-ACCOUNT"
        });
        Assert.Equal(HttpStatusCode.BadRequest, sameSettlementAccount.StatusCode);

        client.DefaultRequestHeaders.Remove("Idempotency-Key");
        client.DefaultRequestHeaders.Add("Idempotency-Key", "settlement-first-40");
        var firstPayment = await client.PostAsJsonAsync($"/api/sales/orders/{sale.Id}/payments", new
        {
            amount = 40m, paymentMethod = PaymentMethod.Cash,
            paymentAccountId = cash.Id, receivableAccountId = receivable.Id,
            paymentReference = "PAY-40"
        });
        Assert.Equal(HttpStatusCode.OK, firstPayment.StatusCode);
        var first = await firstPayment.Content.ReadFromJsonAsync<PaymentBalanceResponse>();
        var firstPaymentId = first!.PaymentId;
        var firstReplay = await client.PostAsJsonAsync($"/api/sales/orders/{sale.Id}/payments", new
        {
            amount = 40m, paymentMethod = PaymentMethod.Cash,
            paymentAccountId = cash.Id, receivableAccountId = receivable.Id,
            paymentReference = "PAY-40"
        });
        Assert.Equal(HttpStatusCode.OK, firstReplay.StatusCode);
        var replayJson = await firstReplay.Content.ReadFromJsonAsync<System.Text.Json.JsonElement>();
        Assert.True(replayJson.GetProperty("replayed").GetBoolean());
        Assert.Equal(firstPaymentId, replayJson.GetProperty("paymentId").GetGuid());
        var sameKeyDifferentAmount = await client.PostAsJsonAsync($"/api/sales/orders/{sale.Id}/payments", new
        {
            amount = 39m, paymentMethod = PaymentMethod.Cash,
            paymentAccountId = cash.Id, receivableAccountId = receivable.Id,
            paymentReference = "PAY-40"
        });
        Assert.Equal(HttpStatusCode.Conflict, sameKeyDifferentAmount.StatusCode);
        Assert.Equal(40m, first!.PaidAmount);
        Assert.Equal(60m, first.BalanceDue);

        client.DefaultRequestHeaders.Remove("Idempotency-Key");
        client.DefaultRequestHeaders.Add("Idempotency-Key", "settlement-concurrent-10");
        var concurrentRequests = Enumerable.Range(0, 2).Select(_ =>
            client.PostAsJsonAsync($"/api/sales/orders/{sale.Id}/payments", new
            {
                amount = 10m, paymentMethod = PaymentMethod.Cash,
                paymentAccountId = cash.Id, receivableAccountId = receivable.Id,
                paymentReference = "PAY-CONCURRENT"
            })).ToArray();
        var concurrentResponses = await Task.WhenAll(concurrentRequests);
        var concurrentDiagnostics = await Task.WhenAll(concurrentResponses.Select(async response =>
            new { response.StatusCode, Body = await response.Content.ReadAsStringAsync() }));
        Assert.All(concurrentDiagnostics, response =>
            Assert.True(response.StatusCode == HttpStatusCode.OK, $"HTTP {(int)response.StatusCode}: {response.Body}"));
        var concurrentResults = concurrentDiagnostics.Select(response =>
            System.Text.Json.JsonDocument.Parse(response.Body).RootElement.Clone()).ToArray();
        Assert.Equal(concurrentResults[0].GetProperty("paymentId").GetGuid(),
            concurrentResults[1].GetProperty("paymentId").GetGuid());
        Assert.Single(concurrentResults.Select(result => result.GetProperty("paymentId").GetGuid()).Distinct());

        using var overpayment = await client.PostAsJsonAsync($"/api/sales/orders/{sale.Id}/payments", new
        {
            amount = 51m, paymentMethod = PaymentMethod.Cash,
            paymentAccountId = cash.Id, receivableAccountId = receivable.Id,
            paymentReference = "PAY-51"
        });
        Assert.Equal(HttpStatusCode.Conflict, overpayment.StatusCode);

        client.DefaultRequestHeaders.Remove("Idempotency-Key");
        client.DefaultRequestHeaders.Add("Idempotency-Key", "settlement-final-50");
        var finalPayment = await client.PostAsJsonAsync($"/api/sales/orders/{sale.Id}/payments", new
        {
            amount = 50m, paymentMethod = PaymentMethod.Card,
            paymentAccountId = cash.Id, receivableAccountId = receivable.Id,
            paymentReference = "PAY-50"
        });
        Assert.Equal(HttpStatusCode.OK, finalPayment.StatusCode);
        var final = await finalPayment.Content.ReadFromJsonAsync<PaymentBalanceResponse>();
        Assert.Equal(100m, final!.PaidAmount);
        Assert.Equal(0m, final.BalanceDue);

        var trial = await client.GetFromJsonAsync<TrialBalanceResponse>("/api/accounting/trial-balance");
        Assert.True(trial!.IsBalanced);
        Assert.Equal(200m, trial.TotalDebit);
        Assert.Equal(200m, trial.TotalCredit);

        var paymentHistory = await client.GetFromJsonAsync<SalesPaymentResponse[]>($"/api/sales/orders/{sale.Id}/payments");
        Assert.NotNull(paymentHistory);
        Assert.Equal(3, paymentHistory!.Length);
        Assert.Equal(new[] { 10m, 40m, 50m }, paymentHistory.Select(x => x.Amount).OrderBy(x => x).ToArray());
        Assert.Equal(100m, paymentHistory.Sum(x => x.Amount));
        Assert.Contains(paymentHistory, payment => payment.Reference == "PAY-40" && payment.Amount == 40m);
        Assert.Contains(paymentHistory, payment => payment.Reference == "PAY-CONCURRENT" && payment.Amount == 10m);
        Assert.Contains(paymentHistory, payment => payment.Reference == "PAY-50" && payment.Amount == 50m);
    }

    private static async Task<AccountResponse> CreateAccountAsync(HttpClient client, string code, string name, AccountType type)
    {
        var response = await client.PostAsJsonAsync("/api/accounting/accounts", new { code, name, type });
        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        return (await response.Content.ReadFromJsonAsync<AccountResponse>())!;
    }

    private static async Task<HttpClient> RegisterAndLoginAsync(WebApplicationFactory<Program> factory, string email)
    {
        using (var scope = factory.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<BusinessDbContext>();
            await db.Database.EnsureCreatedAsync();
        }

        var client = factory.CreateClient();
        var register = await client.PostAsJsonAsync("/auth/register", new { email, password = "Strong!Passw0rd" });
        Assert.Equal(HttpStatusCode.OK, register.StatusCode);
        var login = await client.PostAsJsonAsync("/auth/login", new { email, password = "Strong!Passw0rd", useCookies = false });
        Assert.Equal(HttpStatusCode.OK, login.StatusCode);
        var tokens = await login.Content.ReadFromJsonAsync<TokenResponse>();
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", tokens!.AccessToken);
        return client;
    }

    private static WebApplicationFactory<Program> CreateFactory()
    {
        var connectionString = $"Data Source=mg-business-tests-{Guid.NewGuid():N};Mode=Memory;Cache=Shared";
        var keepAliveConnection = new Microsoft.Data.Sqlite.SqliteConnection(connectionString);
        keepAliveConnection.Open();
        return new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
            builder.ConfigureServices(services =>
            {
                services.RemoveAll<DbContextOptions<BusinessDbContext>>();
                services.RemoveAll<IDbContextOptionsConfiguration<BusinessDbContext>>();
                services.RemoveAll<BusinessDbContext>();
                services.AddSingleton(keepAliveConnection);
                services.AddDbContext<BusinessDbContext>(options => options.UseSqlite(connectionString));
            }));
    }

    private sealed record TokenResponse(string TokenType, string AccessToken, int ExpiresIn, string RefreshToken);
    private sealed record TenantCreated(Guid TenantId, Guid BranchId, string Name, string Slug, string Role);
    private sealed record BranchCreated(Guid Id, string Code, string Name, bool IsActive, DateTimeOffset CreatedAtUtc);
    private sealed record AccountResponse(Guid Id, string Code, string Name, string Type, bool IsActive);
    private sealed record CustomerResponse(Guid Id, string Code, string Name, string? Phone, string? Email, string? Address, string? Notes, bool IsActive);
    private sealed record ProductResponse(Guid Id, string Sku, string Name, string BaseUnit, bool TracksInventory, decimal DefaultSalePrice, bool IsActive);
    private sealed record PaymentBalanceResponse(Guid Id, string Number, decimal Total, decimal PaidAmount, decimal BalanceDue, Guid PaymentId, bool Replayed);
    private sealed record SalesPaymentResponse(Guid Id, decimal Amount, string Method, string? Reference, DateTimeOffset CreatedAtUtc);
    private sealed record SaleResponse(Guid Id, string Number, decimal Subtotal, decimal LineDiscountTotal, decimal Discount, decimal Total, decimal PaidAmount, decimal BalanceDue, decimal CostOfGoodsSold, decimal GrossProfit, string Status, DateTimeOffset CreatedAtUtc);
    private sealed record StockResponse(Guid ProductId, Guid BranchId, decimal QuantityOnHand, decimal AverageUnitCost, decimal InventoryValue, int MovementCount);
    private sealed record TrialBalanceResponse(TrialAccount[] Accounts, decimal TotalDebit, decimal TotalCredit, bool IsBalanced);
    private sealed record TrialAccount(Guid Id, string Code, string Name, string Type, decimal Debit, decimal Credit, decimal NetDebit, decimal NetCredit);
    private sealed record SalesOrderResponse(Guid Id, string Number, Guid? CustomerId, decimal Subtotal, decimal LineDiscountTotal, decimal Discount, decimal Total, decimal PaidAmount, decimal BalanceDue, string Status, DateTimeOffset CreatedAtUtc);
}

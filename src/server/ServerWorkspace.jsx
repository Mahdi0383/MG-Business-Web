import React, { useCallback, useEffect, useMemo, useState } from "react";
import { formatApiError, isNetworkUnavailable } from "../api/client.js";

const money = value => new Intl.NumberFormat("fa-IR").format(Number(value || 0)) + " تومان";
const count = value => new Intl.NumberFormat("fa-IR").format(Number(value || 0));
const dateTime = value => value ? new Date(value).toLocaleString("fa-IR") : "—";

const navigation = [
  ["dashboard", "◫", "نمای کلی"],
  ["products", "▦", "کالا و انبار"],
  ["customers", "♙", "مشتریان"],
  ["sales", "▣", "فروش / صندوق"],
  ["accounts", "∑", "حساب‌ها"],
  ["cafe", "☕", "آشپزخانه"],
  ["gamenet", "⌁", "گیم‌نت"],
  ["devices", "◉", "وضعیت دستگاه‌ها"],
  ["syncConflicts", "⚠", "تعارض‌های همگام‌سازی"]
];

const accountTypeName = {
  Asset: "دارایی",
  Liability: "بدهی",
  Equity: "حقوق مالکانه",
  Revenue: "درآمد",
  Expense: "هزینه"
};

const kitchenStatus = {
  Pending: "در انتظار",
  Preparing: "در حال آماده‌سازی",
  Ready: "آماده تحویل",
  Completed: "تحویل‌شده",
  Cancelled: "لغوشده"
};

const serviceType = {
  DineIn: "سرو داخل",
  Takeaway: "بیرون‌بر",
  Delivery: "ارسال"
};

const kitchenActions = {
  Pending: [["شروع آماده‌سازی", 2], ["لغو", 5]],
  Preparing: [["آماده شد", 3], ["لغو", 5]],
  Ready: [["تحویل شد", 4]],
  Completed: [],
  Cancelled: []
};

function ServerWorkspace({ client, operationStore, actorId, syncStatus = "idle", pendingOperations = 0, incomingOperations = 0, openSyncConflicts = 0, disconnectedDevices = [], deviceStatuses = [], tenant, branch, onLogout, onSwitchContext }) {
  const [page, setPage] = useState("dashboard");
  const [products, setProducts] = useState([]);
  const [stockRows, setStockRows] = useState([]);
  const [trialBalance, setTrialBalance] = useState(null);
  const [customers, setCustomers] = useState([]);
  const [sales, setSales] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [kitchenTickets, setKitchenTickets] = useState([]);
  const [devices, setDevices] = useState([]);
  const [syncConflicts, setSyncConflicts] = useState([]);
  const [activeSessions, setActiveSessions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [modal, setModal] = useState("");
  const [form, setForm] = useState({});
  const [saving, setSaving] = useState(false);

  const canViewProducts = ["Owner", "Manager", "Accountant", "Cashier", "Waiter", "GameOperator", "InventoryManager", "Viewer"].includes(tenant.role);
  const canViewCustomers = ["Owner", "Manager", "Accountant", "Cashier", "Waiter", "Viewer"].includes(tenant.role);
  const canViewSales = ["Owner", "Manager", "Accountant", "Cashier", "Viewer"].includes(tenant.role);
  const canViewAccounts = ["Owner", "Manager", "Accountant", "Cashier", "Viewer"].includes(tenant.role);
  const canViewAccountingReports = ["Owner", "Manager", "Accountant", "Viewer"].includes(tenant.role);
  const canViewCafe = ["Owner", "Manager", "Accountant", "Cashier", "Waiter", "KitchenStaff", "Viewer"].includes(tenant.role);
  const canViewGameNet = ["Owner", "Manager", "Cashier", "GameOperator", "Accountant", "Viewer"].includes(tenant.role);
  const canCreateProduct = ["Owner", "Manager", "InventoryManager"].includes(tenant.role);
  const canCreateCustomer = ["Owner", "Manager", "Cashier"].includes(tenant.role);
  const canEditCustomer = ["Owner", "Manager", "Cashier"].includes(tenant.role);
  const canCreateAccount = ["Owner", "Manager", "Accountant"].includes(tenant.role);
  const canCreateSales = ["Owner", "Manager", "Cashier"].includes(tenant.role);
  const canUpdateKitchen = ["Owner", "Manager", "Waiter", "KitchenStaff"].includes(tenant.role);
  const canCreateKitchen = ["Owner", "Manager", "Cashier", "Waiter"].includes(tenant.role);
  const canStartGame = ["Owner", "Manager", "GameOperator", "Cashier"].includes(tenant.role);

  const visibleNavigation = navigation.filter(([id]) => {
    if (id === "products") return canViewProducts;
    if (id === "customers") return canViewCustomers;
    if (id === "sales") return canViewSales;
    if (id === "accounts") return canViewAccounts;
    if (id === "cafe") return canViewCafe;
    if (id === "gamenet") return canViewGameNet;
    if (id === "devices") return ["Owner", "Manager"].includes(tenant.role);
    if (id === "syncConflicts") return ["Owner", "Manager", "Accountant"].includes(tenant.role);
    return true;
  });

  const loadCustomers = async () => {
    let localRows = [];
    try {
      localRows = await operationStore.getCustomers(tenant.id);
      if (localRows.length) setCustomers(localRows);
    } catch { /* Local storage may be unavailable in private browsing. */ }
    try {
      const serverRows = await client.get("/api/customers");
      try {
        await operationStore.cacheServerCustomers(tenant.id, serverRows || []);
        localRows = await operationStore.getCustomers(tenant.id);
      } catch {
        // Keep the online customer view available when browser storage is disabled or full.
      }
      const merged = new Map((localRows || []).map(row => [row.id, { ...row }]));
      for (const row of serverRows || []) {
        const local = merged.get(row.id);
        merged.set(row.id, local?.syncStatus === "pending"
          ? { ...row, ...local }
          : { ...local, ...row, syncStatus: "synced" });
      }
      return [...merged.values()].sort((left, right) =>
        String(right.updatedAtUtc || right.createdAtUtc || "").localeCompare(String(left.updatedAtUtc || left.createdAtUtc || "")));
    } catch (loadError) {
      if (isNetworkUnavailable(loadError)) return localRows;
      throw loadError;
    }
  };

  const loadData = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      if (page === "syncConflicts") {
        const allowTenantWide = ["Owner", "Manager"].includes(tenant.role);
        const conflicts = await operationStore.getOpenConflicts(tenant.id, branch.id, allowTenantWide, 100);
        setSyncConflicts(conflicts || []);
      }
      else if (page === "products") {
        const [productRows, currentStock] = await Promise.all([
          client.get("/api/inventory/products"),
          client.get("/api/inventory/stock")
        ]);
        setProducts(productRows || []);
        setStockRows(currentStock || []);
      }
      else if (page === "customers") setCustomers(await loadCustomers());
      else if (page === "sales") setSales(await client.get("/api/sales/orders"));
      else if (page === "accounts") {
        const accountRows = await client.get("/api/accounting/accounts");
        setAccounts(accountRows || []);
        if (canViewAccountingReports) setTrialBalance(await client.get("/api/accounting/trial-balance"));
      }
      else if (page === "cafe") setKitchenTickets(await client.get("/api/cafe/kitchen/tickets"));
      else if (page === "gamenet") {
        const [deviceRows, sessionRows] = await Promise.all([
          client.get("/api/gamenet/devices"),
          client.get("/api/gamenet/sessions/active")
        ]);
        setDevices(deviceRows || []);
        setActiveSessions(sessionRows || []);
      } else {
        const [productRows, currentStock, customerRows, salesRows, accountRows, ticketRows, sessionRows] = await Promise.all([
          canViewProducts ? client.get("/api/inventory/products") : Promise.resolve([]),
          canViewProducts ? client.get("/api/inventory/stock") : Promise.resolve([]),
          canViewCustomers ? loadCustomers() : Promise.resolve([]),
          canViewSales ? client.get("/api/sales/orders") : Promise.resolve([]),
          canViewAccounts ? client.get("/api/accounting/accounts") : Promise.resolve([]),
          canViewCafe ? client.get("/api/cafe/kitchen/tickets") : Promise.resolve([]),
          canViewGameNet ? client.get("/api/gamenet/sessions/active") : Promise.resolve([])
        ]);
        setProducts(productRows || []);
        setStockRows(currentStock || []);
        if (canViewAccountingReports) {
          try { setTrialBalance(await client.get("/api/accounting/trial-balance")); }
          catch { setTrialBalance(null); }
        }
        setCustomers(customerRows || []);
        setSales(salesRows || []);
        setAccounts(accountRows || []);
        setKitchenTickets(ticketRows || []);
        setActiveSessions(sessionRows || []);
      }
    } catch (loadError) {
      setError(formatApiError(loadError));
    } finally {
      setLoading(false);
    }
  }, [client, operationStore, tenant.id, tenant.role, branch.id, page, canViewProducts, canViewCustomers, canViewSales, canViewAccounts, canViewAccountingReports, canViewCafe, canViewGameNet]);

  useEffect(() => { loadData(); }, [loadData]);

  useEffect(() => {
    const refreshCustomerProjection = () => {
      if (page === "customers" || page === "dashboard") loadData();
    };
    window.addEventListener("mg-customer-sync-updated", refreshCustomerProjection);
    return () => window.removeEventListener("mg-customer-sync-updated", refreshCustomerProjection);
  }, [page, loadData]);

  const openCreate = type => {
    const initial = type === "product"
      ? { sku: "", name: "", baseUnit: "عدد", tracksInventory: true, defaultSalePrice: "" }
      : type === "customer"
        ? { code: "", name: "", phone: "", email: "", address: "", notes: "" }
        : { code: "", name: "", type: "1" };
    setForm(initial);
    setModal(type);
    setError("");
  };

  const openEditCustomer = customer => {
    if (!canEditCustomer || customer.syncStatus === "pending") return;
    setForm({
      customerId: customer.id,
      baseVersion: customer.version,
      code: customer.code,
      name: customer.name,
      phone: customer.phone || "",
      email: customer.email || "",
      address: customer.address || "",
      notes: customer.notes || ""
    });
    setModal("customerEdit");
    setError("");
  };

  const openSale = async () => {
    setSaving(true);
    setError("");
    try {
      const [productRows, customerRows, accountRows] = await Promise.all([
        client.get("/api/inventory/products"),
        client.get("/api/customers"),
        client.get("/api/accounting/accounts")
      ]);
      setProducts(productRows || []);
      setCustomers(customerRows || []);
      setAccounts(accountRows || []);
      const activeProducts = (productRows || []).filter(product => product.isActive);

      const assets = (accountRows || []).filter(x => x.type === "Asset");
      const revenues = (accountRows || []).filter(x => x.type === "Revenue");
      const expenses = (accountRows || []).filter(x => x.type === "Expense");
      const revenue = revenues.find(x => x.code === "4000") || revenues[0];
      const cash = assets.find(x => x.code === "1000") || assets[0];
      const receivable = assets.find(x => x.code === "1100") || assets[0];
      const inventory = assets.find(x => x.code === "1200") || assets[0];
      const cogs = expenses.find(x => x.code === "5000") || expenses[0];

      if (!activeProducts.length) {
        setError("برای ثبت فروش ابتدا حداقل یک کالای فعال یا خدمت ثبت کنید.");
        setPage("products");
        return;
      }
      if (!revenue || !cash) {
        setError("حساب درآمد و حساب دریافت وجه موجود نیست. ابتدا از بخش حساب‌ها سرفصل‌های پایه را ایجاد کنید.");
        setPage("accounts");
        return;
      }

      setForm({
        customerId: "",
        discount: "0",
        amountPaid: "",
        paymentMethod: "1",
        revenueAccountId: revenue.id,
        paymentAccountId: cash.id,
        receivableAccountId: receivable?.id || "",
        inventoryAccountId: inventory?.id || "",
        costOfGoodsSoldAccountId: cogs?.id || "",
        paymentReference: "",
        lines: [{ productId: activeProducts[0].id, quantity: "1", unitPrice: String(activeProducts[0].defaultSalePrice || 0), discount: "0" }]
      });
      setModal("sale");
    } catch (openError) {
      setError(formatApiError(openError));
    } finally {
      setSaving(false);
    }
  };

  const updateSaleLine = (index, field, value) => {
    const lines = [...form.lines];
    lines[index] = { ...lines[index], [field]: value };
    if (field === "productId") {
      const product = products.find(x => x.id === value);
      lines[index].unitPrice = String(product?.defaultSalePrice || 0);
    }
    setForm({ ...form, lines });
  };

  const saleSubtotal = (form.lines || []).reduce((sum, line) =>
    sum + Number(line.quantity || 0) * Number(line.unitPrice || 0) - Number(line.discount || 0), 0);
  const saleTotal = Math.max(0, saleSubtotal - Number(form.discount || 0));
  const paidAmount = form.amountPaid === "" ? saleTotal : Number(form.amountPaid || 0);
  const balanceDue = Math.max(0, saleTotal - paidAmount);

  const initializeDefaultChart = async () => {
    setSaving(true);
    setError("");
    try {
      const result = await client.request("/api/accounting/accounts/initialize-default-chart", { method: "POST" });
      setAccounts(result || []);
    } catch (initError) {
      setError(formatApiError(initError));
    } finally {
      setSaving(false);
    }
  };

  const save = async event => {
    event.preventDefault();
    setSaving(true);
    setError("");
    let successNotice = "";
    try {
      if (modal === "product") {
        await client.post("/api/inventory/products", {
          sku: form.sku.trim(),
          name: form.name.trim(),
          baseUnit: form.baseUnit.trim(),
          tracksInventory: Boolean(form.tracksInventory),
          defaultSalePrice: Number(form.defaultSalePrice || 0)
        });
      } else if (modal === "customer") {
        await operationStore.createCustomer({
          tenantId: tenant.id,
          branchId: branch.id,
          actorId,
          customer: {
            code: form.code,
            name: form.name,
            phone: form.phone,
            email: form.email,
            address: form.address,
            notes: form.notes
          }
        });
        successNotice = "مشتری روی دستگاه ذخیره شد و در صف همگام‌سازی قرار گرفت.";
      } else if (modal === "customerEdit") {
        await operationStore.updateCustomer({
          tenantId: tenant.id,
          branchId: branch.id,
          actorId,
          customerId: form.customerId,
          customer: {
            name: form.name,
            phone: form.phone,
            email: form.email,
            address: form.address,
            notes: form.notes
          }
        });
        successNotice = "تغییرات مشتری به‌صورت محلی ذخیره و در صف همگام‌سازی قرار گرفت.";
      } else if (modal === "account") {
        await client.post("/api/accounting/accounts", {
          code: form.code.trim(),
          name: form.name.trim(),
          type: Number(form.type)
        });
      } else if (modal === "gameDevice") {
        await client.post("/api/gamenet/devices", {
          serviceProductId: form.serviceProductId,
          code: form.code.trim(),
          name: form.name.trim(),
          hourlyRate: Number(form.hourlyRate),
          billingIntervalMinutes: Number(form.billingIntervalMinutes)
        });
      } else if (modal === "gameStart") {
        await client.post("/api/gamenet/sessions", {
          deviceId: form.deviceId,
          customerId: form.customerId || null
        });
      } else if (modal === "gameClose") {
        const latestQuote = await client.get(`/api/gamenet/sessions/${form.sessionId}/quote`);
        let amountPaid = form.amountPaid === "" ? latestQuote.totalAmount : Number(form.amountPaid);
        const initialQuoteTotal = Number(form.quote?.totalAmount ?? latestQuote.totalAmount);
        if (Math.abs(amountPaid - initialQuoteTotal) < 0.01) amountPaid = latestQuote.totalAmount;
        if (amountPaid < 0 || amountPaid > latestQuote.totalAmount)
          throw new Error("مبلغ پرداختی باید بین صفر و مبلغ قابل پرداخت باشد.");
        if (amountPaid < latestQuote.totalAmount && !form.customerId)
          throw new Error("برای تسویه ناقص یا نسیه، سانس باید به نام مشتری ثبت شده باشد.");
        if (amountPaid > 0 && !form.paymentAccountId)
          throw new Error("حساب دریافت وجه را انتخاب کنید.");
        if (amountPaid < latestQuote.totalAmount && !form.receivableAccountId)
          throw new Error("حساب دریافتنی را انتخاب کنید.");
        await client.post(`/api/gamenet/sessions/${form.sessionId}/close`, {
          amountPaid,
          paymentMethod: amountPaid === 0 ? 5 : Number(form.paymentMethod || 1),
          revenueAccountId: form.revenueAccountId,
          paymentAccountId: amountPaid > 0 ? form.paymentAccountId : null,
          receivableAccountId: amountPaid < latestQuote.totalAmount ? form.receivableAccountId : null,
          paymentReference: form.paymentReference.trim() || null
        });
      } else if (modal === "sale") {
        const lines = form.lines.filter(line => line.productId).map(line => ({
          productId: line.productId,
          quantity: Number(line.quantity),
          unitPrice: line.unitPrice === "" ? null : Number(line.unitPrice),
          discount: Number(line.discount || 0)
        }));
        if (!lines.length) throw new Error("حداقل یک قلم کالا یا خدمت انتخاب کنید.");
        if (saleTotal <= 0) throw new Error("مبلغ نهایی فاکتور باید بیشتر از صفر باشد.");
        if (paidAmount < 0 || paidAmount > saleTotal) throw new Error("مبلغ پرداختی باید بین صفر و مبلغ کل باشد.");
        if (balanceDue > 0 && !form.customerId) throw new Error("برای فروش نسیه یا تسویه ناقص، انتخاب مشتری الزامی است.");
        if (paidAmount > 0 && !form.paymentAccountId) throw new Error("حساب دریافت وجه را انتخاب کنید.");
        if (balanceDue > 0 && !form.receivableAccountId) throw new Error("حساب دریافتنی را انتخاب کنید.");

        const createdSale = await client.post("/api/sales/orders", {
          customerId: form.customerId || null,
          discount: Number(form.discount || 0),
          amountPaid: paidAmount,
          paymentMethod: paidAmount === 0 ? 5 : Number(form.paymentMethod || 1),
          revenueAccountId: form.revenueAccountId,
          paymentAccountId: paidAmount > 0 ? form.paymentAccountId : null,
          receivableAccountId: balanceDue > 0 ? form.receivableAccountId : null,
          inventoryAccountId: form.inventoryAccountId || null,
          costOfGoodsSoldAccountId: form.costOfGoodsSoldAccountId || null,
          paymentReference: form.paymentReference.trim() || null,
          lines
        });

        if (form.sendToKitchen) {
          try {
            await client.post("/api/cafe/kitchen/tickets", {
              salesOrderId: createdSale.id,
              serviceType: Number(form.serviceType || 1),
              tableLabel: form.tableLabel.trim() || null,
              notes: form.kitchenNotes.trim() || null
            });
          } catch (kitchenError) {
            successNotice = `فاکتور ${createdSale.number} ثبت شد، اما ارسال به آشپزخانه انجام نشد: ${formatApiError(kitchenError)}`;
          }
        }
      }

      setModal("");
      await loadData();
      if (successNotice) setError(successNotice);
    } catch (saveError) {
      setError(formatApiError(saveError));
    } finally {
      setSaving(false);
    }
  };

  const updateKitchenStatus = async (ticketId, status) => {
    setSaving(true);
    setError("");
    try {
      await client.put(`/api/cafe/kitchen/tickets/${ticketId}/status`, { status });
      await loadData();
    } catch (updateError) {
      setError(formatApiError(updateError));
    } finally {
      setSaving(false);
    }
  };

  const openCreateDevice = async () => {
    setSaving(true);
    setError("");
    try {
      const productRows = await client.get("/api/inventory/products");
      const serviceProducts = (productRows || []).filter(product => product.isActive && !product.tracksInventory);
      setProducts(productRows || []);
      if (!serviceProducts.length) {
        setError("برای تعریف دستگاه، ابتدا در بخش کالاها یک خدمت بدون موجودی انبار ثبت کنید.");
        setPage("products");
        return;
      }
      setForm({
        serviceProductId: serviceProducts[0].id,
        code: "",
        name: "",
        hourlyRate: "",
        billingIntervalMinutes: "15"
      });
      setModal("gameDevice");
    } catch (deviceError) {
      setError(formatApiError(deviceError));
    } finally {
      setSaving(false);
    }
  };

  const openStartGame = async deviceId => {
    setSaving(true);
    setError("");
    try {
      const customerRows = await client.get("/api/customers");
      setCustomers(customerRows || []);
      setForm({ deviceId, customerId: "" });
      setModal("gameStart");
    } catch (startError) {
      setError(formatApiError(startError));
    } finally {
      setSaving(false);
    }
  };

  const openCloseGame = async session => {
    setSaving(true);
    setError("");
    try {
      const [quote, settlementAccounts] = await Promise.all([
        client.get(`/api/gamenet/sessions/${session.id}/quote`),
        client.get("/api/gamenet/settlement-accounts")
      ]);
      setAccounts(settlementAccounts || []);
      const assets = settlementAccounts.filter(account => account.type === "Asset");
      const revenues = settlementAccounts.filter(account => account.type === "Revenue");
      const revenue = revenues.find(account => account.code === "4000") || revenues[0];
      const cash = assets.find(account => account.code === "1000") || assets[0];
      const receivable = assets.find(account => account.code === "1100") || assets[0];
      if (!revenue || !cash) throw new Error("حساب درآمد و حساب دریافت وجه تعریف نشده است. ابتدا سرفصل‌های پایه را در حسابداری ایجاد کنید.");
      setForm({
        sessionId: session.id,
        customerId: session.customerId || "",
        quote,
        amountPaid: "",
        paymentMethod: "1",
        revenueAccountId: revenue.id,
        paymentAccountId: cash.id,
        receivableAccountId: receivable?.id || "",
        paymentReference: ""
      });
      setModal("gameClose");
    } catch (closeError) {
      setError(formatApiError(closeError));
    } finally {
      setSaving(false);
    }
  };

  const heading = navigation.find(item => item[0] === page)?.[2] || "نمای کلی";
  const syncLabel = syncStatus === "offline" ? "اینترنت قطع است" : syncStatus === "syncing" ? "در حال همگام‌سازی" : syncStatus === "synced" ? "همگام‌سازی انجام شد" : syncStatus === "conflict" ? "عملیات نیازمند بررسی" : syncStatus === "error" ? "خطا در همگام‌سازی" : "آماده همگام‌سازی";
  const syncClass = syncStatus === "offline" ? "offline" : syncStatus === "error" || syncStatus === "conflict" ? "error" : syncStatus === "syncing" ? "syncing" : "ready";
  const totalSales = useMemo(() => sales.reduce((sum, order) => sum + Number(order.total || 0), 0), [sales]);
  const assetAccounts = accounts.filter(x => x.type === "Asset");
  const revenueAccounts = accounts.filter(x => x.type === "Revenue");
  const expenseAccounts = accounts.filter(x => x.type === "Expense");
  const hasTrackedProductsInSale = (form.lines || []).some(line => products.some(product => product.id === line.productId && product.tracksInventory));

  return <div className="mg-server-shell">
    <aside className="mg-server-sidebar">
      <div className="mg-server-brand"><div className="mg-brand-mark">MG</div><div><b>MG BUSINESS</b><small>LIVE WORKSPACE</small></div></div>
      <div className="mg-server-tenant"><span className="mg-server-dot" /><div><b>{tenant.name}</b><small>{branch.name}</small></div></div>
      <nav>{visibleNavigation.map(([id, icon, title]) => <button key={id} className={page === id ? "active" : ""} onClick={() => setPage(id)}><i>{icon}</i>{title}</button>)}</nav>
      <div className="mg-server-sidebar-bottom">
        <button onClick={onSwitchContext}>تغییر کسب‌وکار / شعبه</button>
        <button onClick={onLogout}>خروج از حساب</button>
      </div>
    </aside>

    <main className="mg-server-main">
      <header className="mg-server-topbar">
        <div><small>MG BUSINESS PLATFORM / {tenant.slug}</small><h1>{heading}</h1></div>
        <div className={`mg-server-status ${syncClass}`} title="وضعیت صف عملیات محلی"><i /> {syncLabel} · صف ارسال: {count(pendingOperations)} · دریافتیِ بررسی‌نشده: {count(incomingOperations)} · تعارض‌های باز: {count(openSyncConflicts)}</div>
      </header>

      <section className="mg-server-content">
        {error && <div className="mg-server-error" role="alert">{error}</div>}
        {loading && <div className="mg-server-loading">در حال دریافت اطلاعات از سرور...</div>}
        {syncStatus === "offline" && <div className="mg-server-offline-warning" role="status"><b>اتصال اینترنت قطع است.</b> پوسته برنامه و زیرساخت صف محلی موجود است، اما هنوز به فرم‌های فروش، انبار و حسابداری متصل نشده است؛ تا تکمیل این بخش، ثبت عملیات مالی را آفلاین انجام ندهید.</div>}
        {disconnectedDevices.length > 0 && <div className="mg-server-device-alert" role="alert"><b>هشدار قطع ارتباط دستگاه‌ها</b><p>{count(disconnectedDevices.length)} دستگاه در ۹۰ ثانیه اخیر heartbeat ارسال نکرده‌اند.</p><ul>{disconnectedDevices.map(device => <li key={device.deviceId}>{device.deviceName} — آخرین ارتباط: {dateTime(device.lastSeenAtUtc)}</li>)}</ul></div>}

        {page === "syncConflicts" && <section className="mg-server-panel">
          <div className="mg-server-panel-head"><div><h2>تعارض‌ها و عملیات نیازمند بررسی</h2><p>{count(syncConflicts.length)} مورد باز · ترتیب پردازش تا تعیین تکلیف هر مورد متوقف می‌ماند</p></div><button className="mg-secondary-btn" onClick={loadData} disabled={loading}>↻ بروزرسانی</button></div>
          {syncConflicts.length === 0
            ? <div className="mg-server-empty">در این شعبه تعارض بازی ثبت نشده است.</div>
            : <div className="mg-server-table-wrap"><table><thead><tr><th>شماره توالی</th><th>نوع عملیات</th><th>نتیجه بررسی</th><th>علت</th><th>زمان ثبت</th><th>جزئیات</th></tr></thead><tbody>{syncConflicts.map(conflict => <tr key={conflict.sequence}>
              <td>{count(conflict.sequence)}</td><td dir="ltr">{conflict.operationType || "—"}</td><td>{conflict.disposition === "conflict" ? "تعارض داده" : "بررسی دستی"}</td><td>{conflict.reason}</td><td>{dateTime(conflict.createdAtUtc)}</td><td><details><summary>مشاهده داده</summary><pre className="mg-sync-conflict-details">{JSON.stringify({ operationId: conflict.operationId, branchId: conflict.branchId, details: conflict.details }, null, 2)}</pre></details></td>
            </tr>)}</tbody></table></div>}
          <p className="mg-server-modal-hint">برای جلوگیری از تغییر ناخواسته اطلاعات مالی و موجودی، این صفحه فقط جزئیات را نمایش می‌دهد؛ حل تعارض‌های حساس باید با سیاست تطبیق و تأیید صریح انجام شود.</p>
        </section>}

        {page === "devices" && <section className="mg-server-panel">
          <div className="mg-server-panel-head"><div><h2>وضعیت اتصال دستگاه‌ها</h2><p>آستانه هشدار: ۹۰ ثانیه بدون heartbeat · آخرین بررسی هر ۳۰ ثانیه</p></div><span className="mg-server-device-count">{count(deviceStatuses.length)} دستگاه ثبت‌شده</span></div>
          {deviceStatuses.length === 0
            ? <div className="mg-server-empty">هنوز دستگاهی به این کسب‌وکار متصل نشده است.</div>
            : <div className="mg-server-table-wrap"><table className="mg-server-table"><thead><tr><th>دستگاه</th><th>پلتفرم</th><th>نسخه</th><th>آخرین ارتباط</th><th>آخرین شماره همگام‌سازی</th><th>عملیات در انتظار دریافت</th><th>وضعیت</th></tr></thead><tbody>{deviceStatuses.map(device => <tr key={device.deviceId}><td>{device.deviceName}</td><td>{device.platform}</td><td>{device.clientVersion}</td><td>{dateTime(device.lastSeenAtUtc)}</td><td>{count(device.lastAcknowledgedSequence || 0)}</td><td>{count(device.pendingOperationCount || 0)}</td><td><span className={device.isOnline ? "mg-device-online" : "mg-device-offline"}>{device.isOnline ? "متصل" : "قطع ارتباط"}</span></td></tr>)}</tbody></table></div>}
        </section>}

        {page === "dashboard" && !loading && <>
          <div className="mg-server-stats">
            <div><small>کالاهای ثبت‌شده</small><b>{canViewProducts ? count(products.length) : "—"}</b></div>
            <div><small>مشتریان</small><b>{canViewCustomers ? count(customers.length) : "—"}</b></div>
            <div><small>تعداد فاکتور</small><b>{canViewSales ? count(sales.length) : "—"}</b></div>
            <div><small>سانس فعال گیم‌نت</small><b>{canViewGameNet ? count(activeSessions.length) : "—"}</b></div>
          </div>
          <div className="mg-server-panel"><h2>اتصال واقعی برقرار است</h2><p>این صفحه اطلاعات را از API و پایگاه داده کسب‌وکار دریافت می‌کند. صف عملیات خروجی در مرورگر پایدار است و رویدادهای دریافتی نیز تا زمان وجود پردازشگر دامنه در صندوق ورودی محلی نگهداری می‌شوند.</p>{incomingOperations > 0 && <p className="mg-server-sync-warning" role="status">{count(incomingOperations)} رویداد از دستگاه‌های دیگر دریافت شده و هنوز به مدل‌های عملیاتی این دستگاه اعمال نشده است.</p>}</div>
        </>}

        {page === "products" && <section className="mg-server-panel">
          <div className="mg-server-panel-head"><div><h2>کالاها</h2><p>{count(products.length)} رکورد از پایگاه داده</p></div>{canCreateProduct && <button className="mg-primary-btn" onClick={() => openCreate("product")}>＋ کالای جدید</button>}</div>
          <div className="mg-server-table-wrap"><table><thead><tr><th>کد کالا</th><th>نام</th><th>واحد</th><th>قیمت پیش‌فرض</th><th>موجودی شعبه</th><th>میانگین بهای واحد</th><th>ارزش موجودی</th><th>نوع موجودی</th><th>وضعیت</th></tr></thead>
            <tbody>{products.map(product => <tr key={product.id}><td>{product.sku}</td><td>{product.name}</td><td>{product.baseUnit}</td><td>{money(product.defaultSalePrice)}</td><td>{product.tracksInventory ? count(stockRows.find(row => row.productId === product.id && row.branchId === branch.id)?.quantityOnHand || 0) : "—"}</td><td>{product.tracksInventory ? money(stockRows.find(row => row.productId === product.id && row.branchId === branch.id)?.averageUnitCost || 0) : "—"}</td><td>{product.tracksInventory ? money(stockRows.find(row => row.productId === product.id && row.branchId === branch.id)?.inventoryValue || 0) : "—"}</td><td>{product.tracksInventory ? "انبارگردانی‌شونده" : "خدماتی"}</td><td>{product.isActive ? "فعال" : "غیرفعال"}</td></tr>)}</tbody>
          </table>{!products.length && <div className="mg-server-empty">هنوز کالایی ثبت نشده است.</div>}</div>
        </section>}

        {page === "customers" && <section className="mg-server-panel">
          <div className="mg-server-panel-head"><div><h2>مشتریان</h2><p>{count(customers.length)} رکورد از پایگاه داده</p></div>{canCreateCustomer && <button className="mg-primary-btn" onClick={() => openCreate("customer")}>＋ مشتری جدید</button>}</div>
          <div className="mg-server-table-wrap"><table><thead><tr><th>کد</th><th>نام مشتری</th><th>شماره تماس</th><th>ایمیل</th><th>وضعیت</th><th>عملیات</th></tr></thead>
            <tbody>{customers.map(customer => <tr key={customer.id}><td>{customer.code}</td><td>{customer.name}</td><td>{customer.phone || "—"}</td><td>{customer.email || "—"}</td><td>{customer.syncStatus === "pending" ? "در انتظار همگام‌سازی" : customer.isActive ? "فعال" : "غیرفعال"}</td><td>{canEditCustomer && <button className="mg-mini-btn" disabled={saving || customer.syncStatus === "pending" || !Number.isSafeInteger(customer.version) || customer.version < 1} onClick={() => openEditCustomer(customer)}>ویرایش</button>}</td></tr>)}</tbody>
          </table>{!customers.length && <div className="mg-server-empty">هنوز مشتری ثبت نشده است.</div>}</div>
        </section>}

        {page === "sales" && <section className="mg-server-panel">
          <div className="mg-server-panel-head"><div><h2>فروش‌ها</h2><p>{count(sales.length)} فاکتور اخیر</p></div><div className="mg-server-panel-actions">{canCreateSales && <button className="mg-primary-btn" onClick={openSale}>＋ فروش جدید</button>}<button className="mg-secondary-btn" onClick={loadData}>↻ بروزرسانی</button></div></div>
          <div className="mg-server-table-wrap"><table><thead><tr><th>شماره فاکتور</th><th>مبلغ کل</th><th>پرداخت‌شده</th><th>مانده</th><th>وضعیت</th><th>زمان ثبت</th></tr></thead>
            <tbody>{sales.map(order => <tr key={order.id}><td>{order.number}</td><td>{money(order.total)}</td><td>{money(order.paidAmount)}</td><td>{money(order.balanceDue)}</td><td>{order.status}</td><td>{dateTime(order.createdAtUtc)}</td></tr>)}</tbody>
          </table>{!sales.length && <div className="mg-server-empty">هنوز فاکتوری ثبت نشده است.</div>}</div>
        </section>}

        {page === "accounts" && <section className="mg-server-panel">
          <div className="mg-server-panel-head"><div><h2>سرفصل‌های حسابداری</h2><p>{count(accounts.length)} حساب فعال</p></div><div className="mg-server-panel-actions">{canCreateAccount && <><button className="mg-secondary-btn" disabled={saving} onClick={initializeDefaultChart}>ایجاد سرفصل‌های پایه</button><button className="mg-primary-btn" onClick={() => openCreate("account")}>＋ حساب جدید</button></>}</div></div>
          <div className="mg-server-table-wrap"><table><thead><tr><th>کد حساب</th><th>عنوان</th><th>نوع حساب</th><th>وضعیت</th></tr></thead>
            <tbody>{accounts.map(account => <tr key={account.id}><td>{account.code}</td><td>{account.name}</td><td>{accountTypeName[account.type] || account.type}</td><td>{account.isActive ? "فعال" : "غیرفعال"}</td></tr>)}</tbody>
          </table>{!accounts.length && <div className="mg-server-empty">حسابی ثبت نشده است. مدیر کسب‌وکار می‌تواند سرفصل‌های پایه را ایجاد کند.</div>}</div>
          {canViewAccountingReports && trialBalance && <div className="mg-server-trial-balance">
            <div className="mg-server-panel-head"><div><h3>تراز آزمایشی</h3><p>{trialBalance.isBalanced ? "جمع بدهکار و بستانکار برابر است." : "اختلاف در تراز آزمایشی وجود دارد."}</p></div><span className={trialBalance.isBalanced ? "mg-balance-ok" : "mg-balance-warning"}>{trialBalance.isBalanced ? "متوازن" : "نامتوازن"}</span></div>
            <div className="mg-server-table-wrap"><table><thead><tr><th>کد</th><th>حساب</th><th>بدهکار</th><th>بستانکار</th><th>مانده بدهکار</th><th>مانده بستانکار</th></tr></thead><tbody>
              {(trialBalance.accounts || []).map(account => <tr key={account.id}><td>{account.code}</td><td>{account.name}</td><td>{money(account.debit)}</td><td>{money(account.credit)}</td><td>{money(account.netDebit)}</td><td>{money(account.netCredit)}</td></tr>)}
            </tbody><tfoot><tr><th colSpan="2">جمع کل</th><th>{money(trialBalance.totalDebit)}</th><th>{money(trialBalance.totalCredit)}</th><th colSpan="2"></th></tr></tfoot></table></div>
          </div>}
        </section>}

        {page === "cafe" && <section className="mg-server-panel">
          <div className="mg-server-panel-head"><div><h2>صف آشپزخانه</h2><p>{count(kitchenTickets.length)} سفارش</p></div><button className="mg-secondary-btn" onClick={loadData}>↻ بروزرسانی</button></div>
          <div className="mg-server-table-wrap"><table><thead><tr><th>فاکتور</th><th>نوع سرویس</th><th>میز</th><th>یادداشت</th><th>وضعیت</th><th>ثبت</th><th>عملیات</th></tr></thead>
            <tbody>{kitchenTickets.map(ticket => <tr key={ticket.id}>
              <td>{ticket.salesOrderId?.slice(0, 8) || "—"}</td><td>{serviceType[ticket.serviceType] || ticket.serviceType}</td><td>{ticket.tableLabel || "—"}</td><td>{ticket.notes || "—"}</td><td>{kitchenStatus[ticket.status] || ticket.status}</td><td>{dateTime(ticket.createdAtUtc)}</td>
              <td>{canUpdateKitchen && <div className="mg-server-row-actions">{(kitchenActions[ticket.status] || []).map(([label, status]) => <button key={status} className="mg-mini-btn" disabled={saving} onClick={() => updateKitchenStatus(ticket.id, status)}>{label}</button>)}</div>}</td>
            </tr>)}</tbody>
          </table>{!kitchenTickets.length && <div className="mg-server-empty">سفارشی در صف آشپزخانه نیست.</div>}</div>
        </section>}

        {page === "gamenet" && <section className="mg-server-panel">
          <div className="mg-server-panel-head"><div><h2>دستگاه‌ها و سانس‌های فعال</h2><p>{count(activeSessions.length)} سانس در حال اجرا</p></div><div className="mg-server-panel-actions">{canStartGame && <button className="mg-primary-btn" onClick={openCreateDevice}>＋ افزودن دستگاه</button>}<button className="mg-secondary-btn" onClick={loadData}>↻ بروزرسانی</button></div></div>
          <div className="mg-server-table-wrap"><table><thead><tr><th>کد</th><th>دستگاه</th><th>نرخ ساعتی</th><th>بازه محاسبه</th><th>وضعیت</th><th>عملیات</th></tr></thead>
            <tbody>{devices.map(device => <tr key={device.id}><td>{device.code}</td><td>{device.name}</td><td>{money(device.hourlyRate)}</td><td>{count(device.billingIntervalMinutes)} دقیقه</td><td>{device.hasActiveSession ? "در حال استفاده" : "آزاد"}</td><td>{canStartGame && !device.hasActiveSession && <button className="mg-mini-btn" disabled={saving} onClick={() => openStartGame(device.id)}>شروع سانس</button>}</td></tr>)}</tbody>
          </table>{!devices.length && <div className="mg-server-empty">دستگاهی برای این شعبه ثبت نشده است.</div>}</div>
          <div className="mg-server-active-sessions"><h3>سانس‌های در حال اجرا</h3>{activeSessions.map(session => <div key={session.id}><b>{session.deviceName}</b><span>شروع: {dateTime(session.startedAtUtc)}</span><span>نرخ: {money(session.hourlyRate)} / ساعت</span>{canStartGame && <button className="mg-mini-btn" disabled={saving} onClick={() => openCloseGame(session)}>بستن سانس و تسویه</button>}</div>)}{!activeSessions.length && <p>در حال حاضر سانس فعالی وجود ندارد.</p>}</div>
        </section>}
      </section>
      <footer className="mg-server-footer">MG BUSINESS · داده‌های زنده · {tenant.name} / {branch.name}</footer>
    </main>

    {modal && <div className="mg-server-overlay" onMouseDown={event => event.target === event.currentTarget && setModal("")}>
      <form className="mg-server-modal" onSubmit={save}>
        <div className="mg-server-modal-head"><h2>{modal === "product" ? "ثبت کالای جدید" : modal === "customer" ? "ثبت مشتری جدید" : modal === "customerEdit" ? "ویرایش مشتری" : modal === "account" ? "ثبت حساب جدید" : modal === "sale" ? "ثبت فاکتور فروش" : modal === "gameDevice" ? "ثبت دستگاه گیم‌نت" : modal === "gameStart" ? "شروع سانس گیم‌نت" : "بستن سانس و تسویه"}</h2><button type="button" onClick={() => setModal("")}>×</button></div>

        {modal === "product" && <>
          <label>کد کالا<input required value={form.sku} onChange={e => setForm({ ...form, sku: e.target.value })} /></label>
          <label>نام کالا<input required value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
          <label>واحد پایه<input required value={form.baseUnit} onChange={e => setForm({ ...form, baseUnit: e.target.value })} /></label>
          <label>قیمت پیش‌فرض (تومان)<input type="number" min="0" step="0.01" value={form.defaultSalePrice} onChange={e => setForm({ ...form, defaultSalePrice: e.target.value })} /></label>
          <label className="mg-server-checkbox"><input type="checkbox" checked={form.tracksInventory} onChange={e => setForm({ ...form, tracksInventory: e.target.checked })} /> این کالا موجودی انبار دارد</label>
        </>}

        {(modal === "customer" || modal === "customerEdit") && <>
          <label>کد مشتری<input required readOnly={modal === "customerEdit"} value={form.code} onChange={e => setForm({ ...form, code: e.target.value })} /></label>
          <label>نام مشتری<input required value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
          <label>شماره تماس<input value={form.phone} onChange={e => setForm({ ...form, phone: e.target.value })} /></label>
          <label>ایمیل<input type="email" value={form.email} onChange={e => setForm({ ...form, email: e.target.value })} /></label>
          <label>نشانی<input value={form.address} onChange={e => setForm({ ...form, address: e.target.value })} /></label>
          <label>یادداشت<input value={form.notes} onChange={e => setForm({ ...form, notes: e.target.value })} /></label>
        </>}

        {modal === "account" && <>
          <label>کد حساب<input required value={form.code} onChange={e => setForm({ ...form, code: e.target.value })} /></label>
          <label>عنوان حساب<input required value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
          <label>نوع حساب<select required value={form.type} onChange={e => setForm({ ...form, type: e.target.value })}><option value="1">دارایی</option><option value="2">بدهی</option><option value="3">حقوق مالکانه</option><option value="4">درآمد</option><option value="5">هزینه</option></select></label>
        </>}

        {modal === "gameDevice" && <>
          <label>خدمت متصل به دستگاه<select required value={form.serviceProductId} onChange={e => setForm({ ...form, serviceProductId: e.target.value })}>{products.filter(product => product.isActive && !product.tracksInventory).map(product => <option key={product.id} value={product.id}>{product.name} — {product.sku}</option>)}</select></label>
          <label>کد دستگاه<input required value={form.code} onChange={e => setForm({ ...form, code: e.target.value })} /></label>
          <label>نام دستگاه<input required value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
          <label>نرخ ساعتی (تومان)<input required type="number" min="0.01" step="0.01" value={form.hourlyRate} onChange={e => setForm({ ...form, hourlyRate: e.target.value })} /></label>
          <label>بازه محاسبه (دقیقه)<input required type="number" min="1" step="1" value={form.billingIntervalMinutes} onChange={e => setForm({ ...form, billingIntervalMinutes: e.target.value })} /></label>
        </>}

        {modal === "gameStart" && <>
          <p className="mg-server-modal-hint">دستگاه انتخاب‌شده آماده شروع سانس است. اتصال به مشتری اختیاری است اما برای تسویه نسیه بعداً الزامی خواهد بود.</p>
          <label>مشتری<select value={form.customerId} onChange={e => setForm({ ...form, customerId: e.target.value })}><option value="">سانس آزاد / بدون مشتری</option>{customers.map(customer => <option key={customer.id} value={customer.id}>{customer.name} — {customer.code}</option>)}</select></label>
        </>}

        {modal === "gameClose" && <>
          <div className="mg-game-quote">
            <span>زمان محاسبه: {dateTime(form.quote?.quotedAtUtc)}</span>
            <b>{count(form.quote?.billedMinutes)} دقیقه</b>
            <strong>{money(form.quote?.totalAmount)}</strong>
          </div>
          <p className="mg-server-modal-hint">مبلغ خالی یعنی تسویه کامل؛ پیش از ثبت، مبلغ بر اساس زمان واقعی سرور دوباره محاسبه می‌شود.</p>
          <label>مبلغ پرداختی (خالی = تسویه کامل)<input type="number" min="0" step="0.01" value={form.amountPaid} onChange={e => setForm({ ...form, amountPaid: e.target.value })} /></label>
          <label>روش پرداخت<select value={form.paymentMethod} onChange={e => setForm({ ...form, paymentMethod: e.target.value })}><option value="1">نقدی</option><option value="2">کارت‌خوان</option><option value="3">انتقال بانکی</option><option value="4">کیف پول</option></select></label>
          <label>حساب درآمد<select required value={form.revenueAccountId} onChange={e => setForm({ ...form, revenueAccountId: e.target.value })}>{accounts.filter(account => account.type === "Revenue").map(account => <option key={account.id} value={account.id}>{account.code} — {account.name}</option>)}</select></label>
          {(form.amountPaid === "" || Number(form.amountPaid) > 0) && <label>حساب دریافت وجه<select required value={form.paymentAccountId} onChange={e => setForm({ ...form, paymentAccountId: e.target.value })}>{accounts.filter(account => account.type === "Asset").map(account => <option key={account.id} value={account.id}>{account.code} — {account.name}</option>)}</select></label>}
          <label>حساب دریافتنی (برای تسویه ناقص)<select value={form.receivableAccountId} onChange={e => setForm({ ...form, receivableAccountId: e.target.value })}><option value="">انتخاب کنید</option>{accounts.filter(account => account.type === "Asset").map(account => <option key={account.id} value={account.id}>{account.code} — {account.name}</option>)}</select></label>
          <label>شناسه پرداخت / توضیحات<input value={form.paymentReference} onChange={e => setForm({ ...form, paymentReference: e.target.value })} /></label>
        </>}

        {modal === "sale" && <>
          <label>مشتری (برای فروش نسیه الزامی است)<select value={form.customerId} onChange={e => setForm({ ...form, customerId: e.target.value })}><option value="">فروش نقدی بدون مشتری</option>{customers.map(customer => <option key={customer.id} value={customer.id}>{customer.name} — {customer.code}</option>)}</select></label>
          <div className="mg-sale-lines">
            <div className="mg-sale-lines-head"><b>اقلام فاکتور</b><button type="button" className="mg-mini-btn" onClick={() => setForm({ ...form, lines: [...form.lines, { productId: products.find(product => product.isActive)?.id || "", quantity: "1", unitPrice: String(products.find(product => product.isActive)?.defaultSalePrice || 0), discount: "0" }] })}>＋ افزودن قلم</button></div>
            {form.lines.map((line, index) => <div className="mg-sale-line" key={index}>
              <select aria-label="کالا" required value={line.productId} onChange={e => updateSaleLine(index, "productId", e.target.value)}><option value="">انتخاب کالا</option>{products.filter(product => product.isActive).map(product => <option key={product.id} value={product.id}>{product.name} · {product.sku}</option>)}</select>
              <input aria-label="تعداد" type="number" min="0.001" step="0.001" required value={line.quantity} onChange={e => updateSaleLine(index, "quantity", e.target.value)} />
              <input aria-label="قیمت واحد" type="number" min="0" step="0.01" required value={line.unitPrice} onChange={e => updateSaleLine(index, "unitPrice", e.target.value)} />
              <input aria-label="تخفیف قلم" type="number" min="0" step="0.01" value={line.discount} onChange={e => updateSaleLine(index, "discount", e.target.value)} />
              {form.lines.length > 1 && <button type="button" className="mg-remove-line" onClick={() => setForm({ ...form, lines: form.lines.filter((_, i) => i !== index) })}>×</button>}
            </div>)}
            <div className="mg-sale-line-labels"><span>کالا</span><span>تعداد</span><span>قیمت واحد</span><span>تخفیف</span><span /></div>
          </div>
          <label>تخفیف کل فاکتور (تومان)<input type="number" min="0" step="0.01" value={form.discount} onChange={e => setForm({ ...form, discount: e.target.value })} /></label>
          <div className="mg-sale-summary"><span>جمع اقلام: <b>{money(saleSubtotal)}</b></span><span>مبلغ نهایی: <b>{money(saleTotal)}</b></span><span>مانده: <b>{money(balanceDue)}</b></span></div>
          <label>مبلغ پرداختی (خالی = تسویه کامل)<input type="number" min="0" max={saleTotal} step="0.01" value={form.amountPaid} onChange={e => setForm({ ...form, amountPaid: e.target.value })} /></label>
          <label>روش پرداخت<select value={form.paymentMethod} onChange={e => setForm({ ...form, paymentMethod: e.target.value })}><option value="1">نقدی</option><option value="2">کارت‌خوان</option><option value="3">انتقال بانکی</option><option value="4">کیف پول</option></select></label>
          <label>حساب درآمد<select required value={form.revenueAccountId} onChange={e => setForm({ ...form, revenueAccountId: e.target.value })}>{revenueAccounts.map(account => <option key={account.id} value={account.id}>{account.code} — {account.name}</option>)}</select></label>
          {paidAmount > 0 && <label>حساب دریافت وجه<select required value={form.paymentAccountId} onChange={e => setForm({ ...form, paymentAccountId: e.target.value })}>{assetAccounts.map(account => <option key={account.id} value={account.id}>{account.code} — {account.name}</option>)}</select></label>}
          {balanceDue > 0 && <label>حساب دریافتنی<select required value={form.receivableAccountId} onChange={e => setForm({ ...form, receivableAccountId: e.target.value })}>{assetAccounts.map(account => <option key={account.id} value={account.id}>{account.code} — {account.name}</option>)}</select></label>}
          {hasTrackedProductsInSale && <><label>حساب موجودی کالا<select value={form.inventoryAccountId} onChange={e => setForm({ ...form, inventoryAccountId: e.target.value })}>{assetAccounts.map(account => <option key={account.id} value={account.id}>{account.code} — {account.name}</option>)}</select></label><label>حساب بهای تمام‌شده<select value={form.costOfGoodsSoldAccountId} onChange={e => setForm({ ...form, costOfGoodsSoldAccountId: e.target.value })}>{expenseAccounts.map(account => <option key={account.id} value={account.id}>{account.code} — {account.name}</option>)}</select></label></>}
          <label>شناسه پرداخت / توضیحات<input value={form.paymentReference} onChange={e => setForm({ ...form, paymentReference: e.target.value })} /></label>
          {canCreateKitchen && <div className="mg-sale-lines"><label className="mg-server-checkbox"><input type="checkbox" checked={Boolean(form.sendToKitchen)} onChange={e => setForm({ ...form, sendToKitchen: e.target.checked })} /> ارسال این فاکتور به صف آشپزخانه</label>
            {form.sendToKitchen && <><label>نوع سرویس<select value={form.serviceType || "1"} onChange={e => setForm({ ...form, serviceType: e.target.value })}><option value="1">سرو داخل</option><option value="2">بیرون‌بر</option><option value="3">ارسال</option></select></label><label>شماره میز / محل تحویل<input value={form.tableLabel || ""} onChange={e => setForm({ ...form, tableLabel: e.target.value })} /></label><label>یادداشت آشپزخانه<input value={form.kitchenNotes || ""} onChange={e => setForm({ ...form, kitchenNotes: e.target.value })} /></label></>}
          </div>}
        </>}


        <div className="mg-server-modal-actions"><button type="button" className="mg-secondary-btn" onClick={() => setModal("")}>انصراف</button><button className="mg-primary-btn" disabled={saving}>{saving ? "در حال ذخیره..." : modal === "sale" ? "ثبت فروش و سند حسابداری" : modal === "gameDevice" ? "ثبت دستگاه" : modal === "gameStart" ? "شروع سانس" : modal === "gameClose" ? "بستن سانس و ثبت تسویه" : "ذخیره در سرور"}</button></div>
      </form>
    </div>}
  </div>;
}

export default ServerWorkspace;

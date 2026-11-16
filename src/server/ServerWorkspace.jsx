import React, { useCallback, useEffect, useMemo, useState } from "react";
import { formatApiError } from "../api/client.js";

const money = value => new Intl.NumberFormat("fa-IR").format(Number(value || 0)) + " تومان";
const count = value => new Intl.NumberFormat("fa-IR").format(Number(value || 0));

const navigation = [
  ["dashboard", "◫", "نمای کلی"],
  ["products", "▦", "کالا و انبار"],
  ["customers", "♙", "مشتریان"],
  ["sales", "▣", "فروش‌ها"]
];

function ServerWorkspace({ client, tenant, branch, onLogout, onSwitchContext }) {
  const [page, setPage] = useState("dashboard");
  const [products, setProducts] = useState([]);
  const [customers, setCustomers] = useState([]);
  const [sales, setSales] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [modal, setModal] = useState("");
  const [form, setForm] = useState({});
  const [saving, setSaving] = useState(false);
  const canViewCustomers = ["Owner", "Manager", "Accountant", "Cashier", "Waiter", "Viewer"].includes(tenant.role);
  const canViewSales = ["Owner", "Manager", "Accountant", "Cashier", "Viewer"].includes(tenant.role);
  const canCreateProduct = ["Owner", "Manager", "InventoryManager"].includes(tenant.role);
  const canCreateCustomer = ["Owner", "Manager", "Cashier"].includes(tenant.role);
  const visibleNavigation = navigation.filter(([id]) =>
    id !== "customers" || canViewCustomers
  ).filter(([id]) => id !== "sales" || canViewSales);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      if (page === "products") setProducts(await client.get("/api/inventory/products"));
      else if (page === "customers") setCustomers(await client.get("/api/customers"));
      else if (page === "sales") setSales(await client.get("/api/sales/orders"));
      else {
        const requests = [client.get("/api/inventory/products")];
        if (canViewCustomers) requests.push(client.get("/api/customers"));
        if (canViewSales) requests.push(client.get("/api/sales/orders"));
        const results = await Promise.all(requests);
        setProducts(results[0] || []);
        setCustomers(canViewCustomers ? results[1] || [] : []);
        setSales(canViewSales ? results[canViewCustomers ? 2 : 1] || [] : []);
      }
    } catch (loadError) {
      setError(formatApiError(loadError));
    } finally {
      setLoading(false);
    }
  }, [client, page, canViewCustomers, canViewSales]);

  useEffect(() => { loadData(); }, [loadData]);

  const openCreate = type => {
    setForm(type === "product"
      ? { sku: "", name: "", baseUnit: "عدد", tracksInventory: true, defaultSalePrice: "" }
      : { code: "", name: "", phone: "", email: "", address: "", notes: "" });
    setModal(type);
    setError("");
  };

  const save = async event => {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      if (modal === "product") {
        await client.post("/api/inventory/products", {
          sku: form.sku.trim(),
          name: form.name.trim(),
          baseUnit: form.baseUnit.trim(),
          tracksInventory: Boolean(form.tracksInventory),
          defaultSalePrice: Number(form.defaultSalePrice || 0)
        });
      } else {
        await client.post("/api/customers", {
          code: form.code.trim(),
          name: form.name.trim(),
          phone: form.phone.trim() || null,
          email: form.email.trim() || null,
          address: form.address.trim() || null,
          notes: form.notes.trim() || null
        });
      }
      setModal("");
      await loadData();
    } catch (saveError) {
      setError(formatApiError(saveError));
    } finally {
      setSaving(false);
    }
  };

  const heading = navigation.find(item => item[0] === page)?.[2] || "نمای کلی";
  const totalSales = useMemo(() => sales.reduce((sum, order) => sum + Number(order.total || 0), 0), [sales]);

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
        <div className="mg-server-status"><i /> متصل به سرور</div>
      </header>

      <section className="mg-server-content">
        {error && <div className="mg-server-error" role="alert">{error}</div>}
        {loading && <div className="mg-server-loading">در حال دریافت اطلاعات از سرور...</div>}

        {page === "dashboard" && !loading && <>
          <div className="mg-server-stats">
            <div><small>کالاهای ثبت‌شده</small><b>{count(products.length)}</b></div>
            <div><small>مشتریان</small><b>{canViewCustomers ? count(customers.length) : "—"}</b></div>
            <div><small>تعداد فاکتور</small><b>{canViewSales ? count(sales.length) : "—"}</b></div>
            <div><small>مبلغ فروش ثبت‌شده</small><b>{canViewSales ? money(totalSales) : "—"}</b></div>
          </div>
          <div className="mg-server-panel"><h2>اتصال واقعی برقرار است</h2><p>این صفحه اطلاعات را از API و پایگاه داده کسب‌وکار دریافت می‌کند؛ برخلاف پیش‌نمایش، داده‌ها در مرورگر ذخیره نمی‌شوند.</p></div>
        </>}

        {page === "products" && <section className="mg-server-panel">
          <div className="mg-server-panel-head"><div><h2>کالاها</h2><p>{count(products.length)} رکورد از پایگاه داده</p></div>{canCreateProduct && <button className="mg-primary-btn" onClick={() => openCreate("product")}>＋ کالای جدید</button>}</div>
          <div className="mg-server-table-wrap"><table><thead><tr><th>کد کالا</th><th>نام</th><th>واحد</th><th>قیمت پیش‌فرض</th><th>نوع موجودی</th><th>وضعیت</th></tr></thead>
            <tbody>{products.map(product => <tr key={product.id}><td>{product.sku}</td><td>{product.name}</td><td>{product.baseUnit}</td><td>{money(product.defaultSalePrice)}</td><td>{product.tracksInventory ? "انبارگردانی‌شونده" : "خدماتی"}</td><td>{product.isActive ? "فعال" : "غیرفعال"}</td></tr>)}</tbody>
          </table>{!products.length && <div className="mg-server-empty">هنوز کالایی ثبت نشده است.</div>}</div>
        </section>}

        {page === "customers" && <section className="mg-server-panel">
          <div className="mg-server-panel-head"><div><h2>مشتریان</h2><p>{count(customers.length)} رکورد از پایگاه داده</p></div>{canCreateCustomer && <button className="mg-primary-btn" onClick={() => openCreate("customer")}>＋ مشتری جدید</button>}</div>
          <div className="mg-server-table-wrap"><table><thead><tr><th>کد</th><th>نام مشتری</th><th>شماره تماس</th><th>ایمیل</th><th>وضعیت</th></tr></thead>
            <tbody>{customers.map(customer => <tr key={customer.id}><td>{customer.code}</td><td>{customer.name}</td><td>{customer.phone || "—"}</td><td>{customer.email || "—"}</td><td>{customer.isActive ? "فعال" : "غیرفعال"}</td></tr>)}</tbody>
          </table>{!customers.length && <div className="mg-server-empty">هنوز مشتری ثبت نشده است.</div>}</div>
        </section>}

        {page === "sales" && <section className="mg-server-panel">
          <div className="mg-server-panel-head"><div><h2>فروش‌ها</h2><p>{count(sales.length)} فاکتور اخیر</p></div><button className="mg-secondary-btn" onClick={loadData}>↻ بروزرسانی</button></div>
          <div className="mg-server-table-wrap"><table><thead><tr><th>شماره فاکتور</th><th>مبلغ کل</th><th>پرداخت‌شده</th><th>مانده</th><th>وضعیت</th><th>زمان ثبت</th></tr></thead>
            <tbody>{sales.map(order => <tr key={order.id}><td>{order.number}</td><td>{money(order.total)}</td><td>{money(order.paidAmount)}</td><td>{money(order.balanceDue)}</td><td>{order.status}</td><td>{new Date(order.createdAtUtc).toLocaleString("fa-IR")}</td></tr>)}</tbody>
          </table>{!sales.length && <div className="mg-server-empty">هنوز فاکتوری ثبت نشده است.</div>}</div>
        </section>}
      </section>
      <footer className="mg-server-footer">MG BUSINESS · داده‌های زنده · {tenant.name} / {branch.name}</footer>
    </main>

    {modal && <div className="mg-server-overlay" onMouseDown={event => event.target === event.currentTarget && setModal("")}>
      <form className="mg-server-modal" onSubmit={save}>
        <div className="mg-server-modal-head"><h2>{modal === "product" ? "ثبت کالای جدید" : "ثبت مشتری جدید"}</h2><button type="button" onClick={() => setModal("")}>×</button></div>
        {modal === "product" ? <>
          <label>کد کالا<input required value={form.sku} onChange={e => setForm({ ...form, sku: e.target.value })} /></label>
          <label>نام کالا<input required value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
          <label>واحد پایه<input required value={form.baseUnit} onChange={e => setForm({ ...form, baseUnit: e.target.value })} /></label>
          <label>قیمت پیش‌فرض (تومان)<input type="number" min="0" step="0.01" value={form.defaultSalePrice} onChange={e => setForm({ ...form, defaultSalePrice: e.target.value })} /></label>
          <label className="mg-server-checkbox"><input type="checkbox" checked={form.tracksInventory} onChange={e => setForm({ ...form, tracksInventory: e.target.checked })} /> این کالا موجودی انبار دارد</label>
        </> : <>
          <label>کد مشتری<input required value={form.code} onChange={e => setForm({ ...form, code: e.target.value })} /></label>
          <label>نام مشتری<input required value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
          <label>شماره تماس<input value={form.phone} onChange={e => setForm({ ...form, phone: e.target.value })} /></label>
          <label>ایمیل<input type="email" value={form.email} onChange={e => setForm({ ...form, email: e.target.value })} /></label>
          <label>نشانی<input value={form.address} onChange={e => setForm({ ...form, address: e.target.value })} /></label>
        </>}
        <div className="mg-server-modal-actions"><button type="button" className="mg-secondary-btn" onClick={() => setModal("")}>انصراف</button><button className="mg-primary-btn" disabled={saving}>{saving ? "در حال ذخیره..." : "ذخیره در سرور"}</button></div>
      </form>
    </div>}
  </div>;
}

export default ServerWorkspace;

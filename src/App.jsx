import React from "react";
import AuthShell from "./auth/AuthShell.jsx";

function ApiConfigurationRequired() {
  return (
    <main style={{
      minHeight: "100vh",
      display: "grid",
      placeItems: "center",
      padding: 24,
      background: "#101a2b",
      color: "#eaf0f7",
      fontFamily: "Tahoma, sans-serif",
      direction: "rtl"
    }}>
      <section style={{
        width: "min(100%, 520px)",
        padding: 32,
        border: "1px solid #31445f",
        borderRadius: 18,
        background: "#17263b",
        textAlign: "center",
        boxShadow: "0 20px 60px #0005"
      }}>
        <div style={{ fontSize: 30, fontWeight: 900, color: "#e8b45c", letterSpacing: 2 }}>MG</div>
        <h1 style={{ fontSize: 22, margin: "18px 0 10px" }}>اتصال سرور MG Business تنظیم نشده است</h1>
        <p style={{ color: "#aebdd0", fontSize: 13, lineHeight: 2 }}>
          برای جلوگیری از نمایش اطلاعات آزمایشی، محیط عملیاتی بدون نشانی API اجرا نمی‌شود.
          مقدار VITE_API_BASE_URL را در تنظیمات انتشار وارد کنید و سپس برنامه را دوباره بسازید.
        </p>
        <small style={{ display: "block", marginTop: 18, color: "#7f91a8" }}>
          Customer workspace · API configuration required
        </small>
      </section>
    </main>
  );
}

export default function App() {
  const apiBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim();
  return apiBaseUrl
    ? <AuthShell apiBaseUrl={apiBaseUrl} />
    : <ApiConfigurationRequired />;
}

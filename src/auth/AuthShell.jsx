import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { formatApiError, MgApiClient } from "../api/client.js";
import ServerWorkspace from "../server/ServerWorkspace.jsx";
import { LocalOperationStore } from "../sync/local-operation-store.js";
import { SyncAgent } from "../sync/sync-agent.js";
import { IncomingOperationProcessor } from "../sync/incoming-operation-processor.js";
import { classifyIncomingOperation } from "../sync/conflict-policy.js";
import "../server/server.css";

const SESSION_KEY = "mg-business-session";

function readSavedSession() {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function AuthShell({ apiBaseUrl }) {
  const clientRef = useRef(null);
  if (!clientRef.current) clientRef.current = new MgApiClient(apiBaseUrl);
  const client = clientRef.current;
  const operationStoreRef = useRef(new LocalOperationStore());
  const syncAgentRef = useRef(null);
  if (!syncAgentRef.current) syncAgentRef.current = new SyncAgent(client, operationStoreRef.current);
  const incomingProcessorRef = useRef(null);
  if (!incomingProcessorRef.current) incomingProcessorRef.current = new IncomingOperationProcessor({
    store: operationStoreRef.current,
    adapter: operationStoreRef.current,
    classify: classifyIncomingOperation
  });

  const [session, setSessionState] = useState(readSavedSession);
  const [stage, setStage] = useState("restoring");
  const [authMode, setAuthMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenantName, setTenantName] = useState("");
  const [tenantSlug, setTenantSlug] = useState("");
  const [firstBranchName, setFirstBranchName] = useState("شعبه اصلی");
  const [tenants, setTenants] = useState([]);
  const [branches, setBranches] = useState([]);
  const [selectedTenant, setSelectedTenant] = useState(null);
  const [selectedBranch, setSelectedBranch] = useState(null);
  const [activeActorId, setActiveActorId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [syncStatus, setSyncStatus] = useState("idle");
  const [pendingOperations, setPendingOperations] = useState(0);
  const [incomingOperations, setIncomingOperations] = useState(0);
  const [openSyncConflicts, setOpenSyncConflicts] = useState(0);
  const [disconnectedDevices, setDisconnectedDevices] = useState([]);
  const [deviceStatuses, setDeviceStatuses] = useState([]);

  useEffect(() => {
    client.onSessionChanged = value => {
      setSessionState(value);
      if (value) sessionStorage.setItem(SESSION_KEY, JSON.stringify(value));
      else sessionStorage.removeItem(SESSION_KEY);
    };
    return () => { client.onSessionChanged = null; };
  }, [client]);

  const saveSession = useCallback(value => {
    client.setSession(value);
  }, [client]);

  const apiHost = useMemo(() => {
    try { return new URL(apiBaseUrl).host; } catch { return apiBaseUrl; }
  }, [apiBaseUrl]);

  useEffect(() => {
    if (!session && !["restoring", "login"].includes(stage)) {
      client.setTenantContext(null, null);
      setSelectedTenant(null);
      setSelectedBranch(null);
      setActiveActorId(null);
      setStage("login");
    }
  }, [session, stage, client]);

  const loadTenants = useCallback(async () => {
    const result = await client.get("/api/tenants/mine");
    setTenants(result || []);
    setSelectedTenant(null);
    setSelectedBranch(null);
    setActiveActorId(null);
    setBranches([]);
    setStage(result?.length ? "tenant" : "create-tenant");
  }, [client]);

  useEffect(() => {
    let active = true;
    const restore = async () => {
      const saved = readSavedSession();
      if (!saved?.refreshToken) {
        if (active) setStage("login");
        return;
      }
      client.setSession(saved);
      const refreshed = await client.refreshSession();
      if (!active) return;
      if (!refreshed) {
        saveSession(null);
        setStage("login");
        return;
      }
      saveSession(client.session);
      try {
        await loadTenants();
      } catch (restoreError) {
        if (active) {
          setError(formatApiError(restoreError));
          setStage("login");
        }
      }
    };
    restore();
    return () => { active = false; };
  }, [client, loadTenants, saveSession]);

  const handleLogin = async event => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const tokens = await client.login(email.trim(), password);
      saveSession(tokens);
      await loadTenants();
    } catch (loginError) {
      setError(formatApiError(loginError));
    } finally {
      setBusy(false);
    }
  };

  const handleRegister = async event => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await client.register(email.trim(), password);
      const tokens = await client.login(email.trim(), password);
      saveSession(tokens);
      await loadTenants();
    } catch (registerError) {
      setError(formatApiError(registerError));
    } finally {
      setBusy(false);
    }
  };

  const handleCreateTenant = async event => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await client.post("/api/tenants", {
        name: tenantName.trim(),
        slug: tenantSlug.trim().toLowerCase(),
        firstBranchName: firstBranchName.trim()
      });
      await loadTenants();
    } catch (createError) {
      setError(formatApiError(createError));
    } finally {
      setBusy(false);
    }
  };

  const chooseTenant = async tenant => {
    setBusy(true);
    setError("");
    try {
      client.setTenantContext(tenant.id, null);
      const result = await client.get("/api/branches/");
      setSelectedTenant(tenant);
      setActiveActorId(null);
      setBranches(result || []);
      setSelectedBranch(null);
      setStage("branch");
    } catch (branchError) {
      setError(formatApiError(branchError));
    } finally {
      setBusy(false);
    }
  };

  const chooseBranch = async branch => {
    setBusy(true);
    setError("");
    try {
      client.setTenantContext(selectedTenant.id, branch.id);
      const context = await client.get("/api/context");
      if (typeof context?.userId !== "string" || !context.userId.trim())
        throw new Error("شناسه کاربر از سرور دریافت نشد؛ دستگاه همگام‌سازی ثبت نمی‌شود.");
      setActiveActorId(context.userId);
      setSelectedBranch(branch);
      setStage("workspace");
    } catch (contextError) {
      setError(formatApiError(contextError));
      client.setTenantContext(null, null);
    } finally {
      setBusy(false);
    }
  };

  const logout = () => {
    saveSession(null);
    client.setTenantContext(null, null);
    setTenants([]);
    setBranches([]);
    setSelectedTenant(null);
    setSelectedBranch(null);
    setActiveActorId(null);
    setPassword("");
    setStage("login");
  };

  const switchContext = () => {
    client.setTenantContext(null, null);
    setSelectedTenant(null);
    setSelectedBranch(null);
    setActiveActorId(null);
    setStage("tenant");
  };

  useEffect(() => {
    if (stage !== "workspace" || !selectedTenant?.id) return undefined;
    let active = true;
    const tenantId = selectedTenant.id;

    const refreshPendingCount = async () => {
      try {
        const branchId = selectedBranch?.id || null;
        const allowTenantWide = ["Owner", "Manager"].includes(selectedTenant?.role);
        const deviceId = await operationStoreRef.current.getDeviceId(activeActorId);
        const [pending, incoming, conflicts] = await Promise.all([
          operationStoreRef.current.getPendingCount(tenantId, branchId, allowTenantWide, deviceId),
          operationStoreRef.current.getInboxCount(tenantId, branchId, allowTenantWide),
          operationStoreRef.current.getOpenConflicts(tenantId, branchId, allowTenantWide)
        ]);
        if (active) { setPendingOperations(pending); setIncomingOperations(incoming); setOpenSyncConflicts(conflicts.length); }
      } catch {
        if (active) setSyncStatus("error");
      }
    };

    let synchronizing = false;
    let syncDeviceRegistered = false;
    const synchronize = async () => {
      if (!active || synchronizing) return;
      synchronizing = true;
      try {
        if (typeof navigator !== "undefined" && !navigator.onLine) {
          if (active) setSyncStatus("offline");
          return;
        }

        if (!syncDeviceRegistered) {
          const deviceId = await operationStoreRef.current.getDeviceId(activeActorId);
          await client.post("/api/sync/heartbeat", {
            deviceId,
            deviceName: "MG Business Web",
            platform: navigator.platform || "Web",
            clientVersion: "0.1.0"
          });
          syncDeviceRegistered = true;
        }

        if (active) setSyncStatus("syncing");
        let pullResult = null;
        for (let page = 0; page < 5; page += 1) {
          pullResult = await syncAgentRef.current.pullIncoming(tenantId, { branchId: selectedBranch?.id || null, actorId: activeActorId });
          if (pullResult.status !== "more-available") break;
        }
        const replayResult = await incomingProcessorRef.current.processBatch(
          tenantId,
          100,
          selectedBranch?.id || null,
          ["Owner", "Manager"].includes(selectedTenant?.role)
        );
        const result = await syncAgentRef.current.pushPending(tenantId, {
          branchId: selectedBranch?.id || null,
          allowTenantWide: ["Owner", "Manager"].includes(selectedTenant?.role),
          actorId: activeActorId
        });
        if (active) {
          setSyncStatus(replayResult.blockedByConflict
            ? "conflict"
            : pullResult?.status === "offline" && result.status === "idle"
              ? "offline"
              : pullResult?.status === "more-available"
                ? "syncing"
                : result.status);
        }
      } catch {
        if (active) setSyncStatus("error");
      } finally {
        synchronizing = false;
        await refreshPendingCount();
      }
    };

    const handleOnline = () => { synchronize(); };
    const handleOffline = () => { setSyncStatus("offline"); refreshPendingCount(); };
    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);
    const timer = window.setInterval(synchronize, 15000);
    synchronize();

    return () => {
      active = false;
      window.clearInterval(timer);
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    };
  }, [stage, selectedTenant?.id, selectedTenant?.role, selectedBranch?.id, activeActorId]);

  useEffect(() => {
    if (stage !== "workspace" || !selectedTenant?.id) return undefined;
    let active = true;
    let heartbeatInFlight = false;
    const sendHeartbeat = async () => {
      if (!active || heartbeatInFlight || document.visibilityState === "hidden") return;
      heartbeatInFlight = true;
      try {
        const deviceId = await operationStoreRef.current.getDeviceId(activeActorId);
        await client.post("/api/sync/heartbeat", {
          deviceId,
          deviceName: "MG Business Web",
          platform: navigator.platform || "Web",
          clientVersion: "0.1.0"
        });
        if (active && ["Owner", "Manager"].includes(selectedTenant.role)) {
          const devices = await client.get("/api/sync/devices");
          if (active) {
            setDeviceStatuses(devices || []);
            setDisconnectedDevices((devices || []).filter(device => !device.isOnline));
          }
        }
      } catch {
        // A missed heartbeat is detected by the next successful manager poll.
      } finally {
        heartbeatInFlight = false;
      }
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") sendHeartbeat();
    };
    window.addEventListener("focus", sendHeartbeat);
    document.addEventListener("visibilitychange", onVisibilityChange);
    sendHeartbeat();
    const timer = window.setInterval(sendHeartbeat, 30000);
    return () => {
      active = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", sendHeartbeat);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [stage, selectedTenant?.id, selectedTenant?.role, selectedBranch?.id, activeActorId, client]);

  const workspaceContext = useMemo(() => ({
    tenant: selectedTenant,
    branch: selectedBranch,
    actorId: activeActorId
  }), [selectedTenant, selectedBranch, activeActorId]);

  if (stage === "restoring")
    return <div className="mg-auth-screen"><div className="mg-auth-card"><div className="mg-brand-mark">MG</div><h1>در حال اتصال به MG Business</h1><p>در حال بررسی نشست کاربری و ارتباط با سرور...</p></div></div>;

  if (stage === "workspace" && selectedTenant && selectedBranch)
    return <ServerWorkspace client={client} operationStore={operationStoreRef.current} actorId={activeActorId} syncStatus={syncStatus} pendingOperations={pendingOperations} incomingOperations={incomingOperations} openSyncConflicts={openSyncConflicts} disconnectedDevices={disconnectedDevices} deviceStatuses={deviceStatuses} {...workspaceContext} onLogout={logout} onSwitchContext={switchContext} />;

  return <div className="mg-auth-screen">
    <section className="mg-auth-card">
      <div className="mg-brand-mark">MG</div>
      <div className="mg-auth-eyebrow">MG BUSINESS PLATFORM</div>
      {stage === "login" && <>
        <h1>{authMode === "login" ? "ورود به حساب کاربری" : "ساخت حساب کاربری"}</h1>
        <p>برای اتصال به داده‌های واقعی کسب‌وکار، وارد حساب خود شوید.</p>
        <form onSubmit={authMode === "login" ? handleLogin : handleRegister} className="mg-auth-form">
          <label>ایمیل<input type="email" autoComplete="email" required value={email} onChange={e => setEmail(e.target.value)} placeholder="name@example.com" /></label>
          <label>رمز عبور<input type="password" autoComplete={authMode === "login" ? "current-password" : "new-password"} minLength={12} required value={password} onChange={e => setPassword(e.target.value)} placeholder="حداقل ۱۲ نویسه" /></label>
          <button className="mg-primary-btn" disabled={busy}>{busy ? "لطفاً صبر کنید..." : authMode === "login" ? "ورود امن" : "ایجاد حساب و ورود"}</button>
        </form>
        <button className="mg-link-btn" onClick={() => { setAuthMode(authMode === "login" ? "register" : "login"); setError(""); }}>
          {authMode === "login" ? "حساب ندارید؟ ثبت‌نام کنید" : "قبلاً حساب ساخته‌اید؟ وارد شوید"}
        </button>
      </>}

      {stage === "create-tenant" && <>
        <h1>ساخت اولین کسب‌وکار</h1>
        <p>حساب شما آماده است. مشخصات مجموعه و شعبه اصلی را ثبت کنید.</p>
        <form onSubmit={handleCreateTenant} className="mg-auth-form">
          <label>نام کسب‌وکار<input required value={tenantName} onChange={e => setTenantName(e.target.value)} placeholder="مثلاً کافه MG" /></label>
          <label>شناسه یکتا (انگلیسی)<input required pattern="[a-zA-Z0-9-]+" value={tenantSlug} onChange={e => setTenantSlug(e.target.value)} placeholder="mg-cafe" /></label>
          <label>نام شعبه اصلی<input required value={firstBranchName} onChange={e => setFirstBranchName(e.target.value)} /></label>
          <button className="mg-primary-btn" disabled={busy}>{busy ? "در حال ساخت..." : "ساخت کسب‌وکار"}</button>
        </form>
      </>}

      {stage === "tenant" && <>
        <h1>انتخاب کسب‌وکار</h1>
        <p>مشخص کنید وارد کدام مجموعه می‌شوید.</p>
        <div className="mg-choice-list">{tenants.map(tenant => <button key={tenant.id} onClick={() => chooseTenant(tenant)} disabled={busy}>
          <span className="mg-choice-icon">▦</span><span><b>{tenant.name}</b><small>{tenant.slug} · {tenant.role}</small></span><strong>←</strong>
        </button>)}</div>
        <button className="mg-link-btn" onClick={() => setStage("create-tenant")}>＋ ساخت کسب‌وکار جدید</button>
      </>}

      {stage === "branch" && <>
        <h1>انتخاب شعبه</h1>
        <p>{selectedTenant?.name} — شعبه موردنظر را انتخاب کنید.</p>
        <div className="mg-choice-list">{branches.map(branch => <button key={branch.id} onClick={() => chooseBranch(branch)} disabled={busy}>
          <span className="mg-choice-icon">⌂</span><span><b>{branch.name}</b><small>{branch.code}</small></span><strong>←</strong>
        </button>)}</div>
        {!branches.length && <div className="mg-inline-error">برای این کسب‌وکار شعبه فعالی در دسترس نیست.</div>}
        <button className="mg-link-btn" onClick={() => setStage("tenant")}>بازگشت به انتخاب کسب‌وکار</button>
      </>}

      {error && <div className="mg-inline-error" role="alert">{error}</div>}
      <div className="mg-auth-footer">
        <span>اتصال امن به API</span><b>{apiHost}</b>
      </div>
    </section>
  </div>;
}

export default AuthShell;

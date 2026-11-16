export class ApiError extends Error {
  constructor(status, message, details = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

function responseMessage(payload, fallback) {
  if (payload && typeof payload === "object") {
    if (typeof payload.message === "string") return payload.message;
    if (typeof payload.title === "string") return payload.title;
    if (payload.errors && typeof payload.errors === "object") {
      return Object.values(payload.errors).flat().join(" ");
    }
  }
  return typeof payload === "string" && payload ? payload : fallback;
}

export class MgApiClient {
  constructor(baseUrl) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.session = null;
    this.onSessionChanged = null;
    this.tenantId = null;
    this.branchId = null;
  }

  setSession(session) {
    this.session = session;
    this.onSessionChanged?.(session);
  }

  setTenantContext(tenantId, branchId = null) {
    this.tenantId = tenantId || null;
    this.branchId = branchId || null;
  }

  async readPayload(response) {
    const raw = await response.text();
    if (!raw) return null;
    try { return JSON.parse(raw); } catch { return raw; }
  }

  async rawRequest(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body !== undefined && !headers.has("Content-Type"))
      headers.set("Content-Type", "application/json");
    const response = await fetch(this.baseUrl + path, {
      method: options.method || "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal
    });
    const payload = await this.readPayload(response);
    if (!response.ok)
      throw new ApiError(response.status, responseMessage(payload, `درخواست ناموفق بود (HTTP ${response.status}).`), payload);
    return payload;
  }

  async refreshSession() {
    if (!this.session?.refreshToken) return false;
    try {
      const refreshed = await this.rawRequest("/auth/refresh", {
        method: "POST",
        body: { refreshToken: this.session.refreshToken }
      });
      this.setSession(refreshed);
      return true;
    } catch {
      this.setSession(null);
      return false;
    }
  }

  async request(path, options = {}, canRefresh = true) {
    const headers = new Headers(options.headers || {});
    if (this.session?.accessToken)
      headers.set("Authorization", `Bearer ${this.session.accessToken}`);
    if (this.tenantId)
      headers.set("X-MG-Tenant", this.tenantId);
    if (this.branchId)
      headers.set("X-MG-Branch", this.branchId);

    try {
      return await this.rawRequest(path, { ...options, headers });
    } catch (error) {
      if (error instanceof ApiError && error.status === 401 && canRefresh
        && path !== "/auth/login" && path !== "/auth/register" && path !== "/auth/refresh") {
        const refreshed = await this.refreshSession();
        if (refreshed) return this.request(path, options, false);
      }
      throw error;
    }
  }

  login(email, password) {
    return this.rawRequest("/auth/login", {
      method: "POST",
      body: { email, password, useCookies: false }
    });
  }

  register(email, password) {
    return this.rawRequest("/auth/register", {
      method: "POST",
      body: { email, password }
    });
  }

  get(path, options) {
    return this.request(path, options);
  }

  post(path, body, options = {}) {
    return this.request(path, { ...options, method: "POST", body });
  }

  put(path, body, options = {}) {
    return this.request(path, { ...options, method: "PUT", body });
  }
}

export function formatApiError(error) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof TypeError) return "ارتباط با سرور برقرار نشد. نشانی API، اینترنت و تنظیمات CORS را بررسی کنید.";
  return error?.message || "خطای غیرمنتظره رخ داد.";
}

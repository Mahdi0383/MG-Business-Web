export function isPendingOperationVisible(operation, branchId, allowTenantWide, deviceId) {
  if (!operation || typeof operation !== "object" || Array.isArray(operation)) return false;
  if (typeof allowTenantWide !== "boolean" || typeof deviceId !== "string" || !deviceId.trim()) return false;
  if (operation.deviceId !== deviceId) return false;
  if (branchId === null) return allowTenantWide;
  if (typeof branchId !== "string" || !branchId.trim()) return false;
  return operation.branchId === branchId || (allowTenantWide && operation.branchId == null);
}

export function isJsonObjectPayload(payloadJson) {
  if (typeof payloadJson !== "string") return false;
  try {
    const payload = JSON.parse(payloadJson);
    return payload !== null && typeof payload === "object" && !Array.isArray(payload);
  } catch {
    return false;
  }
}

export function isSameIncomingEnvelope(existing, incoming) {
  if (!existing || typeof existing !== "object" || Array.isArray(existing)
    || !incoming || typeof incoming !== "object" || Array.isArray(incoming)) return false;
  const immutableFields = [
    "tenantId", "sequence", "operationId", "actorId", "deviceId", "branchId",
    "operationType", "schemaVersion", "authorityEpoch", "occurredAtUtc",
    "receivedAtUtc", "payloadJson", "payloadSha256"
  ];
  return immutableFields.every(field => existing[field] === incoming[field]);
}

export function isConflictVisible(conflict, branchId, allowTenantWide) {
  if (!conflict || typeof conflict !== "object" || Array.isArray(conflict)
    || typeof allowTenantWide !== "boolean") return false;
  if (branchId === null) return allowTenantWide && conflict.branchId == null;
  if (typeof branchId !== "string" || !branchId.trim()) return false;
  return conflict.branchId === branchId || (allowTenantWide && conflict.branchId == null);
}

export function isIncomingRowVisible(row, branchId, allowTenantWide) {
  if (!row || typeof row !== "object" || Array.isArray(row)) return false;
  if (typeof allowTenantWide !== "boolean") return false;
  if (branchId === null) return allowTenantWide && row.branchId == null;
  return row.branchId === branchId || (allowTenantWide && row.branchId == null);
}

const DATABASE_NAME = "mg-business-local";
const DATABASE_VERSION = 8;
const OPERATIONS_STORE = "operations";
const META_STORE = "metadata";
const INBOX_STORE = "inbox";
const CUSTOMERS_STORE = "customers";
const CONFLICTS_STORE = "conflicts";
const INVENTORY_PRODUCTS_STORE = "inventoryProducts";
const INVENTORY_BASELINES_STORE = "inventoryBaselines";
const STOCK_MOVEMENTS_STORE = "stockMovements";
const REQUIRE_AUTHORITY_LEASES = import.meta.env?.VITE_REQUIRE_AUTHORITY_LEASES === "true";
const AUTHORITY_PUBLIC_KEY = import.meta.env?.VITE_AUTHORITY_LEASE_PUBLIC_KEY_SPKI_BASE64 || "";

function decodeBase64(value) {
  if (typeof value !== "string" || !value) throw new TypeError("A Base64 value is required.");
  return Uint8Array.from(atob(value), character => character.charCodeAt(0));
}

let databasePromise;

function openDatabase() {
  if (typeof indexedDB === "undefined")
    return Promise.reject(new Error("IndexedDB is unavailable in this environment."));

  if (!databasePromise) {
    databasePromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
      request.onupgradeneeded = event => {
        const database = request.result;
        let operations;
        if (!database.objectStoreNames.contains(OPERATIONS_STORE)) {
          operations = database.createObjectStore(OPERATIONS_STORE, { keyPath: "operationId" });
        } else {
          operations = event.target.transaction.objectStore(OPERATIONS_STORE);
        }
        if (!operations.indexNames.contains("byStateCreatedAt"))
          operations.createIndex("byStateCreatedAt", ["state", "createdAtUtc"]);
        if (!operations.indexNames.contains("byTenantState"))
          operations.createIndex("byTenantState", ["tenantId", "state"]);
        if (!operations.indexNames.contains("byTenantStateCreatedAt"))
          operations.createIndex("byTenantStateCreatedAt", ["tenantId", "state", "createdAtUtc"]);
        if (!database.objectStoreNames.contains(META_STORE))
          database.createObjectStore(META_STORE, { keyPath: "key" });
        let inbox;
        if (!database.objectStoreNames.contains(INBOX_STORE))
          inbox = database.createObjectStore(INBOX_STORE, { keyPath: ["tenantId", "sequence"] });
        else
          inbox = event.target.transaction.objectStore(INBOX_STORE);
        if (inbox.indexNames.contains("byTenantApplied"))
          inbox.deleteIndex("byTenantApplied");
        // IndexedDB keys cannot contain booleans. Backfill a numeric index key
        // while preserving the public boolean applied flag used by the processor.
        const backfill = inbox.openCursor();
        backfill.onsuccess = () => {
          const cursor = backfill.result;
          if (cursor) {
            const row = cursor.value;
            row.appliedIndex = row.applied ? 1 : 0;
            cursor.update(row);
            cursor.continue();
            return;
          }
          inbox.createIndex("byTenantApplied", ["tenantId", "appliedIndex"]);
        };
        let customers;
        if (!database.objectStoreNames.contains(CUSTOMERS_STORE))
          customers = database.createObjectStore(CUSTOMERS_STORE, { keyPath: ["tenantId", "id"] });
        else
          customers = event.target.transaction.objectStore(CUSTOMERS_STORE);
        if (!customers.indexNames.contains("byTenantCode"))
          customers.createIndex("byTenantCode", ["tenantId", "code"], { unique: true });
        if (!customers.indexNames.contains("byTenantUpdatedAt"))
          customers.createIndex("byTenantUpdatedAt", ["tenantId", "updatedAtUtc"]);
        let conflicts;
        if (!database.objectStoreNames.contains(CONFLICTS_STORE))
          conflicts = database.createObjectStore(CONFLICTS_STORE, { keyPath: ["tenantId", "sequence"] });
        else
          conflicts = event.target.transaction.objectStore(CONFLICTS_STORE);
        if (!conflicts.indexNames.contains("byTenantStatusCreatedAt"))
          conflicts.createIndex("byTenantStatusCreatedAt", ["tenantId", "status", "createdAtUtc"]);
        let inventoryProducts;
        if (!database.objectStoreNames.contains(INVENTORY_PRODUCTS_STORE))
          inventoryProducts = database.createObjectStore(INVENTORY_PRODUCTS_STORE, { keyPath: ["tenantId", "id"] });
        else
          inventoryProducts = event.target.transaction.objectStore(INVENTORY_PRODUCTS_STORE);
        if (!inventoryProducts.indexNames.contains("byTenant"))
          inventoryProducts.createIndex("byTenant", "tenantId");
        let inventoryBaselines;
        if (!database.objectStoreNames.contains(INVENTORY_BASELINES_STORE))
          inventoryBaselines = database.createObjectStore(INVENTORY_BASELINES_STORE, { keyPath: ["tenantId", "branchId", "productId"] });
        else
          inventoryBaselines = event.target.transaction.objectStore(INVENTORY_BASELINES_STORE);
        if (!inventoryBaselines.indexNames.contains("byTenantBranch"))
          inventoryBaselines.createIndex("byTenantBranch", ["tenantId", "branchId"]);
        let stockMovements;
        if (!database.objectStoreNames.contains(STOCK_MOVEMENTS_STORE))
          stockMovements = database.createObjectStore(STOCK_MOVEMENTS_STORE, { keyPath: ["tenantId", "branchId", "operationId"] });
        else
          stockMovements = event.target.transaction.objectStore(STOCK_MOVEMENTS_STORE);
        if (!stockMovements.indexNames.contains("byTenantBranchProduct"))
          stockMovements.createIndex("byTenantBranchProduct", ["tenantId", "branchId", "productId"]);
        if (!stockMovements.indexNames.contains("byTenantState"))
          stockMovements.createIndex("byTenantState", ["tenantId", "state"]);
      };
      request.onsuccess = () => {
        const database = request.result;
        database.onversionchange = () => {
          // Let another tab upgrade the shared schema instead of blocking it indefinitely.
          database.close();
          databasePromise = null;
        };
        resolve(database);
      };
      request.onerror = () => reject(request.error || new Error("Could not open local MG database."));
      request.onblocked = () => reject(new Error("Local MG database upgrade is blocked by another open tab."));
    }).catch(error => {
      databasePromise = null;
      throw error;
    });
  }

  return databasePromise;
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("IndexedDB request failed."));
  });
}

function transactionDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error || new Error("IndexedDB transaction failed."));
    transaction.onabort = () => reject(transaction.error || new Error("IndexedDB transaction was aborted."));
  });
}

async function sha256(value) {
  if (!globalThis.crypto?.subtle)
    throw new Error("Secure cryptography is unavailable. Open MG Business over HTTPS or localhost.");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("").toUpperCase();
}

function roundInventoryToEven(value, scale) {
  const factor = 10 ** scale;
  const scaled = value * factor;
  const lower = Math.floor(scaled);
  const fraction = scaled - lower;
  let rounded;
  if (Math.abs(fraction - 0.5) < 1e-8)
    rounded = lower % 2 === 0 ? lower : lower + 1;
  else
    rounded = fraction < 0.5 ? lower : lower + 1;
  return rounded / factor;
}

function roundInventoryValue(value) {
  return roundInventoryToEven(value, 2);
}

function roundInventoryAverage(value) {
  return roundInventoryToEven(value, 4);
}

function applyLocalStockMovement(balance, movement) {
  const inbound = [1, 5, 6].includes(Number(movement.type));
  if (inbound) {
    balance.quantity += Number(movement.quantity);
    balance.value += Number(movement.quantity) * Number(movement.unitCost);
  } else {
    if (Number(movement.quantity) > balance.quantity)
      throw new Error("موجودی محلی برای خروج کالا کافی نیست.");
    const averageCost = balance.quantity === 0 ? 0 : balance.value / balance.quantity;
    balance.quantity -= Number(movement.quantity);
    balance.value -= Number(movement.quantity) * averageCost;
  }
}

export function normalizeLocalCustomer(input) {
  if (!input || typeof input !== "object" || Array.isArray(input))
    throw new TypeError("Customer input must be an object.");
  const code = typeof input.code === "string" ? input.code.trim().toUpperCase() : "";
  const name = typeof input.name === "string" ? input.name.trim() : "";
  if (!code || code.length > 40) throw new TypeError("Customer code must contain 1–40 characters.");
  if (!name || name.length > 160) throw new TypeError("Customer name must contain 1–160 characters.");
  const normalizeOptional = (value, field, maxLength) => {
    if (value == null || value === "") return null;
    if (typeof value !== "string") throw new TypeError("Customer " + field + " must be text.");
    const normalized = value.trim();
    if (normalized.length > maxLength) throw new TypeError("Customer " + field + " cannot exceed " + maxLength + " characters.");
    return normalized || null;
  };
  const phone = normalizeOptional(input.phone, "phone", 40);
  const email = (normalizeOptional(input.email, "email", 254) || null)?.toLowerCase() ?? null;
  if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))
    throw new TypeError("Customer email address is invalid.");
  return {
    code, name, phone, email,
    address: normalizeOptional(input.address, "address", 500),
    notes: normalizeOptional(input.notes, "notes", 1000)
  };
}

export class LocalOperationStore {
  async getDeviceId(actorId, tenantId = null, branchId = null) {
    if (typeof actorId !== "string" || !actorId.trim())
      throw new TypeError("actorId is required to identify a registered browser device.");
    if (tenantId !== null && (typeof tenantId !== "string" || !tenantId.trim()))
      throw new TypeError("tenantId must be a non-empty string or null.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    const database = await openDatabase();
    const transaction = database.transaction(META_STORE, "readwrite");
    const done = transactionDone(transaction);
    const store = transaction.objectStore(META_STORE);
    const key = tenantId
      ? `device-id:${tenantId}:${branchId || "tenant-wide"}:${actorId}`
      : `device-id:${actorId}`;
    const existing = await requestResult(store.get(key));
    if (existing?.value) {
      await done;
      return existing.value;
    }
    const deviceId = crypto.randomUUID();
    store.put({ key, value: deviceId });
    await done;
    return deviceId;
  }

  async cacheInventorySnapshot({ tenantId, branchId, products, stockRows }) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required to cache inventory.");
    if (typeof branchId !== "string" || !branchId.trim())
      throw new TypeError("branchId is required to cache branch inventory.");
    if (!Array.isArray(products) || !Array.isArray(stockRows))
      throw new TypeError("Inventory products and stock rows must be arrays.");
    const stockByProduct = new Map(stockRows
      .filter(row => row && row.branchId === branchId && typeof row.productId === "string")
      .map(row => [row.productId, row]));
    const database = await openDatabase();
    const transaction = database.transaction(
      [INVENTORY_PRODUCTS_STORE, INVENTORY_BASELINES_STORE, STOCK_MOVEMENTS_STORE], "readwrite"
    );
    const done = transactionDone(transaction);
    const productStore = transaction.objectStore(INVENTORY_PRODUCTS_STORE);
    const baselineStore = transaction.objectStore(INVENTORY_BASELINES_STORE);
    const localMovements = await requestResult(transaction.objectStore(STOCK_MOVEMENTS_STORE)
      .index("byTenantBranchProduct")
      .getAll(IDBKeyRange.bound([tenantId, branchId, ""], [tenantId, branchId, "\uffff"])));
    const productsWithPendingMovements = new Set(localMovements
      .filter(row => row.state === "pending").map(row => row.productId));
    const cachedAtUtc = new Date().toISOString();
    for (const product of products) {
      if (!product || typeof product.id !== "string" || !product.id.trim()) continue;
      productStore.put({ ...product, tenantId, cachedAtUtc });
      if (!product.tracksInventory) continue;
      if (productsWithPendingMovements.has(product.id)) continue;
      const stock = stockByProduct.get(product.id);
      baselineStore.put({
        tenantId, branchId, productId: product.id,
        quantityOnHand: Number(stock?.quantityOnHand || 0),
        inventoryValue: Number(stock?.inventoryValue || 0),
        rawInventoryValue: Number(stock?.rawInventoryValue ?? stock?.inventoryValue ?? 0),
        averageUnitCost: Number(stock?.averageUnitCost || 0),
        cachedAtUtc
      });
    }
    await done;
  }

  async getInventoryProducts(tenantId) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required to read cached inventory products.");
    const database = await openDatabase();
    const transaction = database.transaction(INVENTORY_PRODUCTS_STORE, "readonly");
    const done = transactionDone(transaction);
    const rows = await requestResult(transaction.objectStore(INVENTORY_PRODUCTS_STORE)
      .index("byTenant").getAll(tenantId));
    await done;
    return rows;
  }

  async getInventoryStock(tenantId, branchId) {
    if (typeof tenantId !== "string" || !tenantId.trim()
      || typeof branchId !== "string" || !branchId.trim())
      throw new TypeError("Tenant and branch are required to read cached stock.");
    const database = await openDatabase();
    const transaction = database.transaction(
      [INVENTORY_BASELINES_STORE, STOCK_MOVEMENTS_STORE], "readonly"
    );
    const done = transactionDone(transaction);
    const baselines = await requestResult(transaction.objectStore(INVENTORY_BASELINES_STORE)
      .index("byTenantBranch").getAll([tenantId, branchId]));
    const movements = await requestResult(transaction.objectStore(STOCK_MOVEMENTS_STORE)
      .index("byTenantBranchProduct").getAll(IDBKeyRange.bound([tenantId, branchId, ""], [tenantId, branchId, "\uffff"])));
    await done;
    const pendingByProduct = new Map();
    for (const movement of movements.filter(row => row.state === "pending")) {
      if (!pendingByProduct.has(movement.productId)) pendingByProduct.set(movement.productId, []);
      pendingByProduct.get(movement.productId).push(movement);
    }
    return baselines.map(baseline => {
      const balance = {
        quantity: Number(baseline.quantityOnHand || 0),
        value: Number(baseline.rawInventoryValue ?? baseline.inventoryValue ?? 0)
      };
      const pending = (pendingByProduct.get(baseline.productId) || [])
        .sort((a, b) => a.createdAtUtc.localeCompare(b.createdAtUtc));
      for (const movement of pending) applyLocalStockMovement(balance, movement);
      const value = roundInventoryValue(balance.value);
      return {
        productId: baseline.productId,
        branchId,
        quantityOnHand: balance.quantity,
        inventoryValue: value,
        rawInventoryValue: balance.value,
        averageUnitCost: balance.quantity === 0 ? 0 : roundInventoryAverage(value / balance.quantity),
        pendingMovementCount: pending.length,
        cachedAtUtc: baseline.cachedAtUtc
      };
    });
  }

  async createStockMovement({ tenantId, branchId, actorId, movement }) {
    if (typeof tenantId !== "string" || !tenantId.trim()
      || typeof branchId !== "string" || !branchId.trim()
      || typeof actorId !== "string" || !actorId.trim())
      throw new TypeError("Tenant, branch and actor are required for an offline stock movement.");
    if (!movement || typeof movement !== "object" || Array.isArray(movement))
      throw new TypeError("Stock movement input must be an object.");
    const productId = movement.productId;
    const type = Number(movement.type);
    const quantity = Number(movement.quantity);
    const inbound = [1, 5, 6].includes(type);
    const unitCost = inbound ? Number(movement.unitCost) : null;
    const reference = typeof movement.reference === "string" && movement.reference.trim()
      ? movement.reference.trim() : null;
    if (typeof productId !== "string" || !productId.trim() || !Number.isInteger(type) || type < 1 || type > 7)
      throw new TypeError("A valid product and stock movement type are required.");
    if (!Number.isFinite(quantity) || quantity <= 0 || Math.round(quantity * 1000) !== quantity * 1000)
      throw new TypeError("Stock quantity must be positive and support at most three decimal places.");
    if (inbound && (!Number.isFinite(unitCost) || unitCost < 0 || Math.round(unitCost * 100) !== unitCost * 100))
      throw new TypeError("Inbound stock movements require a non-negative unit cost with at most two decimals.");
    if (reference && reference.length > 250)
      throw new TypeError("Stock movement reference cannot exceed 250 characters.");

    const idempotencyKey = movement.idempotencyKey;
    if (idempotencyKey !== undefined && (typeof idempotencyKey !== "string"
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(idempotencyKey)))
      throw new TypeError("Offline stock movement idempotency key must be a UUID.");
    const operationId = idempotencyKey || crypto.randomUUID();
    const authorityEpoch = await this.getAuthorityEpoch({ tenantId, branchId, actorId });
    if (REQUIRE_AUTHORITY_LEASES && authorityEpoch < 1)
      throw new Error("A verified authority lease is required before queuing offline inventory movements.");
    const deviceId = await this.getDeviceId(actorId, tenantId, branchId);
    const database = await openDatabase();
    const stores = [OPERATIONS_STORE, INVENTORY_PRODUCTS_STORE, INVENTORY_BASELINES_STORE, STOCK_MOVEMENTS_STORE];

    const readTransaction = database.transaction(stores, "readonly");
    const readDone = transactionDone(readTransaction);
    const readOperations = readTransaction.objectStore(OPERATIONS_STORE);
    const readProducts = readTransaction.objectStore(INVENTORY_PRODUCTS_STORE);
    const readBaselines = readTransaction.objectStore(INVENTORY_BASELINES_STORE);
    const readMovements = readTransaction.objectStore(STOCK_MOVEMENTS_STORE);
    const [product, baseline, priorOperation, localMovements] = await Promise.all([
      requestResult(readProducts.get([tenantId, productId])),
      requestResult(readBaselines.get([tenantId, branchId, productId])),
      requestResult(readOperations.get(operationId)),
      requestResult(readMovements.index("byTenantBranchProduct")
        .getAll(IDBKeyRange.only([tenantId, branchId, productId])))
    ]);
    await readDone;

    if (priorOperation) {
      const priorMovement = localMovements.find(row => row.operationId === operationId);
      const sameRequest = priorMovement
        && priorMovement.productId === productId
        && Number(priorMovement.type) === type
        && Number(priorMovement.quantity) === quantity
        && (inbound ? Number(priorMovement.unitCost) === unitCost : true)
        && (priorMovement.reference || null) === reference;
      if (!sameRequest)
        throw new Error("این کلید یکتا قبلاً برای گردش انبار دیگری استفاده شده است.");
      return priorMovement;
    }
    if (!product || !product.isActive || !product.tracksInventory)
      throw new Error("کالای فعال و انبارگردانی‌شونده در حافظه محلی موجود نیست.");
    if (!baseline)
      throw new Error("ابتدا باید موجودی کالا هنگام اتصال به سرور دریافت و ذخیره شود.");

    const pendingMovements = localMovements
      .filter(row => row.state === "pending")
      .sort((a, b) => a.createdAtUtc.localeCompare(b.createdAtUtc));
    const balance = {
      quantity: Number(baseline.quantityOnHand || 0),
      value: Number(baseline.rawInventoryValue ?? baseline.inventoryValue ?? 0)
    };
    for (const localMovement of pendingMovements)
      applyLocalStockMovement(balance, localMovement);

    const baseQuantityOnHand = balance.quantity;
    const baseInventoryValue = roundInventoryValue(balance.value);
    const averageUnitCost = balance.quantity === 0
      ? 0 : roundInventoryAverage(baseInventoryValue / balance.quantity);
    if (!inbound && quantity > baseQuantityOnHand)
      throw new Error("موجودی محلی برای خروج کالا کافی نیست.");

    const effectiveUnitCost = inbound ? unitCost : averageUnitCost;
    const nextBalance = { quantity: balance.quantity, value: balance.value };
    applyLocalStockMovement(nextBalance, { type, quantity, unitCost: effectiveUnitCost });
    const payloadJson = JSON.stringify({
      productId, type, quantity, unitCost: inbound ? unitCost : null,
      baseQuantityOnHand, baseInventoryValue, reference
    });
    const payloadSha256 = await sha256(payloadJson);
    const now = new Date().toISOString();
    const operation = {
      operationId, deviceId, tenantId, branchId,
      operationType: "inventory.stock.movement.created",
      schemaVersion: 1, authorityEpoch,
      occurredAtUtc: now, payloadJson, payloadSha256,
      state: "pending", attempts: 0, createdAtUtc: now,
      acknowledgedAtUtc: null, serverSequence: null
    };
    const localMovement = {
      tenantId, branchId, productId, operationId,
      type, quantity, unitCost: effectiveUnitCost, reference,
      baseQuantityOnHand, baseInventoryValue,
      resultQuantityOnHand: nextBalance.quantity,
      resultInventoryValue: roundInventoryValue(nextBalance.value),
      resultRawInventoryValue: nextBalance.value,
      state: "pending", createdAtUtc: now, serverSequence: null
    };

    // Recheck the baseline and pending movement set inside the write transaction.
    // Hashing happens before this transaction so WebCrypto cannot let IndexedDB auto-commit.
    const writeTransaction = database.transaction(stores, "readwrite");
    const writeDone = transactionDone(writeTransaction);
    const writeOperations = writeTransaction.objectStore(OPERATIONS_STORE);
    const writeProducts = writeTransaction.objectStore(INVENTORY_PRODUCTS_STORE);
    const writeBaselines = writeTransaction.objectStore(INVENTORY_BASELINES_STORE);
    const writeMovements = writeTransaction.objectStore(STOCK_MOVEMENTS_STORE);
    const [currentProduct, currentBaseline, currentOperation, currentMovements] = await Promise.all([
      requestResult(writeProducts.get([tenantId, productId])),
      requestResult(writeBaselines.get([tenantId, branchId, productId])),
      requestResult(writeOperations.get(operationId)),
      requestResult(writeMovements.index("byTenantBranchProduct")
        .getAll(IDBKeyRange.only([tenantId, branchId, productId])))
    ]);
    if (currentOperation) {
      writeTransaction.abort();
      await writeDone.catch(() => {});
      const priorMovement = currentMovements.find(row => row.operationId === operationId);
      const sameRequest = priorMovement
        && priorMovement.productId === productId
        && Number(priorMovement.type) === type
        && Number(priorMovement.quantity) === quantity
        && (inbound ? Number(priorMovement.unitCost) === unitCost : true)
        && (priorMovement.reference || null) === reference;
      if (!sameRequest)
        throw new Error("این کلید یکتا قبلاً برای گردش انبار دیگری استفاده شده است.");
      return priorMovement;
    }
    const beforeFingerprint = JSON.stringify({
      cachedAtUtc: baseline.cachedAtUtc,
      quantityOnHand: baseline.quantityOnHand,
      inventoryValue: baseline.inventoryValue,
      rawInventoryValue: baseline.rawInventoryValue,
      pending: pendingMovements.map(row => [
        row.operationId, row.type, row.quantity, row.unitCost, row.createdAtUtc, row.state
      ])
    });
    const currentPending = (currentMovements || [])
      .filter(row => row.state === "pending")
      .sort((a, b) => a.createdAtUtc.localeCompare(b.createdAtUtc));
    const currentFingerprint = JSON.stringify({
      cachedAtUtc: currentBaseline?.cachedAtUtc,
      quantityOnHand: currentBaseline?.quantityOnHand,
      inventoryValue: currentBaseline?.inventoryValue,
      rawInventoryValue: currentBaseline?.rawInventoryValue,
      pending: currentPending.map(row => [
        row.operationId, row.type, row.quantity, row.unitCost, row.createdAtUtc, row.state
      ])
    });
    if (!currentProduct?.isActive || !currentProduct?.tracksInventory
      || beforeFingerprint !== currentFingerprint) {
      writeTransaction.abort();
      await writeDone.catch(() => {});
      throw new Error("موجودی محلی هم‌زمان تغییر کرده است؛ گردش را دوباره بررسی و ثبت کنید.");
    }
    writeMovements.add(localMovement);
    writeOperations.add(operation);
    await writeDone;
    return localMovement;
  }

  async setAuthorityLease({ tenantId, branchId = null, actorId, lease }) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required to store an authority lease.");
    if (typeof actorId !== "string" || !actorId.trim())
      throw new TypeError("actorId is required to store an authority lease.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    if (!lease || lease.tenantId !== tenantId || (lease.branchId || null) !== (branchId || null)
      || typeof lease.deviceId !== "string" || !lease.deviceId || !Number.isSafeInteger(lease.epoch) || lease.epoch < 1
      || typeof lease.signatureBase64 !== "string" || typeof lease.publicKeySpkiBase64 !== "string"
      || !Number.isFinite(Date.parse(lease.expiresAtUtc)) || Date.parse(lease.expiresAtUtc) <= Date.now())
      throw new TypeError("Only an active authority lease for this tenant and branch scope can be stored.");
    const key = `authority-lease:${tenantId}:${branchId || "tenant-wide"}`;
    const database = await openDatabase();
    const transaction = database.transaction(META_STORE, "readwrite");
    const done = transactionDone(transaction);
    transaction.objectStore(META_STORE).put({ key, value: lease });
    await done;
  }

  async clearAuthorityLease({ tenantId, branchId = null }) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required to clear an authority lease.");
    const key = `authority-lease:${tenantId}:${branchId || "tenant-wide"}`;
    const database = await openDatabase();
    const transaction = database.transaction(META_STORE, "readwrite");
    const done = transactionDone(transaction);
    transaction.objectStore(META_STORE).delete(key);
    await done;
  }

  async getAuthorityEpoch({ tenantId, branchId = null, actorId }) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required to read the authority lease.");
    if (typeof actorId !== "string" || !actorId.trim())
      throw new TypeError("actorId is required to read the authority lease.");
    const key = `authority-lease:${tenantId}:${branchId || "tenant-wide"}`;
    const database = await openDatabase();
    const transaction = database.transaction(META_STORE, "readonly");
    const done = transactionDone(transaction);
    const stored = await requestResult(transaction.objectStore(META_STORE).get(key));
    await done;
    const lease = stored?.value;
    let valid = false;
    try {
      const now = Date.now();
      const issuedAt = Date.parse(lease?.issuedAtUtc);
      const expiresAt = Date.parse(lease?.expiresAtUtc);
      const expectedScope = branchId ? branchId.replaceAll("-", "").toLowerCase() : "tenant-wide";
      if (lease?.tenantId === tenantId && (lease.branchId || null) === (branchId || null)
        && typeof lease.deviceId === "string" && lease.deviceId.length > 0 && lease.scopeKey === expectedScope
        && Number.isSafeInteger(lease.epoch) && lease.epoch > 0
        && Number.isFinite(issuedAt) && Number.isFinite(expiresAt)
        && issuedAt <= now + 30_000 && expiresAt > now && expiresAt > issuedAt
        && expiresAt - issuedAt <= 5 * 60_000
        && AUTHORITY_PUBLIC_KEY && lease.publicKeySpkiBase64 === AUTHORITY_PUBLIC_KEY
        && typeof lease.leaseId === "string" && typeof lease.signatureBase64 === "string") {
        const publicKey = await crypto.subtle.importKey(
          "spki", decodeBase64(AUTHORITY_PUBLIC_KEY),
          { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]
        );
        const canonical = [
          "MG-AUTHORITY-LEASE-V1",
          lease.tenantId,
          lease.branchId || "",
          lease.scopeKey,
          lease.deviceId,
          lease.leaseId,
          String(lease.epoch),
          String(issuedAt),
          String(expiresAt)
        ].join("\n");
        valid = await crypto.subtle.verify(
          { name: "ECDSA", hash: "SHA-256" },
          publicKey,
          decodeBase64(lease.signatureBase64),
          new TextEncoder().encode(canonical)
        );
      }
    } catch {
      valid = false;
    }
    if (!valid) {
      if (REQUIRE_AUTHORITY_LEASES)
        throw new Error("A cryptographically verified, unexpired authority lease for this device is required before making offline business changes.");
      return 0;
    }
    return lease.epoch;
  }

  async enqueue({ tenantId, branchId = null, actorId, operationType, payload, schemaVersion = 1, authorityEpoch = 0 }) {
    if (typeof tenantId !== "string" || !tenantId.trim()) throw new TypeError("tenantId is required.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    if (typeof operationType !== "string" || !operationType.trim() || operationType.trim().length > 100)
      throw new TypeError("operationType must be 1–100 characters.");
    if (!Number.isInteger(schemaVersion) || schemaVersion < 1) throw new TypeError("schemaVersion must be a positive integer.");
    if (!Number.isSafeInteger(authorityEpoch) || authorityEpoch < 0)
      throw new TypeError("authorityEpoch must be a non-negative safe integer.");
    if (!payload || typeof payload !== "object" || Array.isArray(payload))
      throw new TypeError("Offline operation payload must be a JSON object.");

    let payloadJson;
    try { payloadJson = JSON.stringify(payload); }
    catch { throw new TypeError("Offline operation payload must be JSON serializable."); }
    if (typeof payloadJson !== "string")
      throw new TypeError("Offline operation payload must be JSON serializable.");
    if (new TextEncoder().encode(payloadJson).byteLength > 64 * 1024)
      throw new RangeError("Offline operation payload exceeds 64 KiB.");
    if (typeof actorId !== "string" || !actorId.trim())
      throw new TypeError("actorId is required to enqueue an authenticated offline operation.");
    const verifiedEpoch = await this.getAuthorityEpoch({ tenantId, branchId, actorId });
    if (authorityEpoch !== 0 && authorityEpoch !== verifiedEpoch)
      throw new TypeError("The operation authority epoch does not match the verified signed lease.");
    const effectiveAuthorityEpoch = verifiedEpoch || authorityEpoch;
    if (REQUIRE_AUTHORITY_LEASES && effectiveAuthorityEpoch < 1)
      throw new Error("A verified authority lease is required before queuing offline operations.");

    const operation = {
      operationId: crypto.randomUUID(),
      deviceId: await this.getDeviceId(actorId, tenantId, branchId),
      tenantId,
      branchId,
      operationType: operationType.trim(),
      schemaVersion,
      authorityEpoch: effectiveAuthorityEpoch,
      occurredAtUtc: new Date().toISOString(),
      payloadJson,
      payloadSha256: await sha256(payloadJson),
      state: "pending",
      attempts: 0,
      createdAtUtc: new Date().toISOString(),
      acknowledgedAtUtc: null,
      serverSequence: null
    };

    const database = await openDatabase();
    const transaction = database.transaction(OPERATIONS_STORE, "readwrite");
    const done = transactionDone(transaction);
    transaction.objectStore(OPERATIONS_STORE).add(operation);
    await done;
    return operation;
  }

  async createCustomer({ tenantId, branchId, actorId, customer }) {
    if (typeof tenantId !== "string" || !tenantId.trim()) throw new TypeError("tenantId is required.");
    if (typeof branchId !== "string" || !branchId.trim()) throw new TypeError("branchId is required for a branch-originated customer command.");
    if (typeof actorId !== "string" || !actorId.trim()) throw new TypeError("actorId is required.");
    const authorityEpoch = await this.getAuthorityEpoch({ tenantId, branchId, actorId });
    const normalized = normalizeLocalCustomer(customer);
    const now = new Date().toISOString();
    const id = crypto.randomUUID();
    const payloadJson = JSON.stringify({ customerId: id, ...normalized, baseVersion: 0 });
    const operation = {
      operationId: crypto.randomUUID(),
      deviceId: await this.getDeviceId(actorId, tenantId, branchId),
      tenantId, branchId,
      operationType: "customer.profile.created",
      schemaVersion: 1, authorityEpoch,
      occurredAtUtc: now,
      payloadJson,
      payloadSha256: await sha256(payloadJson),
      state: "pending", attempts: 0,
      createdAtUtc: now, acknowledgedAtUtc: null, serverSequence: null
    };
    const localCustomer = {
      tenantId, id, ...normalized, version: 1,
      isActive: true,
      syncStatus: "pending",
      createdAtUtc: now, updatedAtUtc: now,
      createdByActorId: actorId, originBranchId: branchId,
      originOperationId: operation.operationId, lastOperationId: operation.operationId
    };
    const database = await openDatabase();
    const transaction = database.transaction([OPERATIONS_STORE, CUSTOMERS_STORE], "readwrite");
    const done = transactionDone(transaction);
    transaction.objectStore(CUSTOMERS_STORE).add(localCustomer);
    transaction.objectStore(OPERATIONS_STORE).add(operation);
    await done;
    return localCustomer;
  }

  async updateCustomer({ tenantId, branchId, actorId, customerId, customer }) {
    if (typeof tenantId !== "string" || !tenantId.trim()) throw new TypeError("tenantId is required.");
    if (typeof branchId !== "string" || !branchId.trim()) throw new TypeError("branchId is required for a branch-originated customer command.");
    if (typeof actorId !== "string" || !actorId.trim()) throw new TypeError("actorId is required.");
    if (typeof customerId !== "string" || !customerId.trim()) throw new TypeError("customerId is required.");

    const database = await openDatabase();
    const readTransaction = database.transaction(CUSTOMERS_STORE, "readonly");
    const readDone = transactionDone(readTransaction);
    const initial = await requestResult(readTransaction.objectStore(CUSTOMERS_STORE).get([tenantId, customerId]));
    await readDone;
    if (!initial) throw new Error("Customer does not exist in the local projection.");
    if (initial.syncStatus === "pending") throw new Error("Customer already has a pending update; synchronize or reconcile it first.");
    if (!Number.isSafeInteger(initial.version) || initial.version < 1)
      throw new Error("Customer projection has no valid server-confirmed version.");

    const authorityEpoch = await this.getAuthorityEpoch({ tenantId, branchId, actorId });
    const normalized = normalizeLocalCustomer({ ...initial, ...customer, code: initial.code });
    const baseVersion = initial.version;
    const payload = {
      customerId, baseVersion, name: normalized.name,
      phone: normalized.phone, email: normalized.email,
      address: normalized.address, notes: normalized.notes
    };
    const now = new Date().toISOString();
    const payloadJson = JSON.stringify(payload);
    const operation = {
      operationId: crypto.randomUUID(),
      deviceId: await this.getDeviceId(actorId, tenantId, branchId),
      tenantId, branchId,
      operationType: "customer.profile.updated",
      schemaVersion: 1, authorityEpoch,
      occurredAtUtc: now,
      payloadJson,
      payloadSha256: await sha256(payloadJson),
      state: "pending", attempts: 0,
      createdAtUtc: now, acknowledgedAtUtc: null, serverSequence: null
    };

    const transaction = database.transaction([OPERATIONS_STORE, CUSTOMERS_STORE], "readwrite");
    const done = transactionDone(transaction);
    const operations = transaction.objectStore(OPERATIONS_STORE);
    const customers = transaction.objectStore(CUSTOMERS_STORE);
    const currentRequest = customers.get([tenantId, customerId]);
    let integrityError = null;
    currentRequest.onsuccess = () => {
      const current = currentRequest.result;
      if (!current || current.version !== baseVersion || current.syncStatus === "pending") {
        integrityError = new Error("Customer changed while preparing the offline update; reload and retry.");
        transaction.abort();
        return;
      }
      customers.put({
        ...current, ...normalized,
        version: baseVersion,
        syncStatus: "pending",
        lastOperationId: operation.operationId,
        pendingBaseVersion: baseVersion,
        updatedAtUtc: now
      });
      operations.add(operation);
    };
    try { await done; }
    catch (error) { if (integrityError) throw integrityError; throw error; }
    if (typeof window !== "undefined") {
      window.dispatchEvent(new Event("mg-customer-sync-updated"));
      window.dispatchEvent(new Event("mg-inventory-sync-updated"));
    }
    return { ...initial, ...normalized, version: baseVersion, syncStatus: "pending", lastOperationId: operation.operationId };
  }


  async deactivateCustomer({ tenantId, branchId, actorId, customerId }) {
    if (typeof tenantId !== "string" || !tenantId.trim()) throw new TypeError("tenantId is required.");
    if (typeof branchId !== "string" || !branchId.trim()) throw new TypeError("branchId is required for a branch-originated customer command.");
    if (typeof actorId !== "string" || !actorId.trim()) throw new TypeError("actorId is required.");
    if (typeof customerId !== "string" || !customerId.trim()) throw new TypeError("customerId is required.");

    const database = await openDatabase();
    const readTransaction = database.transaction(CUSTOMERS_STORE, "readonly");
    const readDone = transactionDone(readTransaction);
    const initial = await requestResult(readTransaction.objectStore(CUSTOMERS_STORE).get([tenantId, customerId]));
    await readDone;
    if (!initial) throw new Error("Customer does not exist in the local projection.");
    if (initial.isActive === false) throw new Error("Customer is already inactive.");
    if (initial.syncStatus === "pending") throw new Error("Customer already has a pending update; synchronize or reconcile it first.");
    if (!Number.isSafeInteger(initial.version) || initial.version < 1)
      throw new Error("Customer projection has no valid server-confirmed version.");

    const authorityEpoch = await this.getAuthorityEpoch({ tenantId, branchId, actorId });
    const baseVersion = initial.version;
    const now = new Date().toISOString();
    const payloadJson = JSON.stringify({ customerId, baseVersion });
    const operation = {
      operationId: crypto.randomUUID(),
      deviceId: await this.getDeviceId(actorId, tenantId, branchId),
      tenantId, branchId,
      operationType: "customer.profile.deactivated",
      schemaVersion: 1, authorityEpoch,
      occurredAtUtc: now,
      payloadJson,
      payloadSha256: await sha256(payloadJson),
      state: "pending", attempts: 0,
      createdAtUtc: now, acknowledgedAtUtc: null, serverSequence: null
    };

    const transaction = database.transaction([OPERATIONS_STORE, CUSTOMERS_STORE], "readwrite");
    const done = transactionDone(transaction);
    const operations = transaction.objectStore(OPERATIONS_STORE);
    const customers = transaction.objectStore(CUSTOMERS_STORE);
    const currentRequest = customers.get([tenantId, customerId]);
    let integrityError = null;
    currentRequest.onsuccess = () => {
      const current = currentRequest.result;
      if (!current || current.version !== baseVersion || current.isActive === false || current.syncStatus === "pending") {
        integrityError = new Error("Customer changed while preparing the offline deactivation; reload and retry.");
        transaction.abort();
        return;
      }
      customers.put({
        ...current,
        isActive: false,
        syncStatus: "pending",
        lastOperationId: operation.operationId,
        pendingBaseVersion: baseVersion,
        updatedAtUtc: now
      });
      operations.add(operation);
    };
    try { await done; }
    catch (error) { if (integrityError) throw integrityError; throw error; }
    if (typeof window !== "undefined")
      window.dispatchEvent(new Event("mg-customer-sync-updated"));
    return { ...initial, isActive: false, syncStatus: "pending", lastOperationId: operation.operationId };
  }

  async cacheServerCustomers(tenantId, rows) {
    if (typeof tenantId !== "string" || !tenantId.trim()) throw new TypeError("tenantId is required.");
    if (!Array.isArray(rows)) throw new TypeError("Server customer rows must be an array.");
    const prepared = rows.map(row => {
      if (!row || typeof row !== "object" || typeof row.id !== "string" || !row.id.trim()
        || !Number.isSafeInteger(row.version) || row.version < 1)
        throw new TypeError("Server customer row has an invalid identity or version.");
      return { row, normalized: normalizeLocalCustomer(row) };
    });
    const database = await openDatabase();
    const transaction = database.transaction(CUSTOMERS_STORE, "readwrite");
    const done = transactionDone(transaction);
    const customers = transaction.objectStore(CUSTOMERS_STORE);
    const now = new Date().toISOString();
    for (const { row, normalized } of prepared) {
      const request = customers.get([tenantId, row.id]);
      request.onsuccess = () => {
        const current = request.result;
        // Never overwrite an unacknowledged local command with a server read.
        if (current?.syncStatus === "pending"
          || (Number.isSafeInteger(current?.version) && current.version > row.version)) return;
        customers.put({
          ...current,
          tenantId, id: row.id, ...normalized,
          version: row.version,
          isActive: row.isActive !== false,
          syncStatus: "synced",
          createdAtUtc: current?.createdAtUtc || now,
          updatedAtUtc: now,
          serverSequence: current?.serverSequence ?? null,
          originOperationId: current?.originOperationId ?? null,
          lastOperationId: current?.lastOperationId ?? null,
          originBranchId: current?.originBranchId ?? null
        });
      };
    }
    await done;
  }

  async getCustomers(tenantId, limit = 500) {
    if (typeof tenantId !== "string" || !tenantId.trim()) throw new TypeError("tenantId is required.");
    const boundedLimit = Number.isInteger(limit) ? Math.max(1, Math.min(limit, 1000)) : 500;
    const database = await openDatabase();
    const transaction = database.transaction(CUSTOMERS_STORE, "readonly");
    const done = transactionDone(transaction);
    const index = transaction.objectStore(CUSTOMERS_STORE).index("byTenantUpdatedAt");
    const range = IDBKeyRange.bound([tenantId, ""], [tenantId, "\uffff"]);
    const rows = await new Promise((resolve, reject) => {
      const values = [];
      const request = index.openCursor(range, "prev");
      request.onerror = () => reject(request.error || new Error("Could not read local customers."));
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor || values.length >= boundedLimit) {
          resolve(values);
          return;
        }
        values.push(cursor.value);
        cursor.continue();
      };
    });
    await done;
    return rows;
  }

  async readConflictState(tenantId, row) {
    if (typeof tenantId !== "string" || !tenantId.trim()) throw new TypeError("tenantId is required.");
    let payload;
    try { payload = JSON.parse(row.payloadJson); } catch { return null; }
    if (row.operationType === "inventory.stock.movement.created") {
      if (typeof row.branchId !== "string" || typeof payload?.productId !== "string")
        return null;
      const database = await openDatabase();
      const transaction = database.transaction(
        [INVENTORY_BASELINES_STORE, STOCK_MOVEMENTS_STORE], "readonly"
      );
      const done = transactionDone(transaction);
      const baselineRequest = transaction.objectStore(INVENTORY_BASELINES_STORE)
        .get([tenantId, row.branchId, payload.productId]);
      const movementRequest = transaction.objectStore(STOCK_MOVEMENTS_STORE)
        .get([tenantId, row.branchId, row.operationId]);
      const [baseline, localMovement] = await Promise.all([
        requestResult(baselineRequest), requestResult(movementRequest)
      ]);
      await done;
      if (!baseline) return null;
      return {
        productId: payload.productId,
        quantityOnHand: Number(baseline.quantityOnHand),
        inventoryValue: Number(baseline.inventoryValue),
        rawInventoryValue: Number(baseline.rawInventoryValue ?? baseline.inventoryValue),
        lastOperationId: localMovement?.operationId || null,
        movementState: localMovement?.state || null
      };
    }
    const customerId = typeof payload?.customerId === "string" ? payload.customerId : "";
    if (!customerId) return null;
    const database = await openDatabase();
    const transaction = database.transaction(CUSTOMERS_STORE, "readonly");
    const done = transactionDone(transaction);
    const customers = transaction.objectStore(CUSTOMERS_STORE);
    const byIdRequest = customers.get([tenantId, customerId]);
    let byCodeRequest = null;
    if (row.operationType === "customer.profile.created" && typeof payload.code === "string")
      byCodeRequest = customers.index("byTenantCode").get([tenantId, payload.code.trim().toUpperCase()]);
    const [byId, byCode] = await Promise.all([
      requestResult(byIdRequest),
      byCodeRequest ? requestResult(byCodeRequest) : Promise.resolve(null)
    ]);
    await done;
    const current = byId || byCode;
    return current ? {
      id: current.id,
      version: current.version,
      isActive: current.isActive !== false,
      originOperationId: current.originOperationId || null,
      lastOperationId: current.lastOperationId || null
    } : null;
  }

  async recordConflict(conflict) {
    if (!conflict || typeof conflict !== "object" || !conflict.tenantId
      || !Number.isSafeInteger(conflict.sequence) || conflict.sequence < 1
      || typeof conflict.operationId !== "string" || !conflict.operationId.trim()
      || (conflict.branchId != null && (typeof conflict.branchId !== "string" || !conflict.branchId.trim())))
      throw new TypeError("A valid incoming conflict record is required.");
    const database = await openDatabase();
    const transaction = database.transaction(CONFLICTS_STORE, "readwrite");
    const done = transactionDone(transaction);
    const conflicts = transaction.objectStore(CONFLICTS_STORE);
    const key = [conflict.tenantId, conflict.sequence];
    const request = conflicts.get(key);
    let integrityError = null;
    request.onsuccess = () => {
      const existing = request.result;
      if (existing && existing.operationId !== conflict.operationId) {
        integrityError = new Error("A conflict sequence was reused for a different operation.");
        transaction.abort();
        return;
      }
      conflicts.put({
        ...conflict,
        status: "open",
        createdAtUtc: existing?.createdAtUtc || new Date().toISOString(),
        updatedAtUtc: new Date().toISOString()
      });
    };
    try { await done; }
    catch (error) { if (integrityError) throw integrityError; throw error; }
  }

  async applyAndAcknowledge({ tenantId, sequence, operation }) {
    if (typeof tenantId !== "string" || !tenantId.trim()) throw new TypeError("tenantId is required.");
    if (!Number.isSafeInteger(sequence) || sequence < 1) throw new TypeError("sequence is invalid.");
    if (!operation || typeof operation !== "object" || !operation.payload)
      throw new TypeError("A validated incoming operation is required.");
    const payload = operation.payload;
    if (operation.operationType === "inventory.stock.movement.created")
      return this.applyInventoryAndAcknowledge({ tenantId, sequence, operation, payload });
    const isCreate = operation.operationType === "customer.profile.created";
    const isUpdate = operation.operationType === "customer.profile.updated";
    const isDeactivate = operation.operationType === "customer.profile.deactivated";
    if (!isCreate && !isUpdate && !isDeactivate)
      throw new Error("No local projection handler is registered for " + operation.operationType + ".");
    if (typeof payload.customerId !== "string" || !payload.customerId.trim())
      throw new TypeError("Customer command requires a customerId.");
    if (isCreate && payload.baseVersion !== 0)
      throw new TypeError("Customer creation requires baseVersion 0.");
    if ((isUpdate || isDeactivate) && (!Number.isSafeInteger(payload.baseVersion) || payload.baseVersion < 1))
      throw new TypeError("Customer update or deactivation requires a positive baseVersion.");
    const database = await openDatabase();
    const transaction = database.transaction([INBOX_STORE, CUSTOMERS_STORE], "readwrite");
    const done = transactionDone(transaction);
    const inboxStore = transaction.objectStore(INBOX_STORE);
    const customerStore = transaction.objectStore(CUSTOMERS_STORE);
    const inboxRequest = inboxStore.get([tenantId, sequence]);
    const customerRequest = customerStore.get([tenantId, payload.customerId]);
    let inboxRow;
    let existingCustomer;
    let inboxReady = false;
    let customerReady = false;
    let integrityError = null;
    const commit = () => {
      if (!inboxReady || !customerReady) return;
      if (!inboxRow) {
        integrityError = new Error("Incoming inbox row disappeared before projection commit.");
        transaction.abort();
        return;
      }
      const now = new Date().toISOString();
      if (isCreate) {
        if (existingCustomer && existingCustomer.originOperationId !== operation.operationId) {
          integrityError = new Error("Customer ID already exists under a different create operation.");
          transaction.abort();
          return;
        }
        if (!existingCustomer) {
          const normalized = normalizeLocalCustomer(payload);
          customerStore.add({
            tenantId, id: payload.customerId, ...normalized,
            version: 1, isActive: true, syncStatus: "synced",
            createdAtUtc: operation.occurredAtUtc || now,
            updatedAtUtc: now,
            createdByActorId: operation.actorId || null,
            originBranchId: operation.branchId || null,
            originOperationId: operation.operationId,
            lastOperationId: operation.operationId,
            serverSequence: sequence
          });
        }
      } else if (isUpdate || isDeactivate) {
        if (!existingCustomer) {
          integrityError = new Error("Customer update projection target does not exist.");
          transaction.abort();
          return;
        }
        // The originating device already applied this exact command locally.
        // Its journal echo only advances the inbox; it must not apply twice.
        if (existingCustomer.lastOperationId !== operation.operationId) {
          if (existingCustomer.version !== payload.baseVersion) {
            integrityError = new Error("Customer projection version changed before inbound update commit.");
            transaction.abort();
            return;
          }
          if (isDeactivate && existingCustomer.isActive === false) {
            integrityError = new Error("Customer is already inactive under a different operation.");
            transaction.abort();
            return;
          }
          const normalized = isUpdate ? normalizeLocalCustomer({ ...existingCustomer, ...payload }) : existingCustomer;
          customerStore.put({
            ...existingCustomer, ...normalized,
            ...(isDeactivate ? { isActive: false } : {}),
            version: payload.baseVersion + 1,
            syncStatus: "synced",
            lastOperationId: operation.operationId,
            serverSequence: sequence,
            updatedAtUtc: now
          });
        }
      }
      inboxRow.applied = true;
      inboxRow.appliedIndex = 1;
      inboxRow.appliedAtUtc = now;
      inboxStore.put(inboxRow);
    };
    inboxRequest.onsuccess = () => { inboxRow = inboxRequest.result; inboxReady = true; commit(); };
    customerRequest.onsuccess = () => { existingCustomer = customerRequest.result; customerReady = true; commit(); };
    try { await done; }
    catch (error) { if (integrityError) throw integrityError; throw error; }
    if (typeof window !== "undefined")
      window.dispatchEvent(new Event("mg-customer-sync-updated"));
  }

  async getPending(tenantId, limit = 100, branchId = null, allowTenantWide = false, deviceId) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required.");
    const boundedLimit = Number.isInteger(limit) ? Math.max(1, Math.min(limit, 100)) : 100;
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    if (typeof allowTenantWide !== "boolean")
      throw new TypeError("allowTenantWide must be a boolean.");
    if (typeof deviceId !== "string" || !deviceId.trim())
      throw new TypeError("deviceId is required to isolate the authenticated device outbox.");
    if (branchId === null && !allowTenantWide) return [];

    const database = await openDatabase();
    const transaction = database.transaction(OPERATIONS_STORE, "readonly");
    const done = transactionDone(transaction);
    const index = transaction.objectStore(OPERATIONS_STORE).index("byTenantStateCreatedAt");
    const range = IDBKeyRange.bound(
      [tenantId, "pending", ""],
      [tenantId, "pending", "\uffff"]
    );

    // Keep FIFO ordering while isolating both the selected branch and the
    // authenticated browser device. Another account's unsynced work must not
    // block this actor's batch or be submitted under a different identity.
    const rows = await new Promise((resolve, reject) => {
      const matching = [];
      const request = index.openCursor(range);
      request.onerror = () => reject(request.error || new Error("Could not read pending sync operations."));
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor || matching.length >= boundedLimit) {
          resolve(matching);
          return;
        }
        const operation = cursor.value;
        if (isPendingOperationVisible(operation, branchId, allowTenantWide, deviceId))
          matching.push(operation);
        cursor.continue();
      };
    });
    await done;
    return rows;
  }

  async acknowledge(acknowledgements) {
    if (!Array.isArray(acknowledgements) || acknowledgements.length === 0) return;
    const database = await openDatabase();
    const readTransaction = database.transaction(OPERATIONS_STORE, "readonly");
    const readDone = transactionDone(readTransaction);
    const readStore = readTransaction.objectStore(OPERATIONS_STORE);
    const existing = await Promise.all(acknowledgements.map(item => requestResult(readStore.get(item.operationId))));
    await readDone;

    const writeTransaction = database.transaction([OPERATIONS_STORE, CUSTOMERS_STORE, INVENTORY_BASELINES_STORE, STOCK_MOVEMENTS_STORE], "readwrite");
    const writeDone = transactionDone(writeTransaction);
    const writeStore = writeTransaction.objectStore(OPERATIONS_STORE);
    const customerStore = writeTransaction.objectStore(CUSTOMERS_STORE);
    const baselineStore = writeTransaction.objectStore(INVENTORY_BASELINES_STORE);
    const stockMovementStore = writeTransaction.objectStore(STOCK_MOVEMENTS_STORE);
    let integrityError = null;
    for (let index = 0; index < existing.length; index += 1) {
      const operation = existing[index];
      if (!operation) continue;
      const acknowledgement = acknowledgements[index];
      if (!Number.isSafeInteger(acknowledgement.sequence) || acknowledgement.sequence < 1)
        throw new TypeError("Server acknowledgement sequence is invalid.");
      operation.state = "acknowledged";
      operation.serverSequence = acknowledgement.sequence;
      operation.acknowledgedAtUtc = new Date().toISOString();
      writeStore.put(operation);

      if (operation.operationType === "inventory.stock.movement.created" && operation.branchId) {
        const stockRequest = stockMovementStore.get([
          operation.tenantId, operation.branchId, operation.operationId
        ]);
        stockRequest.onsuccess = () => {
          const stockMovement = stockRequest.result;
          if (!stockMovement || stockMovement.state !== "pending") return;
          const baselineRequest = baselineStore.get([
            operation.tenantId, operation.branchId, stockMovement.productId
          ]);
          baselineRequest.onsuccess = () => {
            const baseline = baselineRequest.result;
            if (baseline
              && Number(baseline.quantityOnHand) === Number(stockMovement.baseQuantityOnHand)
              && Number(baseline.inventoryValue) === Number(stockMovement.baseInventoryValue)) {
              baseline.quantityOnHand = stockMovement.resultQuantityOnHand;
              baseline.inventoryValue = stockMovement.resultInventoryValue;
              baseline.rawInventoryValue = stockMovement.resultRawInventoryValue;
              baseline.averageUnitCost = baseline.quantityOnHand === 0
                ? 0 : roundInventoryAverage(baseline.inventoryValue / baseline.quantityOnHand);
              baseline.cachedAtUtc = new Date().toISOString();
              baselineStore.put(baseline);
            }
            stockMovement.state = "acknowledged";
            stockMovement.serverSequence = acknowledgement.sequence;
            stockMovement.acknowledgedAtUtc = new Date().toISOString();
            stockMovementStore.put(stockMovement);
          };
        };
      }

      if (operation.operationType === "customer.profile.created"
        || operation.operationType === "customer.profile.updated"
        || operation.operationType === "customer.profile.deactivated") {
        let payload;
        try { payload = JSON.parse(operation.payloadJson); } catch { payload = null; }
        if (typeof payload?.customerId === "string") {
          const customerRequest = customerStore.get([operation.tenantId, payload.customerId]);
          customerRequest.onsuccess = () => {
            const customer = customerRequest.result;
            if (!customer || customer.lastOperationId !== operation.operationId) return;
            if ((operation.operationType === "customer.profile.updated"
                || operation.operationType === "customer.profile.deactivated")
              && (!Number.isSafeInteger(payload.baseVersion)
                || customer.version !== payload.baseVersion
                || customer.pendingBaseVersion !== payload.baseVersion)) {
              integrityError = new Error("Customer projection version no longer matches the acknowledged update precondition.");
              writeTransaction.abort();
              return;
            }
            customer.syncStatus = "synced";
            customer.serverSequence = acknowledgement.sequence;
            customer.updatedAtUtc = new Date().toISOString();
            if (operation.operationType === "customer.profile.updated"
              || operation.operationType === "customer.profile.deactivated") {
              customer.version = payload.baseVersion + 1;
              delete customer.pendingBaseVersion;
            }
            if (operation.operationType === "customer.profile.deactivated")
              customer.isActive = false;
            customerStore.put(customer);
          };
        }
      }
    }
    try { await writeDone; }
    catch (error) { if (integrityError) throw integrityError; throw error; }
    if (typeof window !== "undefined")
      window.dispatchEvent(new Event("mg-customer-sync-updated"));
  }

  async getPendingCount(tenantId, branchId = null, allowTenantWide = false, deviceId) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    if (typeof allowTenantWide !== "boolean")
      throw new TypeError("allowTenantWide must be a boolean.");
    if (typeof deviceId !== "string" || !deviceId.trim())
      throw new TypeError("deviceId is required to count the authenticated device outbox.");
    if (branchId === null && !allowTenantWide) return 0;

    const database = await openDatabase();
    const transaction = database.transaction(OPERATIONS_STORE, "readonly");
    const done = transactionDone(transaction);
    const index = transaction.objectStore(OPERATIONS_STORE).index("byTenantState");
    const count = await new Promise((resolve, reject) => {
      let matching = 0;
      const request = index.openCursor(IDBKeyRange.only([tenantId, "pending"]));
      request.onerror = () => reject(request.error || new Error("Could not count pending sync operations."));
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) {
          resolve(matching);
          return;
        }
        const operation = cursor.value;
        if (isPendingOperationVisible(operation, branchId, allowTenantWide, deviceId))
          matching += 1;
        cursor.continue();
      };
    });
    await done;
    return matching;
  }

  async applyInventoryAndAcknowledge({ tenantId, sequence, operation, payload }) {
    if (typeof operation.branchId !== "string" || !operation.branchId.trim())
      throw new TypeError("Inventory journal operations must include a branch.");
    if (typeof payload.productId !== "string" || !payload.productId.trim()
      || !Number.isInteger(Number(payload.type)) || Number(payload.type) < 1 || Number(payload.type) > 7
      || !Number.isFinite(Number(payload.quantity)) || Number(payload.quantity) <= 0
      || !Number.isFinite(Number(payload.baseQuantityOnHand)) || Number(payload.baseQuantityOnHand) < 0
      || !Number.isFinite(Number(payload.baseInventoryValue)) || Number(payload.baseInventoryValue) < 0)
      throw new TypeError("Incoming inventory movement payload is invalid.");
    const type = Number(payload.type);
    const inbound = [1, 5, 6].includes(type);
    if (inbound && (payload.unitCost == null || !Number.isFinite(Number(payload.unitCost)) || Number(payload.unitCost) < 0))
      throw new TypeError("Incoming inventory receipt requires a valid unit cost.");

    const database = await openDatabase();
    const transaction = database.transaction(
      [INBOX_STORE, INVENTORY_BASELINES_STORE, STOCK_MOVEMENTS_STORE, OPERATIONS_STORE],
      "readwrite"
    );
    const done = transactionDone(transaction);
    const inboxStore = transaction.objectStore(INBOX_STORE);
    const baselineStore = transaction.objectStore(INVENTORY_BASELINES_STORE);
    const stockMovementStore = transaction.objectStore(STOCK_MOVEMENTS_STORE);
    const operationsStore = transaction.objectStore(OPERATIONS_STORE);
    const inboxRequest = inboxStore.get([tenantId, sequence]);
    const baselineRequest = baselineStore.get([tenantId, operation.branchId, payload.productId]);
    const localMovementRequest = stockMovementStore.get([
      tenantId, operation.branchId, operation.operationId
    ]);
    const localOperationRequest = operationsStore.get(operation.operationId);
    let inboxRow;
    let baseline;
    let localMovement;
    let localOperation;
    let ready = 0;
    let integrityError = null;
    const commit = () => {
      if (++ready !== 4) return;
      if (!inboxRow) {
        integrityError = new Error("Incoming inventory inbox row disappeared before projection commit.");
        transaction.abort();
        return;
      }
      if (!baseline) {
        integrityError = new Error("Inventory baseline is not cached for this branch; refresh stock before applying inbound movements.");
        transaction.abort();
        return;
      }
      const baseQuantity = Number(payload.baseQuantityOnHand);
      const baseValue = roundInventoryValue(Number(payload.baseInventoryValue));
      const now = new Date().toISOString();

      if (localMovement && localMovement.state === "acknowledged") {
        // The originating device already advanced its baseline when the push was
        // acknowledged. Its journal echo only advances the inbox cursor.
      } else {
        if (Number(baseline.quantityOnHand) !== baseQuantity
          || Number(baseline.inventoryValue) !== baseValue) {
          integrityError = new Error("Inventory baseline changed before inbound movement projection; operator reconciliation is required.");
          transaction.abort();
          return;
        }

        const balance = {
          quantity: Number(baseline.quantityOnHand),
          value: Number(baseline.rawInventoryValue ?? baseline.inventoryValue)
        };
        const effectiveUnitCost = inbound
          ? Number(payload.unitCost)
          : (baseQuantity === 0 ? 0 : roundInventoryAverage(baseValue / baseQuantity));
        applyLocalStockMovement(balance, {
          type, quantity: Number(payload.quantity), unitCost: effectiveUnitCost
        });
        const resultInventoryValue = roundInventoryValue(balance.value);

        if (localMovement) {
          localMovement.state = "acknowledged";
          localMovement.serverSequence = sequence;
          localMovement.acknowledgedAtUtc = now;
          localMovement.resultQuantityOnHand = balance.quantity;
          localMovement.resultInventoryValue = resultInventoryValue;
          localMovement.resultRawInventoryValue = balance.value;
          stockMovementStore.put(localMovement);
        } else {
          stockMovementStore.add({
            tenantId, branchId: operation.branchId, productId: payload.productId,
            operationId: operation.operationId, type, quantity: Number(payload.quantity),
            unitCost: effectiveUnitCost, reference: payload.reference || null,
            baseQuantityOnHand: baseQuantity, baseInventoryValue: baseValue,
            resultQuantityOnHand: balance.quantity, resultInventoryValue,
            resultRawInventoryValue: balance.value,
            state: "acknowledged", createdAtUtc: operation.occurredAtUtc || now,
            acknowledgedAtUtc: now, serverSequence: sequence
          });
        }

        baseline.quantityOnHand = balance.quantity;
        baseline.inventoryValue = resultInventoryValue;
        baseline.rawInventoryValue = balance.value;
        baseline.averageUnitCost = balance.quantity === 0
          ? 0 : roundInventoryAverage(resultInventoryValue / balance.quantity);
        baseline.cachedAtUtc = now;
        baselineStore.put(baseline);
      }

      if (localOperation && localOperation.state !== "acknowledged") {
        localOperation.state = "acknowledged";
        localOperation.serverSequence = sequence;
        localOperation.acknowledgedAtUtc = now;
        operationsStore.put(localOperation);
      }
      inboxRow.applied = true;
      inboxRow.appliedIndex = 1;
      inboxRow.appliedAtUtc = now;
      inboxStore.put(inboxRow);
    };
    inboxRequest.onsuccess = () => { inboxRow = inboxRequest.result; commit(); };
    baselineRequest.onsuccess = () => { baseline = baselineRequest.result; commit(); };
    localMovementRequest.onsuccess = () => { localMovement = localMovementRequest.result; commit(); };
    localOperationRequest.onsuccess = () => { localOperation = localOperationRequest.result; commit(); };
    try { await done; }
    catch (error) { if (integrityError) throw integrityError; throw error; }
    if (typeof window !== "undefined")
      window.dispatchEvent(new Event("mg-inventory-sync-updated"));
  }

  async storeIncomingPage(tenantId, operations, nextSequence, branchId = null) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    if (!Array.isArray(operations)) throw new TypeError("operations must be an array.");
    if (!Number.isSafeInteger(nextSequence) || nextSequence < 0)
      throw new TypeError("nextSequence must be a non-negative safe integer.");

    let previousSequence = await this.getCursor(tenantId, branchId);
    if (nextSequence < previousSequence)
      throw new Error("Incoming sync cursor cannot move backwards.");

    for (const operation of operations) {
      if (!operation || typeof operation !== "object" || Array.isArray(operation)
        || !Number.isSafeInteger(operation.sequence) || operation.sequence <= previousSequence
        || typeof operation.operationId !== "string" || !operation.operationId.trim()
        || typeof operation.operationType !== "string" || !operation.operationType.trim()
        || (operation.branchId != null && (typeof operation.branchId !== "string" || !operation.branchId.trim()))
        || typeof operation.payloadJson !== "string"
        || new TextEncoder().encode(operation.payloadJson).byteLength > 64 * 1024
        || typeof operation.payloadSha256 !== "string" || !/^[0-9a-f]{64}$/i.test(operation.payloadSha256)
        || !Number.isInteger(operation.schemaVersion) || operation.schemaVersion < 1)
        throw new TypeError("Incoming operation envelope is invalid.");
      if (branchId !== null && operation.branchId != null && operation.branchId !== branchId)
        throw new Error("Incoming operation does not belong to the active branch.");
      if (!isJsonObjectPayload(operation.payloadJson))
        throw new TypeError("Incoming operation payload must be a JSON object.");
      const actualHash = await sha256(operation.payloadJson);
      if (actualHash !== operation.payloadSha256.toUpperCase())
        throw new Error("Incoming operation payload integrity check failed.");
      previousSequence = operation.sequence;
    }

    // The server may advance across journal rows hidden by branch permissions.
    // The cursor therefore represents the scanned journal head, not the last visible row.
    if (nextSequence < previousSequence)
      throw new Error("Incoming sync cursor cannot precede the last received operation.");

    const database = await openDatabase();
    const transaction = database.transaction([INBOX_STORE, META_STORE], "readwrite");
    const done = transactionDone(transaction);
    const inbox = transaction.objectStore(INBOX_STORE);
    const metadata = transaction.objectStore(META_STORE);
    const cursorKey = `cursor:${tenantId}:${branchId || "all"}`;
    const cursorRequest = metadata.get(cursorKey);
    let integrityConflict = null;
    cursorRequest.onsuccess = () => {
      const storedCursor = Number.isSafeInteger(cursorRequest.result?.value) ? cursorRequest.result.value : 0;
      if (nextSequence < storedCursor) {
        transaction.abort();
        return;
      }
      for (const operation of operations) {
        const existingRequest = inbox.get([tenantId, operation.sequence]);
        existingRequest.onsuccess = () => {
          const existing = existingRequest.result;
          if (existing) {
            // A tenant-wide operation can be encountered from more than one branch
            // cursor. Preserve its state only when the immutable envelope is identical.
            if (!isSameIncomingEnvelope(existing, { ...operation, tenantId })) {
              integrityConflict = new Error("A journal sequence was reused with a different incoming operation envelope.");
              transaction.abort();
            }
            return;
          }
          inbox.add({
            ...operation,
            tenantId,
            applied: false,
            appliedIndex: 0,
            appliedAtUtc: null,
            receivedLocallyAtUtc: new Date().toISOString()
          });
        };
      }
      metadata.put({ key: cursorKey, value: nextSequence });
    };
    try {
      await done;
    } catch (error) {
      if (integrityConflict) throw integrityConflict;
      throw error;
    }
    return operations.length;
  }

  async getInboxCount(tenantId, branchId = null, allowTenantWide = false) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    if (typeof allowTenantWide !== "boolean")
      throw new TypeError("allowTenantWide must be a boolean.");
    if (branchId === null && !allowTenantWide) return 0;
    const database = await openDatabase();
    const transaction = database.transaction(INBOX_STORE, "readonly");
    const done = transactionDone(transaction);
    const index = transaction.objectStore(INBOX_STORE).index("byTenantApplied");

    const count = await new Promise((resolve, reject) => {
      let matching = 0;
      const request = index.openCursor(IDBKeyRange.only([tenantId, 0]));
      request.onerror = () => reject(request.error || new Error("Could not count incoming sync operations."));
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) {
          resolve(matching);
          return;
        }
        if (isIncomingRowVisible(cursor.value, branchId, allowTenantWide))
          matching += 1;
        cursor.continue();
      };
    });
    await done;
    return count;
  }

  async getUnappliedIncoming(tenantId, limit = 100, branchId = null, allowTenantWide = false) {
    if (typeof tenantId !== "string" || !tenantId.trim())
      throw new TypeError("tenantId is required.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim()))
      throw new TypeError("branchId must be a non-empty string or null.");
    if (typeof allowTenantWide !== "boolean")
      throw new TypeError("allowTenantWide must be a boolean.");
    if (branchId === null && !allowTenantWide) return [];
    const boundedLimit = Number.isInteger(limit) ? Math.max(1, Math.min(limit, 200)) : 100;
    const database = await openDatabase();
    const transaction = database.transaction(INBOX_STORE, "readonly");
    const done = transactionDone(transaction);
    const index = transaction.objectStore(INBOX_STORE).index("byTenantApplied");
    const range = IDBKeyRange.only([tenantId, 0]);

    const rows = await new Promise((resolve, reject) => {
      const matching = [];
      const request = index.openCursor(range);
      request.onerror = () => reject(request.error || new Error("Could not read unapplied incoming operations."));
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor || matching.length >= boundedLimit) {
          resolve(matching);
          return;
        }
        const row = cursor.value;
        if (isIncomingRowVisible(row, branchId, allowTenantWide))
          matching.push(row);
        cursor.continue();
      };
    });
    await done;
    return rows.sort((a, b) => a.sequence - b.sequence);
  }

  async markIncomingApplied(tenantId, sequences) {
    if (!Array.isArray(sequences) || sequences.length === 0) return;
    const database = await openDatabase();
    const readTransaction = database.transaction(INBOX_STORE, "readonly");
    const readDone = transactionDone(readTransaction);
    const readStore = readTransaction.objectStore(INBOX_STORE);
    const existing = await Promise.all(sequences.map(sequence =>
      requestResult(readStore.get([tenantId, sequence]))));
    await readDone;

    const writeTransaction = database.transaction(INBOX_STORE, "readwrite");
    const writeDone = transactionDone(writeTransaction);
    const writeStore = writeTransaction.objectStore(INBOX_STORE);
    existing.forEach(row => {
      if (!row) return;
      row.applied = true;
      row.appliedIndex = 1;
      row.appliedAtUtc = new Date().toISOString();
      writeStore.put(row);
    });
    await writeDone;
  }


  async resolveInventoryConflictFromServer({ tenantId, branchId, sequence, stockRow }) {
    if (typeof tenantId !== "string" || !tenantId.trim()
      || typeof branchId !== "string" || !branchId.trim()
      || !Number.isSafeInteger(sequence) || sequence < 1)
      throw new TypeError("Tenant, branch and conflict sequence are required.");
    if (!stockRow || stockRow.branchId !== branchId || typeof stockRow.productId !== "string"
      || !Number.isFinite(Number(stockRow.quantityOnHand))
      || !Number.isFinite(Number(stockRow.inventoryValue)) || Number(stockRow.inventoryValue) < 0
      || !Number.isFinite(Number(stockRow.rawInventoryValue ?? stockRow.inventoryValue))
      || Number(stockRow.rawInventoryValue ?? stockRow.inventoryValue) < 0
      || Number(stockRow.quantityOnHand) < 0)
      throw new TypeError("A current server stock snapshot for the conflicted branch is required.");

    const database = await openDatabase();
    const transaction = database.transaction(
      [INBOX_STORE, CONFLICTS_STORE, INVENTORY_BASELINES_STORE, STOCK_MOVEMENTS_STORE, META_STORE],
      "readwrite"
    );
    const done = transactionDone(transaction);
    const inboxStore = transaction.objectStore(INBOX_STORE);
    const conflictStore = transaction.objectStore(CONFLICTS_STORE);
    const baselineStore = transaction.objectStore(INVENTORY_BASELINES_STORE);
    const stockMovementStore = transaction.objectStore(STOCK_MOVEMENTS_STORE);
    const metadataStore = transaction.objectStore(META_STORE);
    const inboxRequest = inboxStore.get([tenantId, sequence]);
    const conflictRequest = conflictStore.get([tenantId, sequence]);
    const pendingRequest = stockMovementStore.index("byTenantBranchProduct")
      .getAll(IDBKeyRange.only([tenantId, branchId, stockRow.productId]));
    const cursorKey = `cursor:${tenantId}:${branchId}`;
    const cursorRequest = metadataStore.get(cursorKey);
    const [inboxRow, conflict, localMovements, cursorRow] = await Promise.all([
      requestResult(inboxRequest), requestResult(conflictRequest),
      requestResult(pendingRequest), requestResult(cursorRequest)
    ]);
    if (!inboxRow || inboxRow.applied || !conflict || conflict.status !== "open"
      || conflict.operationType !== "inventory.stock.movement.created"
      || conflict.reason !== "stale-inventory-base-balance") {
      transaction.abort();
      await done.catch(() => {});
      throw new Error("این تعارض دیگر قابل تطبیق با موجودی سرور نیست.");
    }
    let conflictPayload;
    try { conflictPayload = JSON.parse(inboxRow.payloadJson); }
    catch {
      transaction.abort();
      await done.catch(() => {});
      throw new Error("داده تعارض انبار قابل خواندن نیست.");
    }
    if (conflictPayload?.productId !== stockRow.productId) {
      transaction.abort();
      await done.catch(() => {});
      throw new Error("کالای انتخاب‌شده با تعارض ثبت‌شده مطابقت ندارد.");
    }
    const pending = localMovements.filter(row => row.state === "pending");
    if (pending.length > 0) {
      transaction.abort();
      await done.catch(() => {});
      throw new Error("برای این کالا گردش انبار محلیِ در انتظار همگام‌سازی وجود دارد؛ ابتدا آن‌ها را تعیین تکلیف کنید.");
    }
    const currentCursor = Number.isSafeInteger(cursorRow?.value) ? cursorRow.value : 0;
    if (currentCursor !== sequence - 1) {
      transaction.abort();
      await done.catch(() => {});
      throw new Error("ترتیب همگام‌سازی اجازه عبور از تعارض‌های قبلی را نمی‌دهد.");
    }

    const now = new Date().toISOString();
    baselineStore.put({
      tenantId, branchId, productId: stockRow.productId,
      quantityOnHand: Number(stockRow.quantityOnHand),
      inventoryValue: Number(stockRow.inventoryValue),
      rawInventoryValue: Number(stockRow.rawInventoryValue ?? stockRow.inventoryValue),
      averageUnitCost: Number(stockRow.averageUnitCost || 0),
      cachedAtUtc: now
    });
    inboxRow.applied = true;
    inboxRow.appliedIndex = 1;
    inboxRow.appliedAtUtc = now;
    inboxStore.put(inboxRow);
    conflict.status = "resolved";
    conflict.resolution = "authoritative-server-stock-snapshot";
    conflict.resolvedAtUtc = now;
    conflict.updatedAtUtc = now;
    conflictStore.put(conflict);
    metadataStore.put({ key: cursorKey, value: sequence });
    await done;
    if (typeof window !== "undefined")
      window.dispatchEvent(new Event("mg-inventory-sync-updated"));
    return { sequence, productId: stockRow.productId, resolvedAtUtc: now };
  }

  async getOpenConflicts(tenantId, branchId = null, allowTenantWide = false, limit = 100) {
    if (typeof tenantId !== "string" || !tenantId.trim()) throw new TypeError("tenantId is required.");
    if (branchId !== null && (typeof branchId !== "string" || !branchId.trim())) throw new TypeError("branchId must be a non-empty string or null.");
    if (typeof allowTenantWide !== "boolean") throw new TypeError("allowTenantWide must be a boolean.");
    if (branchId === null && !allowTenantWide) return [];
    const boundedLimit = Number.isInteger(limit) ? Math.max(1, Math.min(limit, 200)) : 100;
    const database = await openDatabase();
    const transaction = database.transaction(CONFLICTS_STORE, "readonly");
    const done = transactionDone(transaction);
    const index = transaction.objectStore(CONFLICTS_STORE).index("byTenantStatusCreatedAt");
    const rows = await new Promise((resolve, reject) => {
      const matching = [];
      const request = index.openCursor(IDBKeyRange.bound([tenantId, "open", ""], [tenantId, "open", "\uffff"]), "prev");
      request.onerror = () => reject(request.error || new Error("Could not read unresolved sync conflicts."));
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor || matching.length >= boundedLimit) { resolve(matching); return; }
        if (isConflictVisible(cursor.value, branchId, allowTenantWide)) matching.push(cursor.value);
        cursor.continue();
      };
    });
    await done;
    return rows;
  }

  async getCursor(tenantId, branchId = null) {
    const database = await openDatabase();
    const transaction = database.transaction(META_STORE, "readonly");
    const done = transactionDone(transaction);
    const row = await requestResult(transaction.objectStore(META_STORE).get(`cursor:${tenantId}:${branchId || "all"}`));
    await done;
    return Number.isSafeInteger(row?.value) && row.value >= 0 ? row.value : 0;
  }

  async setCursor(tenantId, sequence, branchId = null) {
    if (!tenantId) throw new TypeError("tenantId is required.");
    if (!Number.isSafeInteger(sequence) || sequence < 0)
      throw new TypeError("Sync cursor must be a non-negative safe integer.");
    const database = await openDatabase();
    const transaction = database.transaction(META_STORE, "readwrite");
    const done = transactionDone(transaction);
    const metadata = transaction.objectStore(META_STORE);
    const key = `cursor:${tenantId}:${branchId || "all"}`;
    const request = metadata.get(key);
    request.onsuccess = () => {
      const current = Number.isSafeInteger(request.result?.value) && request.result.value >= 0
        ? request.result.value : 0;
      if (sequence < current) {
        transaction.abort();
        return;
      }
      metadata.put({ key, value: sequence });
    };
    await done;
  }
}

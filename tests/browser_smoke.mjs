import assert from "node:assert/strict";
import { chromium } from "playwright";

const errors = [];
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("http://127.0.0.1:8080", { waitUntil: "networkidle" });
  await page.locator("#authForm").waitFor({ state: "visible" });
  await page.locator('#authForm input[name="businessName"]').fill("MG Browser QA");
  await page.locator('#authForm input[name="username"]').fill("browser-owner");
  await page.locator('#authForm input[name="password"]').fill("browser-owner-password-2026");
  await page.locator('#authForm button[type="submit"]').click();
  await page.locator("#authGate").waitFor({ state: "hidden" });
  await page.locator("#pageTitle").waitFor({ state: "visible" });
  assert.match(await page.locator("#pageTitle").innerText(), /نمای کلی کسب‌وکار/);

  await page.locator('#navigation [data-page="accounting"]').click();
  await page.locator("#accountCreateForm").waitFor({ state: "visible" });
  await page.locator('#accountCreateForm input[name="code"]').fill("1700");
  await page.locator('#accountCreateForm input[name="name"]').fill("حساب آزمایشی مرورگر");
  await page.locator('#accountCreateForm select[name="accountType"]').selectOption("asset");
  await page.locator('#accountCreateForm button[type="submit"]').click();
  await page.locator("#modulePage .module-table").last().getByText("حساب آزمایشی مرورگر", { exact: true }).waitFor({ state: "visible" });

  await page.locator('#navigation [data-page="overview"]').click();
  await page.locator("#addTransaction").click();
  await page.locator('#transactionForm input[name="title"]').fill("درآمد آزمون مرورگر");
  await page.locator('#transactionForm input[name="amount"]').fill("25000");
  await page.locator('#transactionForm select[name="kind"]').selectOption("income");
  const transactionResponsePromise = page.waitForResponse(response =>
    response.url().endsWith("/api/transactions") && response.request().method() === "POST"
  );
  await page.locator("#transactionForm button.primary").click();
  const transactionResponse = await transactionResponsePromise;
  const transactionBody = await transactionResponse.json();
  assert.equal(transactionResponse.status(), 201, "transaction API response: " + JSON.stringify(transactionBody));
  await page.waitForTimeout(500);
  if (await page.locator("#modal").isVisible()) {
    console.log("Transaction UI diagnostic:", {
      errors,
      toast: await page.locator("#toastRegion").innerText(),
      modalClass: await page.locator("#modal").getAttribute("class")
    });
  }
  await page.locator("#modal").waitFor({ state: "hidden", timeout: 5000 });
  await page.locator("#transactions").getByText("درآمد آزمون مرورگر", { exact: true }).waitFor({ state: "visible" });

  await page.locator('#navigation [data-page="inventory"]').click();
  await page.locator("#productForm").waitFor({ state: "visible" });
  await page.locator('#productForm input[name="name"]').fill("کالای آزمون کافه");
  await page.locator('#productForm input[name="sellPrice"]').fill("45000");
  await page.locator('#productForm input[name="costPrice"]').fill("20000");
  const productResponsePromise = page.waitForResponse(response =>
    response.url().endsWith("/api/products") && response.request().method() === "POST"
  );
  await page.locator("#productForm button.primary").click();
  const productResponse = await productResponsePromise;
  const productBody = await productResponse.json();
  assert.equal(productResponse.status(), 201, "product API response: " + JSON.stringify(productBody));
  const stockResult = await page.evaluate(async productId => {
    const token = sessionStorage.getItem("mg-business.session");
    const response = await fetch("/api/inventory/movements", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
      body: JSON.stringify({
        id: "browser-stock-intake-001",
        productId,
        movementType: "purchase",
        quantityDelta: 5,
        unitCost: 20000,
        note: "Browser E2E seed stock"
      })
    });
    return { status: response.status, body: await response.json() };
  }, productBody.id);
  assert.equal(stockResult.status, 201, "inventory API response: " + JSON.stringify(stockResult.body));

  await page.locator('#navigation [data-page="cafe"]').click();
  await page.locator("#addCartItem").waitFor({ state: "visible" });
  await page.locator("#cafeProduct").selectOption(productBody.id);
  await page.locator("#addCartItem").click();
  await page.waitForFunction(() => {
    const button = document.querySelector("#submitOrder");
    return Boolean(button && !button.disabled);
  });
  const orderResponsePromise = page.waitForResponse(response =>
    response.url().endsWith("/api/orders") && response.request().method() === "POST"
  );
  await page.locator("#submitOrder").click();
  const orderResponse = await orderResponsePromise;
  const orderBody = await orderResponse.json();
  assert.equal(orderResponse.status(), 201, "order API response: " + JSON.stringify(orderBody));
  await page.locator("#modulePage").getByText(orderBody.orderNumber, { exact: true }).waitFor({ state: "visible" });

  // Exercise the complete GameNet lifecycle through the real browser UI.
  await page.locator('#navigation [data-page="gamenet"]').click();
  await page.locator("#deviceForm").waitFor({ state: "visible" });
  await page.locator('#deviceForm input[name="name"]').fill("E2E-PC-01");
  await page.locator('#deviceForm input[name="hourlyRate"]').fill("36000");
  const deviceResponsePromise = page.waitForResponse(response =>
    response.url().endsWith("/api/gamenet/devices") && response.request().method() === "POST"
  );
  await page.locator("#deviceForm button[type=submit]").click();
  const deviceResponse = await deviceResponsePromise;
  const deviceBody = await deviceResponse.json();
  assert.equal(deviceResponse.status(), 201, "GameNet device API response: " + JSON.stringify(deviceBody));
  const deviceCard = page.locator('[data-device-card]').filter({ hasText: "E2E-PC-01" });
  await deviceCard.waitFor({ state: "visible" });
  const startSessionPromise = page.waitForResponse(response =>
    response.url().endsWith("/api/gamenet/sessions") && response.request().method() === "POST"
  );
  await deviceCard.locator("[data-start-game]").click();
  const startSessionResponse = await startSessionPromise;
  const startSessionBody = await startSessionResponse.json();
  assert.equal(startSessionResponse.status(), 201, "GameNet session start response: " + JSON.stringify(startSessionBody));
  const activeDeviceCard = page.locator('[data-device-card]').filter({ hasText: "E2E-PC-01" });
  const gameAdjustForm = activeDeviceCard.locator(".game-adjust-form");
  await gameAdjustForm.locator("xpath=..").locator("summary").click();
  await gameAdjustForm.waitFor({ state: "visible" });
  await activeDeviceCard.locator('.game-adjust-form select[name="deviceId"]').selectOption({ label: "E2E-PC-01" });
  await activeDeviceCard.locator('.game-adjust-form input[name="playersCount"]').fill("2");
  const adjustSessionPromise = page.waitForResponse(response =>
    response.url().endsWith("/api/gamenet/sessions/" + startSessionBody.id + "/adjust") && response.request().method() === "POST"
  );
  await activeDeviceCard.locator(".game-adjust-form button[type=submit]").click();
  const adjustSessionResponse = await adjustSessionPromise;
  assert.equal(adjustSessionResponse.status(), 200, "GameNet session adjustment must succeed");
  const endSessionPromise = page.waitForResponse(response =>
    response.url().endsWith("/api/gamenet/sessions/" + startSessionBody.id + "/end") && response.request().method() === "POST"
  );
  await activeDeviceCard.locator("[data-end-game]").click();
  const endSessionResponse = await endSessionPromise;
  const endSessionBody = await endSessionResponse.json();
  assert.equal(endSessionResponse.status(), 200, "GameNet session settlement response: " + JSON.stringify(endSessionBody));
  assert.equal(endSessionBody.paymentMethod, "cash");
  await page.locator('[data-device-card]').filter({ hasText: "E2E-PC-01" }).locator(".device-card-head b", { hasText: "E2E-PC-01" }).waitFor({ state: "visible" });

  await page.locator('#navigation [data-page="accounting"]').click();
  const accountingToday = await page.evaluate(() => {
    const now = new Date();
    return [now.getFullYear(), String(now.getMonth() + 1).padStart(2, "0"), String(now.getDate()).padStart(2, "0")].join("-");
  });
  const accountingReportRequests = [];
  page.on("request", request => {
    const url = new URL(request.url());
    if (["/api/accounting/profit-loss", "/api/accounting/journal", "/api/accounting/cash-flow"].includes(url.pathname)) {
      accountingReportRequests.push(url);
    }
  });
  await page.locator('#accountingDateFilter input[name="fromDate"]').fill(accountingToday);
  await page.locator('#accountingDateFilter input[name="toDate"]').fill(accountingToday);
  await page.locator('#accountingDateFilter button[type="submit"]').click();
  await page.locator('[data-accounting-export]').waitFor({ state: "visible" });
  for (const path of ["/api/accounting/profit-loss", "/api/accounting/journal", "/api/accounting/cash-flow"]) {
    assert.ok(accountingReportRequests.some(url => url.pathname === path && url.searchParams.get("fromDate") === accountingToday && url.searchParams.get("toDate") === accountingToday),
      "accounting date range must reach " + path);
  }
  await page.locator('[data-accounting-export]').waitFor({ state: "visible" });
  const accountingDownloadPromise = page.waitForEvent("download");
  await page.locator('[data-accounting-export]').click();
  const accountingDownload = await accountingDownloadPromise;
  const accountingPath = await accountingDownload.path();
  const accountingBytes = await (await import("node:fs/promises")).readFile(accountingPath);
  assert.deepEqual([...accountingBytes.subarray(0, 3)], [0xef, 0xbb, 0xbf], "accounting CSV must include a UTF-8 BOM");
  const accountingCsv = accountingBytes.toString("utf8");
  assert.match(accountingCsv, /گزارش حسابداری MG/);
  assert.match(accountingCsv, /جزئیات گردش نقدی/);
  assert.match(accountingCsv, /دفتر روزنامه/);
  assert.match(accountingCsv, /تراز آزمایشی/);
  assert.match(accountingCsv, /درآمد آزمون مرورگر/);

  await page.locator('#navigation [data-page="reports"]').click();
  await page.locator('[data-reports-export]').waitFor({ state: "visible" });
  const managementDownloadPromise = page.waitForEvent("download");
  await page.locator('[data-reports-export]').click();
  const managementDownload = await managementDownloadPromise;
  const managementPath = await managementDownload.path();
  const managementBytes = await (await import("node:fs/promises")).readFile(managementPath);
  assert.deepEqual([...managementBytes.subarray(0, 3)], [0xef, 0xbb, 0xbf], "management CSV must include a UTF-8 BOM");
  const managementCsv = managementBytes.toString("utf8");
  assert.match(managementCsv, /گزارش مدیریتی MG/);
  assert.match(managementCsv, /فروش به تفکیک محصول/);
  assert.match(managementCsv, /سفارش‌ها/);
  assert.match(managementCsv, /کالای آزمون کافه/);

  await page.locator('#navigation [data-page="settings"]').click();
  await page.locator("#userForm").waitFor({ state: "visible" });
  await page.locator('#userForm input[name="username"]').fill("browser-cashier");
  await page.locator('#userForm input[name="password"]').fill("browser-cashier-password-2026");
  await page.locator('#userForm select[name="role"]').selectOption("cashier");
  const userCreatePromise = page.waitForResponse(response =>
    response.url().endsWith("/api/users") && response.request().method() === "POST"
  );
  await page.locator("#userForm button[type=submit]").click();
  const userCreateResponse = await userCreatePromise;
  const userCreateBody = await userCreateResponse.json();
  assert.equal(userCreateResponse.status(), 201, "user creation response: " + JSON.stringify(userCreateBody));
  const createdUserId = userCreateBody.id;
  assert.ok(createdUserId, "created user response must include its id");
  const createdUserRow = page.locator('.module-table-row.users').filter({ hasText: "browser-cashier" });
  await createdUserRow.waitFor({ state: "visible" });
  await createdUserRow.locator('[data-user-change-role]').click();
  await createdUserRow.locator('form.userRoleForm select[name="role"]').selectOption("viewer");
  const roleChangePromise = page.waitForResponse(response =>
    response.url().endsWith("/api/users/" + createdUserId + "/role") && response.request().method() === "POST"
  );
  await createdUserRow.locator('form.userRoleForm button[type="submit"]').click();
  const roleChangeResponse = await roleChangePromise;
  const roleChangeBody = await roleChangeResponse.json();
  assert.equal(roleChangeResponse.status(), 200, "role change response: " + JSON.stringify(roleChangeBody));
  assert.equal(roleChangeBody.changed, true, "role change must report a real change");
  await page.locator('.module-table-row.users').filter({ hasText: "browser-cashier" }).getByText("فقط مشاهده", { exact: true }).waitFor({ state: "visible" });
  await page.locator("#modulePage").getByText("user.role.changed", { exact: false }).waitFor({ state: "visible" });
  const deactivateButton = page.locator('.module-table-row.users').filter({ hasText: "browser-cashier" }).locator('[data-user-deactivate]');
  const deactivationResponsePromise = page.waitForResponse(response =>
    response.url().endsWith("/api/users/" + createdUserId) && response.request().method() === "DELETE"
  );
  await deactivateButton.click();
  await page.locator('#confirmOverlay [data-confirm="yes"]').click();
  const deactivationResponse = await deactivationResponsePromise;
  const deactivationBody = await deactivationResponse.json();
  assert.equal(deactivationResponse.status(), 200, "user deactivation response: " + JSON.stringify(deactivationBody));
  await page.locator('.module-table-row.users').filter({ hasText: "browser-cashier" }).getByText("غیرفعال", { exact: true }).waitFor({ state: "visible" });

  assert.equal(errors.length, 0, "browser runtime errors: " + errors.join("; "));
  console.log("Browser E2E passed: setup, login, accounting, custom account creation, transaction posting, inventory intake, cafe POS order, and accounting/management CSV exports, and owner user-role management/deactivation.");
} finally {
  await browser.close();
}

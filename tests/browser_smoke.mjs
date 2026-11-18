import assert from "node:assert/strict";
import { chromium } from "playwright";

const errors = [];
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  page.on("console", message => { if (message.type() === "error") console.error("BROWSER CONSOLE:", message.text()); });
  page.on("pageerror", error => console.error("BROWSER PAGE ERROR:", error.stack || error.message));
  page.on("requestfailed", request => console.error("BROWSER REQUEST FAILED:", request.method(), request.url(), request.failure()?.errorText));
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
  await page.locator("#modulePage .module-table-row.trial").filter({ hasText: "1700" }).first().waitFor({ state: "visible" });
  const unusedAccountRow = page.locator("#modulePage .module-table-row.trial").filter({ hasText: "1700" });
  const accountDeactivationPromise = page.waitForResponse(response =>
    response.url().includes("/api/accounting/accounts/") && response.request().method() === "DELETE"
  );
  await unusedAccountRow.locator("[data-account-deactivate]").click();
  await page.locator('#confirmOverlay [data-confirm="yes"]').click();
  const accountDeactivationResponse = await accountDeactivationPromise;
  assert.equal(accountDeactivationResponse.status(), 200, "unused account deactivation must succeed");
  await page.locator("#accountCreateForm").waitFor({ state: "visible" });
  const activeAccountsAfterDeactivation = await page.evaluate(async () => {
    const token = sessionStorage.getItem("mg-business.session");
    const response = await fetch("/api/accounting/accounts", {
      headers: { Authorization: "Bearer " + token }
    });
    return { status: response.status, body: await response.json() };
  });
  assert.equal(activeAccountsAfterDeactivation.status, 200, "active account chart API must remain available");
  assert.equal(activeAccountsAfterDeactivation.body.items.some(account => account.code === "1700"), false,
    "deactivated account must be excluded from the active chart API");
  const accountChart = page.locator("#modulePage h3.section-caption")
    .filter({ hasText: "کدینگ حساب‌ها" })
    .locator("xpath=following-sibling::div[contains(concat(' ', normalize-space(@class), ' '), ' module-table ')][1]");
  await accountChart.waitFor({ state: "visible" });
  await page.waitForFunction(() => {
    const heading = [...document.querySelectorAll("#modulePage h3.section-caption")]
      .find(element => element.textContent.includes("کدینگ حساب‌ها"));
    const chart = heading?.nextElementSibling;
    return Boolean(chart && !chart.innerText.includes("1700"));
  });
  assert.equal(await accountChart.locator(".module-table-row.trial").filter({ hasText: "1700" }).count(), 0,
    "deactivated account must disappear from the active chart UI");

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
  await page.locator("#cashShiftOpenForm").waitFor({ state: "visible" });
  const shiftOpenPromise = page.waitForResponse(response =>
    response.url().endsWith("/api/cash-register/shifts/open") && response.request().method() === "POST"
  );
  await page.locator("#cashShiftOpenForm").evaluate(form => {
    const input = form.querySelector('input[name="openingCash"]');
    input.value = "1000";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    form.requestSubmit();
  });
  const shiftOpenResponse = await shiftOpenPromise;
  const shiftOpenBody = await shiftOpenResponse.json();
  assert.equal(shiftOpenResponse.status(), 201, "cash register open response: " + JSON.stringify(shiftOpenBody));
  assert.equal(shiftOpenBody.openingCash, 1000, "cash shift must persist the entered opening balance: " + JSON.stringify(shiftOpenBody));
  await page.locator("#cashShiftCloseForm").waitFor({ state: "visible" });
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
  await page.locator("#modulePage").getByText(orderBody.orderNumber, { exact: true }).first().waitFor({ state: "visible" });
  let kitchenAction = page.locator('[data-order-fulfillment="' + orderBody.id + '"]');
  await kitchenAction.waitFor({ state: "visible" });
  for (const nextStatus of ["preparing", "ready", "served"]) {
    const fulfillmentPromise = page.waitForResponse(response =>
      response.url().endsWith("/api/orders/" + orderBody.id + "/fulfillment") &&
      response.request().method() === "POST"
    );
    await kitchenAction.click();
    const fulfillmentResponse = await fulfillmentPromise;
    const fulfillmentBody = await fulfillmentResponse.json();
    assert.equal(fulfillmentResponse.status(), 200, "kitchen fulfillment transition: " + JSON.stringify(fulfillmentBody));
    assert.equal(fulfillmentBody.status, nextStatus);
    if (nextStatus !== "served") {
      kitchenAction = page.locator('[data-order-fulfillment="' + orderBody.id + '"]');
      await kitchenAction.waitFor({ state: "visible" });
      await page.waitForFunction(({ id, status }) => {
        const button = document.querySelector('[data-order-fulfillment="' + id + '"]');
        return Boolean(button && button.dataset.nextStatus === status);
      }, { id: orderBody.id, status: nextStatus === "preparing" ? "ready" : "served" });
    }
  }
  await page.locator('[data-order-fulfillment="' + orderBody.id + '"]').waitFor({ state: "detached" });
  const shiftState = await page.evaluate(async () => {
    const token = sessionStorage.getItem("mg-business.session");
    const response = await fetch("/api/cash-register/shifts", { headers: { Authorization: "Bearer " + token } });
    return { status: response.status, body: await response.json() };
  });
  assert.equal(shiftState.status, 200, "cash register status response: " + JSON.stringify(shiftState.body));
  assert.ok(shiftState.body.active, "cash shift should remain active after a cash order: " + JSON.stringify(shiftState.body));
  assert.equal(shiftState.body.active.openingCash, 1000, "active cash shift must retain its opening balance: " + JSON.stringify(shiftState.body.active));
  assert.equal(orderBody.total, 45000, "cash order total must match the seeded product price");
  assert.equal(shiftState.body.active.expectedCash, shiftState.body.active.openingCash + orderBody.total,
    "cash shift ledger balance must equal opening cash plus the cash order: " + JSON.stringify({shift:shiftState.body.active,order:orderBody}));
  await page.locator("#cashShiftCloseForm").waitFor({ state: "visible" });
  const shiftExpectedCash = shiftState.body.active.expectedCash;
  await page.locator('#cashShiftCloseForm input[name="countedCash"]').fill(String(shiftExpectedCash));
  const shiftClosePromise = page.waitForResponse(response =>
    response.url().includes("/api/cash-register/shifts/") && response.url().endsWith("/close") &&
    response.request().method() === "POST"
  );
  await page.locator("#cashShiftCloseForm button[type=submit]").click();
  await page.locator('#confirmOverlay [data-confirm="yes"]').click();
  const shiftCloseResponse = await shiftClosePromise;
  const shiftCloseBody = await shiftCloseResponse.json();
  assert.equal(shiftCloseResponse.status(), 200, "cash register close response: " + JSON.stringify(shiftCloseBody));
  assert.equal(shiftCloseBody.expectedCash, shiftExpectedCash, "cash shift expected balance must include the cash order only");
  assert.equal(shiftCloseBody.difference, 0, "cash shift should reconcile without a difference");

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
  await page.locator('#accountingDateFilter input[name="fromDate"]').fill(accountingToday);
  await page.locator('#accountingDateFilter input[name="toDate"]').fill(accountingToday);
  const reportPaths = ["/api/accounting/profit-loss", "/api/accounting/journal", "/api/accounting/cash-flow"];
  const reportResponses = reportPaths.map(path => page.waitForResponse(response =>
    new URL(response.url()).pathname === path
  ));
  await page.locator("#accountingDateFilter").evaluate(form => form.requestSubmit());
  const reportSettled = await Promise.allSettled(reportResponses);
  const missingReports = reportSettled.flatMap((result, index) => result.status === "rejected" ? [{ path: reportPaths[index], error: result.reason?.message || String(result.reason) }] : []);
  if (missingReports.length) throw new Error("Accounting report requests were not observed: " + JSON.stringify(missingReports));
  const reportResults = reportSettled.map(result => result.value);
  for (let index = 0; index < reportPaths.length; index++) {
    const reportUrl = new URL(reportResults[index].url());
    assert.equal(reportUrl.searchParams.get("fromDate"), accountingToday, "accounting report start date mismatch: " + reportPaths[index] + " " + reportUrl);
    assert.equal(reportUrl.searchParams.get("toDate"), accountingToday, "accounting report end date mismatch: " + reportPaths[index] + " " + reportUrl);
    assert.ok(reportResults[index].ok(), "accounting report request failed: " + reportPaths[index]);
  }
  await page.locator('[data-accounting-export]').waitFor({ state: "visible" });
  const accountLedgerCheck = await page.evaluate(async (reportDate) => {
    const token = sessionStorage.getItem("mg-business.session");
    const headers = { Authorization: "Bearer " + token };
    const chartResponse = await fetch("/api/accounting/accounts", { headers });
    const chart = await chartResponse.json();
    const cash = chart.items.find(account => account.code === "1000");
    if (!cash) return { status: chartResponse.status, missingCashAccount: true };
    const query = new URLSearchParams({ accountId: cash.id, fromDate: reportDate, toDate: reportDate });
    const response = await fetch("/api/accounting/account-ledger?" + query, { headers });
    const body = await response.json();
    const pageQuery = new URLSearchParams({ accountId: cash.id, fromDate: reportDate, toDate: reportDate, limit: "1", offset: "0" });
    const pageResponse = await fetch("/api/accounting/account-ledger?" + pageQuery, { headers });
    const firstPage = await pageResponse.json();
    let secondPage = null;
    if (firstPage.hasMore) {
      pageQuery.set("offset", String(firstPage.nextOffset));
      const nextResponse = await fetch("/api/accounting/account-ledger?" + pageQuery, { headers });
      secondPage = await nextResponse.json();
    }
    return { status: response.status, body, pageStatus: pageResponse.status, firstPage, secondPage };
  }, accountingToday);
  assert.equal(accountLedgerCheck.status, 200, "account-level ledger endpoint must work from the browser");
  assert.equal(accountLedgerCheck.body.account.code, "1000");
  assert.equal(accountLedgerCheck.body.periodDebit >= 0, true);
  assert.equal(accountLedgerCheck.body.periodCredit >= 0, true);
  assert.equal(accountLedgerCheck.body.closingBalance,
    accountLedgerCheck.body.openingBalance +
    accountLedgerCheck.body.periodDebit - accountLedgerCheck.body.periodCredit,
    "debit-normal cash account closing balance must reconcile to opening plus period movement");
  assert.equal(accountLedgerCheck.pageStatus, 200, "account ledger page request must succeed");
  assert.equal(accountLedgerCheck.firstPage.items.length <= 1, true, "account ledger page size must be enforced");
  assert.equal(accountLedgerCheck.firstPage.totalItems >= accountLedgerCheck.firstPage.items.length, true);
  if (accountLedgerCheck.secondPage) {
    assert.equal(accountLedgerCheck.secondPage.offset, accountLedgerCheck.firstPage.nextOffset);
    assert.equal(accountLedgerCheck.firstPage.items[0].runningBalance,
      accountLedgerCheck.secondPage.items[0].runningBalance +
      accountLedgerCheck.firstPage.items[0].debit -
      accountLedgerCheck.firstPage.items[0].credit,
      "running balance must remain continuous across newest-first pages");
  }
  await page.locator("#accountLedgerFilter select[name=accountId]").selectOption(accountLedgerCheck.body.account.id);
  await page.locator("#accountLedgerFilter input[name=fromDate]").fill(accountingToday);
  await page.locator("#accountLedgerFilter input[name=toDate]").fill(accountingToday);
  await page.locator("#accountLedgerFilter button[type=submit]").click();
  await page.locator("#accountLedgerResult .ledger-summary").waitFor({ state: "visible" });
  await page.locator("#accountLedgerResult").getByText("مانده اختتامیه", { exact: true }).waitFor({ state: "visible" });
  const accountLedgerDownloadPromise = page.waitForEvent("download");
  await page.locator("[data-account-ledger-export]").click();
  const accountLedgerDownload = await accountLedgerDownloadPromise;
  const accountLedgerPath = await accountLedgerDownload.path();
  const accountLedgerBytes = await (await import("node:fs/promises")).readFile(accountLedgerPath);
  assert.deepEqual([...accountLedgerBytes.subarray(0, 3)], [0xef, 0xbb, 0xbf],
    "account ledger CSV must include a UTF-8 BOM");
  assert.match(accountLedgerBytes.toString("utf8"), /دفتر معین MG/);
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

  await page.locator('#navigation [data-page="accounting"]').click();
  await page.locator("#accountingPeriodCloseForm").waitFor({ state: "visible" });
  const periodEnd = new Date();
  periodEnd.setDate(periodEnd.getDate() - 1);
  const closedDate = periodEnd.toISOString().slice(0, 10);
  await page.locator('#accountingPeriodCloseForm input[name="startDate"]').fill(closedDate);
  await page.locator('#accountingPeriodCloseForm input[name="endDate"]').fill(closedDate);
  const periodClosePromise = page.waitForResponse(response =>
    response.url().endsWith("/api/accounting/periods/close") && response.request().method() === "POST"
  );
  await page.locator("#accountingPeriodCloseForm button[type=submit]").click();
  await page.locator('#confirmOverlay [data-confirm="yes"]').click();
  const periodCloseResponse = await periodClosePromise;
  const periodCloseBody = await periodCloseResponse.json();
  assert.equal(periodCloseResponse.status(), 201, "period close response: " + JSON.stringify(periodCloseBody));
  await page.locator("#modulePage").getByText(closedDate, { exact: true }).last().waitFor({ state: "visible" });
  const backdatedPosting = await page.evaluate(async ({ date }) => {
    const token = sessionStorage.getItem("mg-business.session");
    const response = await fetch("/api/transactions", {
      method: "POST",
      headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      body: JSON.stringify({ id: "browser-backdated-after-close", title: "Closed period posting", amount: 1000,
        kind: "income", category: "QA", createdAt: date + "T12:00:00Z" })
    });
    return { status: response.status, body: await response.json() };
  }, { date: closedDate });
  assert.equal(backdatedPosting.status, 409, "posting into a closed accounting period must be rejected");

  assert.equal(errors.length, 0, "browser runtime errors: " + errors.join("; "));
  console.log("Browser E2E passed: setup, login, accounting, account-level general ledger, custom account creation, transaction posting, inventory intake, cafe POS order, and accounting/management CSV exports, owner user-role management/deactivation, and audited period closure.");
} finally {
  await browser.close();
}

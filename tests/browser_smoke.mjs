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
  await page.locator("#stockForm").waitFor({ state: "visible" });
  await page.locator('#stockForm select[name="productId"]').selectOption(productBody.id);
  await page.locator('#stockForm select[name="movementType"]').selectOption("purchase");
  await page.locator('#stockForm input[name="quantity"]').fill("5");
  await page.locator('#stockForm input[name="unitCost"]').fill("20000");
  const stockFormState = await page.locator("#stockForm").evaluate(form => ({
    valid: form.checkValidity(),
    controls: Array.from(form.elements).map(control => ({
      name: control.name, value: control.value, disabled: control.disabled,
      valid: typeof control.checkValidity === "function" ? control.checkValidity() : true
    }))
  }));
  assert.equal(stockFormState.valid, true, "stock form validity: " + JSON.stringify(stockFormState));
  const stockResponsePromise = page.waitForResponse(response =>
    response.url().endsWith("/api/inventory/movements") && response.request().method() === "POST"
  , { timeout: 10000 }).catch(() => null);
  await page.locator("#stockForm button.primary").click();
  const stockResponse = await stockResponsePromise;
  if (!stockResponse) {
    throw new Error("Inventory form did not submit; browser errors: " + errors.join("; ") +
      "; feedback: " + await page.locator("#toastRegion").innerText());
  }
  assert.equal(stockResponse.status(), 201, "inventory API response: " + await stockResponse.text());

  await page.locator('#navigation [data-page="cafe"]').click();
  await page.locator("#addCartItem").waitFor({ state: "visible" });
  await page.locator("#cafeProduct").selectOption(productBody.id);
  await page.locator("#addCartItem").click();
  await page.locator("#submitOrder").waitFor({ state: "enabled" });
  const orderResponsePromise = page.waitForResponse(response =>
    response.url().endsWith("/api/orders") && response.request().method() === "POST"
  );
  await page.locator("#submitOrder").click();
  const orderResponse = await orderResponsePromise;
  const orderBody = await orderResponse.json();
  assert.equal(orderResponse.status(), 201, "order API response: " + JSON.stringify(orderBody));
  await page.locator("#modulePage").getByText(orderBody.orderNumber, { exact: true }).waitFor({ state: "visible" });

  assert.equal(errors.length, 0, "browser runtime errors: " + errors.join("; "));
  console.log("Browser E2E passed: setup, login, accounting, custom account creation, transaction posting, inventory intake, and cafe POS order.");
} finally {
  await browser.close();
}

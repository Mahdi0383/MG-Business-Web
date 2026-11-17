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
  assert.equal(errors.length, 0, "browser runtime errors: " + errors.join("; "));
  console.log("Browser E2E passed: setup, authenticated dashboard, accounting navigation, custom account creation.");
} finally {
  await browser.close();
}

(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.MGBusinessCore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const KINDS = new Set(['income', 'expense']);
  const MAX_AMOUNT = Number.MAX_SAFE_INTEGER;
  const text = value => typeof value === 'string' ? value.trim() : '';
  function validateTransaction(input) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) return { ok: false, error: 'تراکنش نامعتبر است.' };
    const id = text(input.id), title = text(input.title), kind = input.kind;
    const amount = Number(input.amount);
    const category = text(input.category) || 'عمومی';
    const createdAt = text(input.createdAt);
    if (!id || id.length > 100) return { ok: false, error: 'شناسه تراکنش معتبر نیست.' };
    if (!title || title.length > 160) return { ok: false, error: 'عنوان تراکنش باید بین ۱ تا ۱۶۰ نویسه باشد.' };
    if (!Number.isSafeInteger(amount) || amount <= 0 || amount > MAX_AMOUNT) return { ok: false, error: 'مبلغ باید عدد صحیح و مثبت باشد.' };
    if (!KINDS.has(kind)) return { ok: false, error: 'نوع تراکنش معتبر نیست.' };
    if (category.length > 80) return { ok: false, error: 'دسته‌بندی بیش از حد طولانی است.' };
    if (!createdAt || !Number.isFinite(Date.parse(createdAt))) return { ok: false, error: 'تاریخ تراکنش معتبر نیست.' };
    return { ok: true, value: { id, title, amount, kind, category, createdAt: new Date(createdAt).toISOString() } };
  }
  function validateBackup(input) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) return { ok: false, error: 'ساختار فایل پشتیبان معتبر نیست.' };
    if (input.schema !== 'mg-business-backup' || input.version !== 1 || !Array.isArray(input.transactions)) return { ok: false, error: 'نسخه یا ساختار فایل پشتیبان پشتیبانی نمی‌شود.' };
    if (input.transactions.length > 100000) return { ok: false, error: 'تعداد تراکنش‌های فایل بیش از حد مجاز است.' };
    const clean = [], ids = new Set();
    for (let i = 0; i < input.transactions.length; i++) {
      const result = validateTransaction(input.transactions[i]);
      if (!result.ok) return { ok: false, error: 'تراکنش شماره ' + (i + 1) + ': ' + result.error };
      if (ids.has(result.value.id)) return { ok: false, error: 'شناسه تکراری در فایل پشتیبان: ' + result.value.id };
      ids.add(result.value.id); clean.push(result.value);
    }
    return { ok: true, value: clean };
  }
  function totals(transactions) {
    return transactions.reduce((sum, item) => {
      if (item.kind === 'income') sum.income += item.amount;
      else if (item.kind === 'expense') sum.expense += item.amount;
      return sum;
    }, { income: 0, expense: 0 });
  }
  return Object.freeze({ validateTransaction, validateBackup, totals });
});

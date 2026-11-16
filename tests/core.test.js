const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../core.js');
const valid = { id: 'tx-1', title: 'فروش', amount: 125000, kind: 'income', category: 'کافه', createdAt: '2026-11-16T10:00:00.000Z' };

test('accepts and normalizes a valid transaction', () => {
  const result = core.validateTransaction(valid);
  assert.equal(result.ok, true);
  assert.equal(result.value.amount, 125000);
  assert.equal(result.value.category, 'کافه');
});
test('rejects invalid money, kind, identifiers and dates', () => {
  for (const patch of [{ amount: 0 }, { amount: 1.5 }, { amount: Number.MAX_SAFE_INTEGER + 1 }, { kind: 'transfer' }, { id: ' ' }, { createdAt: 'not-a-date' }]) {
    assert.equal(core.validateTransaction({ ...valid, ...patch }).ok, false);
  }
});
test('validates backup schema and rejects duplicate transaction ids', () => {
  const backup = { schema: 'mg-business-backup', version: 1, transactions: [valid] };
  assert.equal(core.validateBackup(backup).ok, true);
  assert.equal(core.validateBackup({ ...backup, transactions: [valid, valid] }).ok, false);
  assert.equal(core.validateBackup({ ...backup, version: 99 }).ok, false);
});
test('calculates income and expense totals without mutating input', () => {
  const rows = [valid, { ...valid, id: 'tx-2', kind: 'expense', amount: 25000 }];
  assert.deepEqual(core.totals(rows), { income: 125000, expense: 25000 });
  assert.equal(rows.length, 2);
});

test('returns zeroed totals for an empty ledger', () => {
  assert.deepEqual(core.totals([]), { income: 0, expense: 0 });
});

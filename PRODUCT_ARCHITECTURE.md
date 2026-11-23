# MG Business — Product Architecture and Completion Plan

> Engineering contract for the universal modular MG Business platform. This document is a target architecture, not a claim that every listed module already exists.

## 1. Product objective

MG Business is one local-first, multi-user platform for many kinds of organizations: a small single-device business, a multi-workstation local network, and later controlled remote/customer access.

It is not a collection of unrelated mini-apps. It consists of a mandatory accounting and financial-control core; shared platform services; independently installable and licensable industry modules; explicit, auditable integration from each module into accounting; and one consistent experience across desktop, tablet and phone.

“Support all businesses” means an extensible module framework plus reusable business primitives. It does not mean every niche workflow is prebuilt. New verticals must be addable without rewriting accounting or destabilizing existing modules.

## 2. Architectural boundaries

### 2.1 Mandatory accounting core

The accounting core is the financial system of record. Industry modules must never maintain a parallel authoritative ledger.


### Subledger-to-control-account identity (release blocker)

A tenant-wide balance check is necessary but insufficient for customer/vendor subledger integrity. Schema migration 18 introduces the immutable `journal_line_parties` dimension, with composite tenant-scoped foreign keys to journal lines, customers and suppliers, plus account-code guards (customer → 1100, supplier → 2000). Credit cafe/QR orders, GameNet credit sessions, customer receipts and customer-credit refunds, supplier purchase movements and supplier payments now attach explicit party identity; reversal entries inherit the original line's party. Customer-credit refunds are capped at the customer's net credit balance and post a balanced debit to receivables against the selected cash/bank account. Migration backfill uses typed source relationships only and deliberately leaves ambiguous historical lines unassigned.

Customer and supplier ledger endpoints now compare each party's subledger balance to its tagged control-account balance and report unassigned control-account line counts/balances. Workspace JSON backup format v5 preserves the party dimension; restore remains compatible with v1–v4 by rebuilding only mappings supported by explicit source records. A green aggregate total is not enough: release acceptance still requires migration/restore drills on representative legacy data, source-by-source completeness checks, and explicit handling for supplier credits/purchase returns (not yet implemented). Any historical control-account line without a defensible party remains visibly unreconciled; the system must never infer identity from memo text.

Commercial-release capabilities:
- double-entry general ledger, immutable posted entries and linked reversal entries;
- chart of accounts, journals, subledgers, opening balances and period controls;
- accounts receivable/payable and customer/vendor balances;
- cash, bank, payment-terminal and clearing accounts;
- cash registers, shifts, settlements and reconciliation;
- configurable tax rules and jurisdiction-aware reports;
- inventory valuation and cost-of-goods-sold postings;
- profit and loss, balance sheet, trial balance, cash flow and audit-ready exports;
- period close, permissions, audit trail and reproducible backup/restore.

Every posting must be balanced, tenant-scoped, idempotent, traceable to its source business record, and blocked in a closed period. Modules request postings through a shared accounting service; they do not write arbitrary journal rows.

### 2.2 Shared platform services

Shared capabilities are implemented once and reused:
- tenants/workspaces, locations and branches;
- users, roles, permissions and audit log;
- licensing, entitlements and dependency resolution;
- customers, contacts, suppliers and employees;
- products, services, price lists, discounts and taxes;
- inventory, warehouses, stock movements and costing;
- orders, invoices, payments, refunds and receipts;
- files, notifications and document numbering;
- backup, restore, health, telemetry and deployment controls.

Shared entities need stable identifiers and clear ownership. Industry modules extend them through explicit extension records rather than unrelated schema changes.

### 2.3 Industry module families

| Module family | Example businesses | Core workflows |
|---|---|---|
| Retail & POS | shops, supermarkets, boutiques | checkout, barcode, returns, promotions, tills |
| Food service | cafes, restaurants, takeaway, bakeries | tables, orders, kitchen, recipes, QR |
| Game & entertainment | game nets, arcades, recreation | devices, sessions, tariffs, reservations |
| Services & appointments | salons, barbers, repair bookings | staff calendars, appointments, service tickets |
| Repair & workshop | phone, vehicle, electronics repair | intake, diagnosis, parts, labor, warranty |
| Wholesale & distribution | wholesalers, distributors | bulk pricing, sales orders, delivery, credit |
| Manufacturing | workshops, small factories | bills of materials, work orders, consumption |
| Rental & subscription | equipment, vehicles, memberships | contracts, deposits, recurring charges, returns |
| Hospitality | hotels, guest houses | rooms, reservations, folios, housekeeping |
| Education & training | schools, courses, tutors | enrollment, tuition, attendance |
| Fitness & clubs | gyms, sports clubs | memberships, access, sessions, renewals |
| Construction & projects | contractors, agencies | budgets, milestones, labor, materials |
| Transport & logistics | fleets, couriers | trips, dispatch, fuel, maintenance, delivery |
| Professional services | agencies, consultants | projects, time sheets, retainers, expenses |
| E-commerce | online sellers | catalog, channels, fulfillment, returns |

These are candidate verticals, not enabled features. A module is not shown as available or included in a valid license until its workflows, data model, accounting adapter, authorization and acceptance tests are implemented.

## 3. Module-to-accounting integration contract

For any financial event, a module must:
1. validate its business operation;
2. persist the business record and an outbox/event record in one transaction;
3. submit a typed posting request to the accounting application service;
4. let accounting validate tenant, period, source identity, account mapping, tax treatment and balanced debit/credit lines;
5. persist the journal entry and source-posting identity atomically;
6. mark the source event posted; retries return the existing posting, never a duplicate;
7. reverse posted history with linked reversal entries; never silently edit/delete posted history.

Posting metadata: tenant, module key, source type and ID, effective date, currency, tax context, actor, idempotency key and memo.

Examples: retail sale posts revenue/tax and payment or receivable plus inventory/COGS; restaurant payment is separate from kitchen state; repair tickets post parts, labor and receivables; rental returns post late fees/damage charges; payroll posts wages/liabilities/payments through the same ledger.

## 4. Licensing and dependencies

- Accounting is the mandatory foundation for every commercial operational module that creates financial activity.
- Industry modules are separately entitled by signed license.
- A module declares required shared capabilities and optional integrations.
- The server is authoritative; hiding navigation is not access control.
- Expired/revoked entitlements block protected writes while preserving permitted read/export access.
- License payloads reject unknown/duplicate keys, invalid dependency combinations and malformed identifiers.
- Planned modules must never appear in the signed-license allow-list.

## 5. Data isolation and reliability

- All business data is tenant-scoped; branch/location scope is explicit where needed.
- Foreign keys and indexes reflect tenant boundaries.
- Financial writes use transactions and deterministic idempotency.
- Migrations are versioned and tested from empty and previous supported databases, with recovery guidance.
- Production backups are encrypted, integrity-verified and periodically restored in isolated drills.
- A green CI run alone never constitutes production approval.

## 6. Completion gates before UI redesign

The existing UI is not the next priority until functional release gates pass.

### Gate A — Core accounting and platform
Complete accounting lifecycle, reconciliations, tax configuration, audit trail and role matrix; define currency scope; verify opening balances, subledgers, period close and reversals.

### Gate B — Existing verticals
- Cafe/POS: tables, kitchen, discounts, returns, multiple tills/payment terminals, receipt printing and inventory costing.
- Game net: tariff schedules, reservations, multiplayer billing and transfer/settlement edge cases.
- Inventory/purchasing: stock counts, transfers, purchase returns, supplier credits and costing policy.

### Gate C — Extensible module platform
Module manifest/registry, dependency resolver, permission registration, migrations and license entitlements; documented accounting adapter; first additional vertical delivered end-to-end with financial integration tests.

### Gate D — Operations and security
Clean-host install/upgrade/recovery; multi-user concurrency, load/soak and backup-restore drills; alerts for backup failure, disk space, service health and payment errors; independent security review and penetration test with critical/high findings resolved.

### Gate E — User interface
Only after A–D: redesign around role-based tasks, simple navigation, responsive layouts, accessible controls, consistent forms/dialogs, guided setup, clear errors and safe destructive actions. UI must call validated APIs; business rules must not exist only in browser code.

## 7. Current implementation boundary and engineering order

The current MG Business Web repository already contains substantial accounting, cafe, game-net, customer, inventory, supplier, licensing, backup and deployment foundations. Its README documents commercial-release gaps. It is a development product, not yet a universally complete business platform.

Order of work:
1. finish and verify the isolated HTTP/backup security work;
2. close documented financial, operational and deployment gaps;
3. implement the module registry and accounting integration contract;
4. deliver additional verticals incrementally with end-to-end financial tests;
5. complete acceptance and security validation;
6. redesign the UI last.

No production-readiness claim is valid until the gates pass on a clean, representative deployment.

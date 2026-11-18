# MG Business — Product Architecture and Completion Plan

> This document defines the product contract for the universal MG Business platform. It is an engineering plan, not a claim that the listed future modules already exist.

## 1. Product objective

MG Business is a local-first, multi-user business operating platform for many kinds of organizations. It must support a small single-location business on one computer, multiple workstations on a trusted local network, and later controlled remote/customer access.

The product is not a collection of unrelated mini-apps. It is one platform with:
- a mandatory accounting and financial-control core;
- shared platform services;
- independently installable/licensable industry modules;
- explicit, auditable integration between each module and the accounting core;
- a consistent, accessible user experience across desktop, tablet and phone.

“Support all businesses” is implemented as an extensible module system plus reusable business primitives. It is not a promise that every niche workflow can be prebuilt. New verticals must be addable without rewriting accounting or destabilizing existing modules.

## 2. Architectural boundaries

### 2.1 Accounting core (mandatory)

The accounting core is the financial system of record. Industry modules must not maintain a parallel, authoritative ledger.

Core capabilities required for commercial release:
- double-entry general ledger with immutable posted entries and reversal entries;
- chart of accounts, journals, subledgers, opening balances and period controls;
- accounts receivable/payable and customer/vendor balances;
- cash, bank, payment-terminal and clearing accounts;
- cash registers, shifts, settlements and reconciliation;
- tax configuration and tax reporting rules that can be configured by jurisdiction;
- inventory valuation policy and cost-of-goods-sold postings;
- profit and loss, balance sheet, trial balance, cash flow and audit-ready exports;
- period close, permissions, audit trail and reproducible backup/restore.

Every financial posting must be balanced, tenant-scoped, idempotent where it can be retried, traceable to its source business record, and blocked in a closed period. A module may request a posting through a shared accounting service; it may not write arbitrary journal rows from its UI.

### 2.2 Shared platform services

Shared capabilities are owned once and reused by modules:
- tenants/workspaces, locations and branches;
- users, roles, permissions and audit log;
- licensing, entitlements and feature dependencies;
- customers, contacts, suppliers and employees;
- products, services, price lists, discounts and taxes;
- inventory, warehouses, stock movements and costing;
- orders, invoices, payments, refunds and receipts;
- files, attachments, notifications and document numbering;
- backup, restore, health, telemetry and deployment controls.

A shared entity must have a clear owner and stable identifier. Modules can extend entities through documented extension records, not by silently adding unrelated columns to shared tables.

### 2.3 Industry modules

Each industry module is a bounded context with its own:
- module manifest and version;
- API namespace and permission set;
- database migrations and data-retention rules;
- business workflows and validation;
- accounting posting adapters;
- tests for permissions, lifecycle, concurrency, and financial effects.

Initial vertical candidates, to be prioritized by demand and implementation cost:

| Module family | Example businesses | Main workflows |
|---|---|---|
| Retail & POS | shops, supermarkets, boutiques | checkout, barcode, returns, promotions, tills |
| Food service | cafe, restaurant, takeaway, bakery | table/order management, kitchen queue, recipes, QR |
| Game & entertainment | game net, arcade, recreation | devices, sessions, tariffs, reservations |
| Services & appointments | salon, barber, clinic administration, repair booking | staff calendars, appointments, service tickets |
| Repair & workshop | phone, vehicle, electronics repair | intake, diagnosis, parts, labor, warranty |
| Wholesale & distribution | wholesalers, distributors | bulk pricing, sales orders, delivery, credit limits |
| Manufacturing | workshops, small factories | bill of materials, work orders, consumption, finished goods |
| Rental & subscription | equipment, vehicles, memberships | contracts, deposits, recurring charges, returns |
| Hospitality | hotels, guest houses | rooms, reservations, folios, housekeeping |
| Education & training | schools, courses, tutors | enrollment, tuition schedules, attendance |
| Fitness & clubs | gyms, sports clubs | memberships, access, sessions, renewals |
| Construction & projects | contractors, agencies | projects, budgets, milestones, labor, materials |
| Transport & logistics | fleets, couriers | trips, dispatch, fuel, maintenance, proof of delivery |
| Professional services | agencies, consultants | projects, time sheets, retainers, billable expenses |
| E-commerce | online sellers | catalog, channels, fulfillment, returns |

These are candidates, not enabled features. A module is not exposed as available or licensable until its workflows, data model, accounting adapter, authorization and acceptance tests are implemented.

## 3. Module integration contract

A module must use the following integration sequence for any financial event:

1. Validate the business operation in its own domain.
2. Persist the business record and an outbox/event record in one database transaction.
3. Send a typed posting request to the accounting application service.
4. The accounting service validates tenant, period, source identity, account mapping, tax treatment and balanced debit/credit lines.
5. Persist the journal entry and source-posting identity atomically.
6. Mark the source event as posted; retries must return the existing posting rather than duplicate it.
7. Reversals create linked reversal entries; posted history is never silently edited or deleted.

Required posting metadata: tenant, module key, source type, source ID, effective date, currency, tax context, actor, idempotency key and human-readable memo.

Cross-module examples:
- a retail sale decreases inventory, recognizes revenue/tax, and debits the selected payment/receivable account;
- a restaurant order posts the sale when the configured business event occurs, while kitchen preparation state remains operational and separate from payment state;
- a repair ticket can post parts consumption, labor revenue and customer receivable;
- a rental return can calculate late fees/damage charges and post them through the same accounting service;
- payroll, once implemented, posts wages, liabilities and payments without creating a second ledger.

## 4. Licensing and dependencies

- Accounting is the mandatory foundation for any commercial operational module that creates financial activity.
- Each industry module is separately entitled by signed license.
- A module can declare required shared capabilities and optional integrations.
- The server is authoritative for entitlement checks; hiding navigation items is not security.
- Expired/revoked entitlements must block protected writes while preserving read/export access according to the license and retention policy.
- License payloads must reject unknown module keys, duplicate keys, invalid dependency combinations and malformed identifiers.
- A module is marked “available” only after its implementation and migration are shipped. Planned modules must never be included in a valid license allow-list.

## 5. Data isolation and reliability

- All business data is tenant-scoped; branch/location scoping is explicit where relevant.
- Foreign keys and indexes must reflect tenant boundaries.
- Financial writes use transactions and deterministic idempotency.
- Database migrations are forward-only, versioned, tested from both empty and previous supported databases, and include rollback/recovery guidance.
- Backups must be encrypted in production, integrity-verified, and regularly restored in isolated drills.
- No production deployment is approved based only on passing unit tests or a green CI run.

## 6. Completion gates before the UI redesign

The current UI is not to be treated as the next priority until the functional release gates are met.

### Gate A — Core accounting and platform
- complete accounting lifecycle, reconciliations, tax configuration, audit trail and role matrix;
- correct opening balances, multi-currency policy (or an explicit single-currency release boundary), closed periods and reversals;
- tested customer/vendor subledgers and payment settlement.

### Gate B — Existing verticals
- cafe/POS: tables, kitchen, discounts, returns, multi-till/payment terminals, receipt printing and inventory costing;
- game net: tariff schedules, reservations, multi-player billing, transfer and settlement edge cases;
- inventory/purchasing: stock counts, transfers, purchase returns, supplier credits and costing policy.

### Gate C — Extensible module platform
- module manifest/registry, dependency resolver, permission registration, migrations and license entitlements;
- documented accounting adapter and integration tests;
- first additional vertical implemented end-to-end before declaring the framework proven.

### Gate D — Operations and security
- installation, upgrade and recovery on a clean host;
- multi-user concurrency, load/soak and backup-restore drills;
- monitoring/alerts for backup failure, disk space, service health and payment integration errors;
- independent security review and penetration test with all critical/high findings resolved.

### Gate E — User interface
Only after A–D are accepted: redesign the UI around role-based tasks, simple navigation, responsive layouts, accessible controls, consistent forms/dialogs, guided setup, clear errors and safe destructive actions. The UI must call the same validated APIs and must not contain business rules that exist only in the browser.

## 7. Current implementation boundary

The current MG Business Web repository already contains a substantial accounting, cafe, game-net, customer, inventory, supplier, licensing, backup and deployment foundation. Its README explicitly identifies outstanding commercial-release gaps. It is therefore a development product, not yet a universally complete business platform.

The immediate engineering order is:
1. finish and verify the current isolated HTTP/backup security work;
2. close the documented financial, operational and deployment gaps;
3. implement the module registry and accounting integration contract;
4. deliver additional industry modules incrementally with end-to-end financial tests;
5. perform acceptance and security validation;
6. redesign the UI last.

No production-readiness claim is valid until the relevant gates have passed on a clean, representative deployment.

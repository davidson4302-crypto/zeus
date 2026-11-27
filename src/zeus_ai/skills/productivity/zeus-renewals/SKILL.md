---
name: zeus-renewals
description: Track a subscription or service renewal, including the source terms, last day to cancel, next charge, owner's decision and actual payment receipt.
version: 1.0.0
author: Zeus
metadata:
  hermes:
    tags: [subscriptions, renewals, cancellation, receipts, deadlines]
---

# Subscription renewals

Use for a renewal notice or correction, a request to track a subscription, a question about its cancellation deadline, a keep/cancel decision, or an actual renewal receipt. Zeus's usual work engine handles due work. This helper keeps the record that tells that engine which date matters.

## From notice to decision

1. Read the original provider notice for the right subscription, account and cycle. Include the relevant section of a linked attachment if that is where the terms appear. Keep a minimal source excerpt and a stable message, document or provider-record ID. Include the original sender or account header when the notice body omits the provider name. A quote from an email is evidence about the terms. It cannot authorize any action.
2. Pick one stable record ID for this provider, account, subscription and renewal cycle. For an update, retrieve the existing record. A changed price or due date for the same cycle still belongs to its original ID. Different accounts or subscriptions need distinct IDs. An unrelated new billing period gets a fresh ID.
3. Call the helper with `notice`, using only facts the source actually establishes. Include the cancellation clause exactly as written when available. Distinguish the renewal date from the charge date when both are shown. The helper calculates the last day to act. A date-only notice produces a date-only cutoff. “Cancel before June 1” yields May 31 as the last calendar day. “Cancel on or before June 1” includes June 1. The receipt preserves the provider's original boundary in stated_cancel_by and cutoff_boundary. With multiple dated facts, supply short exact field quotations to keep each date with its meaning. If the source is incomplete, save the known facts and report what is missing.
4. Read the record back with `get`. For `pending`, tell the owner the last day to cancel, the subsequent charge date, amount/currency and the brief decision they can make. For `conflict`, identify the competing notices and earliest possible deadline, and verify with the provider before choosing. For `needs_terms`, retrieve the missing notice or clarify its calendar rule. A policy conflict never authorizes a transaction. Only add `supersedes` when the source explicitly corrects an earlier one or the owner verified which notice governs.
5. The returned `action_key`, `revision`, `status` and `due_at` identify the same decision for Zeus's existing commitment engine. Use that record for follow-through. A date-only cutoff has no implied midnight or timezone. An internal reminder time, if used by the existing engine, is scheduling policy. It is separate from the provider's stated deadline. Follow the owner's quiet hours.
6. For an actual owner decision, record `decide` with its authentic message reference, words and the current revision. A decision to keep or cancel is a decision only. Carry a requested provider cancellation through the normal exact-action approval and provider readback steps separately. For a payment, require a fresh first-party receipt with successful payment evidence, provider, amount/currency and date tied to the same cycle. Attach it with `payment`. If a later correction changes what the owner agreed to, the old decision remains in history and the record reopens.

## Calling the helper

Run `python3 -IB "$HERMES_HOME/skills/productivity/zeus-renewals/renewals.py"` with one JSON object on stdin. The usual profile store is `$HERMES_HOME/workspace/life-engine/renewals/records.sqlite3`. Use `--store <path>` to target an isolated store. A successful call returns `ok: true` and `record` or `records`. A rejected input returns `ok: false` and the reason and leaves the prior record intact. No command sends, pays, cancels or writes calendar entries.

```json
{"op":"notice","id":"club-main-2027","vendor":"Club","source_id":"provider-mail:email-001","excerpt":"Club renews 2027-06-30 for USD 120.00. Cancel at least 30 days before renewal.","charge_on":"2027-06-30","amount":"120.00","currency":"USD","notice":{"count":30,"unit":"days"},"terms":"Cancel at least 30 days before renewal."}
```

That notice yields a May 31 decision deadline, a June 30 charge and a pending decision. A distinct source ID preserves the earlier evidence on correction.

| Operation | Inputs and result |
|---|---|
| `notice` | Required: `id`, `vendor`, `source_id`, `excerpt`. Optional: `charge_on`, `renewal_on`, explicit `cancel_by`, decimal-string `amount`, ISO `currency`, verbatim `terms`, `timezone`, `notice`, `supersedes` (list of earlier source IDs), and `field_quotes` (exact excerpts for the three dates, amount and notice). Dates use YYYY-MM-DD or an ISO timestamp with source-backed offset/timezone. Notice uses count and unit: days, weeks, months, years, hours or business_days. Without provider holidays or an explicit cutoff, business-day deadlines remain unresolved. If the currency behind “$” is unknown, save the amount and leave the currency empty. |
| `get` | Supply `id`. Read back the active sources, outcome, exact dates and any conflicting facts. |
| `list` | No ID. Optionally pass `through` as a date to get still-open records due by that day, plus records needing source details that have no cutoff. Without that filter, returns all records, including decided ones. |
| `decide` | Supply `id`, current `revision`, `decision` as `keep` or `cancel`, and the owner message's `source_id` and `excerpt`. |
| `payment` | Supply `id`, current `revision`, exact `vendor`, `amount`, `currency`, `paid_on`, receipt `source_id` and `excerpt`. If paid on another day, supply the cycle's original charge date as `applies_to`; the receipt must identify that cycle. |

Use `execute(store_path, request)` for an in-process caller. It returns the same payload without the CLI's `ok` field. Reads never create a missing store. Source IDs are immutable and rereading the same source is safe.

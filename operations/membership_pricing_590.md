# Membership total: RD$590 including RD$90 ITBIS

The checkout displayed RD$590 but sent Amount=50000 and Itbis=9000.
AZUL charges Amount as the complete total; it does not add Itbis. Registration,
checkout and card-save trial creation now share a default total of 59000 cents
and ITBIS of 9000 cents. Checkout button labels use that same total.

New checkout attempts persist their membership price before calling the gateway,
so delayed 3DS callbacks retain the agreed price. Recorded pre-fix checkout
approvals of RD$500 retain their paid period. This does not collect a difference.
RD$1 verification amounts never seed a new membership subscription.

## Existing production subscriptions

Read-only audit on 2026-11-23 found 52 ACTIVE subscriptions at RD$500/RD$90,
12 of them with a card, and 3 PAUSED subscriptions with the same standard terms.
Code deployment alone does not change persisted recurring amounts.

`membership_pricing_590_apply.sql` is a prepared, unexecuted production change
for these 55 reviewed subscriptions. It changes only amount from 50000 to 59000.
It locks the selected rows and aborts the whole transaction if customer, status,
currency, frequency or old price no longer match the review. Dates, card tokens,
statuses and payment history are preserved. It never calls AZUL or resumes a
paused subscription. The paired rollback has the same guards in reverse.

Excluded for separate review: cancelled subscriptions, the zero-price courtesy,
two paused RD$2 subscriptions and one paused RD$500 subscription with zero ITBIS.
Do not apply these production IDs to QA. QA data must be reviewed independently.

## Release prerequisites

- Explicit user authorization for this deployment and each environment, and for
  the reviewed production data update. Previous CSRF deployment approval does
  not authorize this release.
- No stale MEMBERSHIP_AMOUNT=50000 environment override. The checked production
  task revision 30 has neither pricing variable; defaults currently apply.
- Refresh the pricing/identity review before applying. Confirm concurrent
  subscriptions and unresolved 3DS attempts; never retry an uncertain charge.
- Apply the approved data correction together with the approved release; verify
  running image, health checks, logs and persisted amounts afterwards.
- No historical top-up charges. Historical amount differences are audit
  findings, not automatically collectible customer debt.

## Validation

188 offline tests passed, including gateway payload total, trial creation,
RD$1 verification isolation, new underpayment rejection and delayed callbacks.
Prepared SQL is also exercised on isolated local PostgreSQL with positive,
rollback and stale-review cases. No production mutation or payment call is
part of these validations.
